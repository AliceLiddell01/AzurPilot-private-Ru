from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from module.exception import GamePageUnknownError
from module.game_settings.model import (
    FrameRateValue,
    GameSettingState,
    GameSettingsScanResult,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.registry import GAME_SETTINGS_OPTIONS_REGISTRY
from module.game_settings.snapshot import (
    GameSettingsSnapshotAccessSource,
    GameSettingsSnapshotStatus,
    save_game_settings_snapshot,
)
from module.ui.page import page_dock, page_main, page_main_white

from module.dock_inventory.navigation import (
    DockInventoryNavigationError,
    DockInventoryNavigator,
    DockInventoryPrerequisiteError,
    DockInventoryStage2Result,
    DockPrerequisiteEvidence,
)
from module.dock_inventory.traversal import DockTraversalResult


_EXPECTED = (
    ("frame_rate", FrameRateValue.FPS_60),
    ("opsi_reduce_tb_guidance", GameSettingState.ON),
    ("opsi_auto_use_items", GameSettingState.ON),
    ("opsi_default_auto_mode_threat_safe", GameSettingState.OFF),
    ("story_autoplay", StoryAutoplayValue.ENABLED),
    ("text_auto_scroll_speed", TextAutoScrollSpeedValue.VERY_FAST),
    ("enable_idle_screen", GameSettingState.OFF),
    ("duplicate_ship_display", GameSettingState.OFF),
    ("display_quick_switch_prompt", GameSettingState.OFF),
    ("display_battle_result_cutscene", GameSettingState.OFF),
    ("custom_ship_names", GameSettingState.OFF),
)


def _settings_result(
    custom_ship_names: GameSettingState,
    overrides: dict[str, object] | None = None,
) -> GameSettingsScanResult:
    by_key = {entry.key: entry for entry in GAME_SETTINGS_OPTIONS_REGISTRY}
    overrides = {} if overrides is None else overrides
    return GameSettingsScanResult(
        by_key[key].make_result(
            custom_ship_names
            if key == "custom_ship_names"
            else overrides.get(key, required)
        )
        for key, required in _EXPECTED
    )


class _Audit:
    def __init__(self, result: GameSettingsScanResult) -> None:
        self.result = result
        self.game_settings_snapshot_path: Path | str = Path("unused")

    def scan_game_settings(self) -> GameSettingsScanResult:
        save_game_settings_snapshot(
            self.result,
            path=self.game_settings_snapshot_path,
        )
        return self.result


class _PrerequisiteNavigator(DockInventoryNavigator):
    def __init__(self, path: Path, live_result: GameSettingsScanResult) -> None:
        self.game_settings_snapshot_path = path
        self.live_result = live_result
        self.factory_calls = 0
        self.enter_calls = 0

    def _make_game_settings_scanner(self):
        self.factory_calls += 1
        return _Audit(self.live_result)

    def enter_dock(self) -> bool:
        self.enter_calls += 1
        return True


def test_valid_off_snapshot_is_zero_ui_cache_hit(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    save_game_settings_snapshot(_settings_result(GameSettingState.OFF), path=path)
    navigator = _PrerequisiteNavigator(
        path,
        _settings_result(GameSettingState.UNKNOWN),
    )

    evidence = navigator.check_dock_prerequisite()

    assert evidence.compatible is True
    assert evidence.detected is GameSettingState.OFF
    assert evidence.snapshot_source is GameSettingsSnapshotAccessSource.SNAPSHOT
    assert evidence.cache_status is GameSettingsSnapshotStatus.VALID
    assert navigator.factory_calls == 0


@pytest.mark.parametrize("state", [GameSettingState.ON, GameSettingState.UNKNOWN])
def test_valid_incompatible_snapshot_blocks_before_dock(
    tmp_path: Path,
    state: GameSettingState,
) -> None:
    path = tmp_path / "snapshot.json"
    save_game_settings_snapshot(_settings_result(state), path=path)
    navigator = _PrerequisiteNavigator(path, _settings_result(GameSettingState.OFF))

    with pytest.raises(DockInventoryPrerequisiteError) as caught:
        navigator.run_stage2(lambda _viewport: None)

    assert caught.value.evidence.detected is state
    assert caught.value.evidence.compatible is False
    assert navigator.factory_calls == 0
    assert navigator.enter_calls == 0


def test_missing_snapshot_refreshes_once_and_proceeds(tmp_path: Path) -> None:
    navigator = _PrerequisiteNavigator(
        tmp_path / "snapshot.json",
        _settings_result(GameSettingState.OFF),
    )

    evidence = navigator.check_dock_prerequisite()

    assert evidence.compatible is True
    assert evidence.snapshot_source is GameSettingsSnapshotAccessSource.LIVE_AUDIT
    assert evidence.cache_status is GameSettingsSnapshotStatus.MISSING
    assert navigator.factory_calls == 1


@pytest.mark.parametrize("state", [GameSettingState.ON, GameSettingState.UNKNOWN])
def test_live_refresh_incompatible_result_still_blocks(
    tmp_path: Path,
    state: GameSettingState,
) -> None:
    navigator = _PrerequisiteNavigator(
        tmp_path / "snapshot.json",
        _settings_result(state),
    )

    with pytest.raises(DockInventoryPrerequisiteError) as caught:
        navigator.check_dock_prerequisite()

    assert caught.value.evidence.detected is state
    assert navigator.factory_calls == 1


def test_unrelated_global_requirement_does_not_block_dock(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    save_game_settings_snapshot(
        _settings_result(
            GameSettingState.OFF,
            {"frame_rate": FrameRateValue.FPS_30},
        ),
        path=path,
    )
    navigator = _PrerequisiteNavigator(path, _settings_result(GameSettingState.UNKNOWN))

    evidence = navigator.check_dock_prerequisite()

    assert evidence.compatible is True
    assert navigator.factory_calls == 0


def test_force_refresh_bypasses_valid_cache_once(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    save_game_settings_snapshot(_settings_result(GameSettingState.ON), path=path)
    navigator = _PrerequisiteNavigator(path, _settings_result(GameSettingState.OFF))

    evidence = navigator.check_dock_prerequisite(force_refresh=True)

    assert evidence.detected is GameSettingState.OFF
    assert evidence.snapshot_source is GameSettingsSnapshotAccessSource.LIVE_AUDIT
    assert evidence.cache_status is None
    assert navigator.factory_calls == 1


class _Device:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)


class _SequenceDevice:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.shared = np.zeros((8, 8, 3), dtype=np.uint8)
        self.image = self.shared
        self.screenshot_calls = 0

    def screenshot(self) -> None:
        self.screenshot_calls += 1
        self.shared.fill(self.values.pop(0))
        self.image = self.shared


class _HashGenerator:
    def scan(self, image, cached=False, output=False):
        return [int(image[0, 0, 0])]


class _NavigationNavigator(DockInventoryNavigator):
    def __init__(self, current, *, confirm_dock: bool = True) -> None:
        self.ui_current = current
        self.confirm_dock = confirm_dock
        self.device = _Device()
        self.dock_clicks = 0
        self.ensure_targets = []

    def ui_ensure(self, destination, skip_first_screenshot=True):
        self.ensure_targets.append(destination)
        if self.ui_current is destination:
            return False
        if destination is page_dock:
            self.dock_clicks += 1
            if self.confirm_dock:
                self.ui_current = page_dock
            return True
        raise GamePageUnknownError("unsupported")

    def ui_get_current_page(self, skip_first_screenshot=True):
        return self.ui_current

    def capture_stable_dock_frame(self):
        return self.device.image.copy()


@pytest.mark.parametrize("start_page", [page_main, page_main_white])
def test_main_variants_enter_dock_through_page_graph(start_page) -> None:
    navigator = _NavigationNavigator(start_page)

    assert navigator.enter_dock() is True
    assert navigator.ui_current is page_dock
    assert navigator.ensure_targets == [page_dock]
    assert navigator.dock_clicks == 1


def test_already_dock_does_not_issue_redundant_navigation() -> None:
    navigator = _NavigationNavigator(page_dock)

    assert navigator.enter_dock() is False
    assert navigator.dock_clicks == 0


def test_dock_must_be_confirmed_after_navigation() -> None:
    navigator = _NavigationNavigator(page_main, confirm_dock=False)

    with pytest.raises(GamePageUnknownError, match="не подтверждён"):
        navigator.enter_dock()


def test_unknown_page_failure_is_propagated_without_arbitrary_clicks() -> None:
    class _UnknownNavigation(_NavigationNavigator):
        def ui_ensure(self, destination, skip_first_screenshot=True):
            raise GamePageUnknownError("unknown")

    navigator = _UnknownNavigation(object())

    with pytest.raises(GamePageUnknownError, match="unknown"):
        navigator.enter_dock()


def test_current_and_white_main_both_link_to_canonical_dock_page() -> None:
    assert page_dock in page_main.links
    assert page_dock in page_main_white.links


def test_stabilization_rejects_transition_frame_and_owns_result(monkeypatch) -> None:
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _SequenceDevice([1, 2, 2, 99])

    frame = navigator.capture_stable_dock_frame()
    navigator.device.screenshot()

    assert navigator.device.screenshot_calls == 4
    assert int(frame[0, 0, 0]) == 2


def test_stabilization_timeout_fails_instead_of_accepting_transition(monkeypatch) -> None:
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _SequenceDevice([1, 2, 3])
    navigator.DOCK_STABILITY_MAX_CAPTURES = 3

    with pytest.raises(DockInventoryNavigationError, match="не достиг стабильного"):
        navigator.capture_stable_dock_frame()


def _evidence() -> DockPrerequisiteEvidence:
    return DockPrerequisiteEvidence(
        snapshot_path=Path("config/state/game_settings_snapshot.json"),
        snapshot_source=GameSettingsSnapshotAccessSource.SNAPSHOT,
        cache_status=GameSettingsSnapshotStatus.VALID,
        scanned_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        detected=GameSettingState.OFF,
        required=GameSettingState.OFF,
        compatible=True,
    )


def _traversal_result() -> DockTraversalResult:
    return DockTraversalResult(1, (1.0,), True, True, 0)


class _WorkflowNavigator(DockInventoryNavigator):
    def __init__(self, traversal_error=None, cleanup_error=None) -> None:
        self.traversal_error = traversal_error
        self.cleanup_error = cleanup_error
        self.leave_calls = 0

    def check_dock_prerequisite(self, **kwargs):
        return _evidence()

    def enter_dock(self):
        return True

    def traverse_dock(self, visitor, **kwargs):
        if self.traversal_error is not None:
            raise self.traversal_error
        return _traversal_result()

    def leave_dock(self):
        self.leave_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return True


def test_cleanup_pass_after_traversal_pass() -> None:
    navigator = _WorkflowNavigator()

    result = navigator.run_stage2(lambda _viewport: None)

    assert result == DockInventoryStage2Result(_evidence(), _traversal_result())
    assert navigator.leave_calls == 1


def test_primary_traversal_error_survives_successful_cleanup() -> None:
    primary = RuntimeError("primary")
    navigator = _WorkflowNavigator(traversal_error=primary)

    with pytest.raises(RuntimeError) as caught:
        navigator.run_stage2(lambda _viewport: None)

    assert caught.value is primary
    assert navigator.leave_calls == 1


def test_cleanup_failure_after_success_is_operational_failure() -> None:
    navigator = _WorkflowNavigator(cleanup_error=RuntimeError("cleanup"))

    with pytest.raises(DockInventoryNavigationError) as caught:
        navigator.run_stage2(lambda _viewport: None)

    assert isinstance(caught.value.__cause__, RuntimeError)


def test_cleanup_failure_does_not_mask_primary_failure() -> None:
    primary = RuntimeError("primary")
    navigator = _WorkflowNavigator(
        traversal_error=primary,
        cleanup_error=ValueError("cleanup"),
    )

    with pytest.raises(RuntimeError) as caught:
        navigator.run_stage2(lambda _viewport: None)

    assert caught.value is primary
    assert any("возврат" in note for note in caught.value.__notes__)


def test_stage2_runtime_contains_no_settings_or_dock_display_mutators() -> None:
    source = inspect.getsource(DockInventoryNavigator)
    for forbidden in (
        "enforce_required_game_settings",
        "dock_reset",
        "dock_filter",
        "dock_favourite",
        "dock_sort",
    ):
        assert forbidden not in source
