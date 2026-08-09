"""Concrete read-only Game Settings preflight scanner."""

from __future__ import annotations

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    GameSettingCheckResult,
    GameSettingsScanResult,
    GameSettingState,
)
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.traversal import OptionsViewport
from module.logger import logger


class GameSettingsPreflightScanner(GameSettingsScanner):
    """Проверить обязательные Game Settings без auto-fix или toggle clicks."""

    def _scan_game_settings(self) -> GameSettingsScanResult:
        detected_state = GameSettingState.UNKNOWN

        def visit(_viewport: OptionsViewport) -> bool:
            nonlocal detected_state

            state = detect_custom_ship_names(self.device.image)
            if state is None:
                return False

            if state is GameSettingState.UNKNOWN:
                logger.warning(
                    "[Игровые настройки] Custom Ship Names найден, "
                    "но состояние пока неоднозначно"
                )
                return False

            detected_state = state
            logger.attr("Custom Ship Names", state.value)
            return True

        self.traverse_options(visit)

        result = GameSettingsScanResult(
            (
                GameSettingCheckResult(
                    definition=CUSTOM_SHIP_NAMES,
                    detected_state=detected_state,
                    requirement=CUSTOM_SHIP_NAMES_REQUIRED_OFF,
                ),
            )
        )
        self.return_to_main()
        return result
