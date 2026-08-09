"""Архитектурная граница общей подсистемы Game Settings Scanner.

Stage 1 фиксирует только место подсистемы и публичную точку входа. Конкретная
навигация по Settings/Options, распознавание переключателей и модели отдельных
настроек добавляются следующими этапами.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from module.ui.ui import UI


_ScanResultT = TypeVar("_ScanResultT")


class GameSettingsScanner(UI, ABC, Generic[_ScanResultT]):
    """UI-capable граница для общих сканеров игровых настроек.

    Consumer-модули должны зависеть от этой подсистемы, а не наоборот. Базовый
    класс намеренно не знает о Dock Scanner, конкретных игровых настройках,
    координатах, assets или persistence.
    """

    def scan_game_settings(self) -> _ScanResultT:
        """Запустить реализацию сканирования через стабильный public entry point."""
        return self._scan_game_settings()

    @abstractmethod
    def _scan_game_settings(self) -> _ScanResultT:
        """Реализовать конкретное сканирование на следующем этапе."""
        raise NotImplementedError
