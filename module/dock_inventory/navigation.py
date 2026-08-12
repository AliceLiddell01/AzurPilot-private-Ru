"""Game Settings prerequisite and UI navigation for Dock Inventory Stage 2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from module.base.timer import Timer
from module.exception import GamePageUnknownError
from module.game_settings.definitions import CUSTOM_SHIP_NAMES
from module.game_settings.model import GameSettingState
from module.game_settings.preflight import GameSettingsPreflightScanner
from module.game_settings.snapshot import (
    DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    GameSettingsSnapshotAccessSource,
    GameSettingsSnapshotStatus,
    get_or_refresh_game_settings_snapshot,
)
from module.logger import logger
from module.ui.page import page_dock, page_main, page_main_white

from module.dock_inventory.mumu_traversal import DockMuMuInventoryTraversal
from module.dock_inventory.traversal import (
    DockTraversalResult,
    DockViewportVisitor,
)


@dataclass(frozen=True, slots=True)
class DockPrerequisiteEvidence:
    """Auditable evidence for the Dock-critical Game Settings requirement."""

    snapshot_path: Path
    snapshot_source: GameSettingsSnapshotAccessSource
    cache_status: GameSettingsSnapshotStatus | None
    scanned_at: datetime
    detected: GameSettingState
    required: GameSettingState
    compatible: bool


@dataclass(frozen=True, slots=True)
class DockInventoryStage2Result:
    prerequisite: DockPrerequisiteEvidence
    traversal: DockTraversalResult


class DockInventoryPrerequisiteError(RuntimeError):
    def __init__(self, message: str, evidence: DockPrerequisiteEvidence) -> None:
        super().__init__(message)
        self.evidence = evidence


class DockInventoryNavigationError(RuntimeError):
    """Dock entry, confirmation, or cleanup failed."""


class DockInventoryNavigator(GameSettingsPreflightScanner):
    """High-level Stage 2 runtime using the existing UI and Game Settings stack."""

    game_settings_snapshot_path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH
    DOCK_STABILITY_TIMEOUT = 3.0
    DOCK_STABILITY_MIN_CAPTURES = 2
    DOCK_STABILITY_MAX_CAPTURES = 12

    def _make_game_settings_scanner(self) -> GameSettingsPreflightScanner:
        # Reuse this UI/device owner so a cache miss performs one controlled
        # live audit and returns to Main before Dock navigation begins.
        return self

    def check_dock_prerequisite(
        self,
        *,
        snapshot_path: Path | str | None = None,
        max_age: timedelta | None = None,
        force_refresh: bool = False,
        scanner_factory: Callable[[], GameSettingsPreflightScanner] | None = None,
    ) -> DockPrerequisiteEvidence:
        """Require only ``custom_ship_names=OFF`` using snapshot-first access."""
        path = Path(
            self.game_settings_snapshot_path if snapshot_path is None else snapshot_path
        )
        access = get_or_refresh_game_settings_snapshot(
            self._make_game_settings_scanner if scanner_factory is None else scanner_factory,
            path=path,
            max_age=max_age,
            force_refresh=force_refresh,
        )
        check = access.snapshot.scan_result.get(CUSTOM_SHIP_NAMES.key)
        if check is None or not isinstance(check.detected_value, GameSettingState):
            raise RuntimeError(
                "В валидном снимке игровых настроек отсутствует типизированное "
                "значение custom_ship_names."
            )

        required = check.required_value
        if not isinstance(required, GameSettingState):
            raise RuntimeError(
                "Для custom_ship_names отсутствует типизированное требование."
            )
        evidence = DockPrerequisiteEvidence(
            snapshot_path=path,
            snapshot_source=access.source,
            cache_status=access.cache_status,
            scanned_at=access.snapshot.scanned_at,
            detected=check.detected_value,
            required=required,
            compatible=access.snapshot.satisfies((CUSTOM_SHIP_NAMES.key,)),
        )
        logger.info(
            "[Dock Inventory] Предварительное условие: snapshot=%s, source=%s, "
            "cache_status=%s, scanned_at=%s, custom_ship_names=%s, "
            "required=%s, compatible=%s",
            evidence.snapshot_path,
            evidence.snapshot_source.value,
            evidence.cache_status.value if evidence.cache_status is not None else None,
            evidence.scanned_at.isoformat(),
            evidence.detected.value,
            evidence.required.value,
            evidence.compatible,
        )
        if evidence.detected is GameSettingState.UNKNOWN:
            raise DockInventoryPrerequisiteError(
                "Dock Inventory заблокирован: значение Custom Ship Names неизвестно.",
                evidence,
            )
        if not evidence.compatible:
            raise DockInventoryPrerequisiteError(
                "Dock Inventory заблокирован: Custom Ship Names должен быть OFF.",
                evidence,
            )
        return evidence

    def capture_stable_dock_frame(self) -> np.ndarray:
        """Require repeated card hashes and detach the proven stable frame."""
        from module.retire.scanner import HashGenerator

        scanner = HashGenerator()
        previous = None
        timeout = Timer(
            self.DOCK_STABILITY_TIMEOUT,
            count=self.DOCK_STABILITY_MIN_CAPTURES,
        ).start()
        captures = 0
        while captures < self.DOCK_STABILITY_MAX_CAPTURES:
            self.device.screenshot()
            captures += 1
            frame = np.array(self.device.image, copy=True)
            current = scanner.scan(frame, cached=False, output=False)
            if previous is not None and current == previous:
                self.device.image = frame
                return frame
            previous = current
            if timeout.reached():
                break
        raise DockInventoryNavigationError(
            "Dock не достиг стабильного состояния по последовательным card-hash "
            f"кадрам: captures={captures}, timeout={self.DOCK_STABILITY_TIMEOUT:.1f}s."
        )

    def enter_dock(self) -> bool:
        """Navigate through the current page graph and confirm ``page_dock``."""
        changed = self.ui_ensure(page_dock)
        self.ui_get_current_page(skip_first_screenshot=True)
        if self.ui_current is not page_dock:
            raise GamePageUnknownError(
                "[Dock Inventory] Dock не подтверждён после навигации."
            )
        self.capture_stable_dock_frame()
        return changed

    def leave_dock(self) -> bool:
        """Return to either supported Main variant and confirm it."""
        changed = self.ui_goto_main()
        self.ui_get_current_page(skip_first_screenshot=True)
        if self.ui_current is not page_main and self.ui_current is not page_main_white:
            raise GamePageUnknownError(
                "[Dock Inventory] Main не подтверждён после выхода из Dock."
            )
        return changed

    def traverse_dock(
        self,
        visitor: DockViewportVisitor,
        **traversal_kwargs: object,
    ) -> DockTraversalResult:
        return DockMuMuInventoryTraversal(self, **traversal_kwargs).traverse(visitor)

    def run_stage2(
        self,
        visitor: DockViewportVisitor,
        *,
        snapshot_path: Path | str | None = None,
        max_age: timedelta | None = None,
        force_refresh: bool = False,
        scanner_factory: Callable[[], GameSettingsPreflightScanner] | None = None,
        traversal_kwargs: dict[str, object] | None = None,
    ) -> DockInventoryStage2Result:
        """Check prerequisite, traverse Dock, and preserve primary error on cleanup."""
        prerequisite = self.check_dock_prerequisite(
            snapshot_path=snapshot_path,
            max_age=max_age,
            force_refresh=force_refresh,
            scanner_factory=scanner_factory,
        )
        primary_error: Exception | None = None
        navigation_started = False
        try:
            navigation_started = True
            self.enter_dock()
            traversal = self.traverse_dock(
                visitor,
                **({} if traversal_kwargs is None else traversal_kwargs),
            )
            return DockInventoryStage2Result(prerequisite, traversal)
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            if navigation_started:
                try:
                    self.leave_dock()
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise DockInventoryNavigationError(
                            "Обход Dock завершён, но возврат на Main не подтверждён."
                        ) from cleanup_error
                    primary_error.add_note(
                        "Дополнительно не удалось подтвердить возврат на Main: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                    logger.warning(
                        "[Dock Inventory] Ошибка cleanup не маскирует основную ошибку: %s",
                        type(cleanup_error).__name__,
                    )
