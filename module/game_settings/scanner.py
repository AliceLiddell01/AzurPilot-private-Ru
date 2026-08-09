"""Общая подсистема Game Settings Scanner.

Stage 2 добавляет только reusable-навигацию до Settings/Options и обратно.
Чтение/изменение игровых настроек по-прежнему принадлежит следующим этапам.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from module.exception import GamePageUnknownError, RequestHumanTakeover
from module.game_settings.navigation import page_settings, page_settings_options
from module.ui.page import page_main, page_main_white
from module.ui.ui import UI


_ScanResultT = TypeVar("_ScanResultT")


class GameSettingsScanner(UI, ABC, Generic[_ScanResultT]):
    """UI-capable граница для общих сканеров игровых настроек.

    Consumer-модули должны зависеть от этой подсистемы, а не наоборот.
    """

    def scan_game_settings(self) -> _ScanResultT:
        """Запустить реализацию сканирования через стабильный public entry point."""
        return self._scan_game_settings()

    def ensure_options_page(self) -> bool:
        """Гарантировать открытый Options через штатный ``Page/UI``-граф.

        Возвращает ``False``, если Options уже открыт, иначе ``True`` после
        подтверждённого перехода. Legacy/dark Main пока не имеет подтверждённого
        Settings gear asset и поэтому завершается fail-closed вместо blind click.
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
                "[Game Settings] Навигация Stage 2 поддерживает старт из Main, "
                "Settings или Options."
            )

        self.ui_goto(page_settings_options, skip_first_screenshot=True)
        self.ui_get_current_page(skip_first_screenshot=True)
        if self.ui_current is not page_settings_options:
            raise GamePageUnknownError(
                "[Game Settings] Options не подтверждён после навигации."
            )
        return True

    def return_to_main(self) -> bool:
        """Вернуться из Settings/Options на Main и синхронизировать ``ui_current``."""
        changed = self.ui_goto_main()
        self.ui_get_current_page(skip_first_screenshot=True)
        return changed

    @abstractmethod
    def _scan_game_settings(self) -> _ScanResultT:
        """Реализовать конкретное сканирование на следующем этапе."""
        raise NotImplementedError
