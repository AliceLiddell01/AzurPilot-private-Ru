"""UI-capable граница сканирования игровых настроек."""

from abc import ABC, abstractmethod
from pathlib import Path

from module.exception import GamePageUnknownError, RequestHumanTakeover
from module.game_settings.model import GameSettingsScanResult
from module.game_settings.navigation import page_settings, page_settings_options
from module.game_settings.snapshot import (
    DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH,
    GameSettingsSnapshot,
    GameSettingsSnapshotSource,
    invalidate_game_settings_snapshot,
    is_current_game_settings_scan_result,
    save_game_settings_snapshot,
)
from module.game_settings.traversal import OptionsTraversalMixin
from module.ui.page import page_main, page_main_white
from module.ui.ui import UI


class GameSettingsScanner(OptionsTraversalMixin, UI, ABC):
    """UI-capable граница для общих сканеров игровых настроек.

    Consumer-модули должны зависеть от этой подсистемы, а не наоборот.
    """

    game_settings_snapshot_path: Path | str = DEFAULT_GAME_SETTINGS_SNAPSHOT_PATH

    def scan_game_settings(self) -> GameSettingsScanResult:
        """Запустить complete audit и сохранить только production snapshot."""
        result = self._scan_game_settings()
        if self._should_persist_game_settings_snapshot(result):
            self.persist_game_settings_snapshot(
                result,
                source=GameSettingsSnapshotSource.AUDIT,
            )
        return result

    def _should_persist_game_settings_snapshot(
        self,
        result: GameSettingsScanResult,
    ) -> bool:
        """Не сохранять test/custom registries как production cache."""
        return is_current_game_settings_scan_result(result)

    def persist_game_settings_snapshot(
        self,
        result: GameSettingsScanResult,
        *,
        source: GameSettingsSnapshotSource = GameSettingsSnapshotSource.AUDIT,
    ) -> GameSettingsSnapshot:
        return save_game_settings_snapshot(
            result,
            path=self.game_settings_snapshot_path,
            source=source,
        )

    def invalidate_game_settings_snapshot(self) -> None:
        invalidate_game_settings_snapshot(self.game_settings_snapshot_path)

    def _capture_options_frame(self):
        """Expose the detached traversal snapshot as the callback device image.

        ``OptionsTraversalMixin`` deliberately copies every screenshot so a
        backend may safely reuse or overwrite its numpy buffer.  The scanner
        mirrors that detached copy back to ``device.image`` so visitors and
        semantic landmark detection consume the exact same object and can
        share the identity-keyed OCR cache.
        """

        frame = super()._capture_options_frame()
        self.device.image = frame
        return frame

    def ensure_options_page(self) -> bool:
        """Гарантировать открытый Options через штатный ``Page/UI``-граф.

        Возвращает ``False``, если Options уже открыт, иначе ``True`` после
        подтверждённого перехода. Реально неизвестные экраны намеренно остаются
        в штатном recovery-контуре ``UI.ui_get_current_page()``. Распознанные,
        но не входящие в контракт Stage 2 страницы сканер не пытается расширять
        собственным recovery engine.
        """
        self.ui_get_current_page()

        if self.ui_current is page_settings_options:
            return False

        if self.ui_current is page_main:
            raise RequestHumanTakeover(
                "[Game Settings] Вход в Settings из legacy Main UI не подтверждён "
                "реальным asset; поддерживается текущий new/white Main UI."
            )

        if self.ui_current is not page_main_white and self.ui_current is not page_settings:
            raise GamePageUnknownError(
                "[Game Settings] Распознанная стартовая страница не входит в "
                "поддерживаемые Stage 2 состояния Main, Settings или Options."
            )

        self.ui_goto(page_settings_options, skip_first_screenshot=True)
        self.ui_get_current_page(skip_first_screenshot=True)
        if self.ui_current is not page_settings_options:
            raise GamePageUnknownError(
                "[Game Settings] Options не подтверждён после навигации."
            )
        return True

    def return_to_main(self) -> bool:
        """Вернуться на Main и подтвердить целевую страницу распознаванием."""
        changed = self.ui_goto_main()
        self.ui_get_current_page(skip_first_screenshot=True)
        if self.ui_current is not page_main and self.ui_current is not page_main_white:
            raise GamePageUnknownError(
                "[Game Settings] Main не подтверждён после возврата из Settings/Options."
            )
        return changed

    @abstractmethod
    def _scan_game_settings(self) -> GameSettingsScanResult:
        """Вернуть результат, заполненный конкретной реализацией scanner-а."""
        raise NotImplementedError
