"""Определения production-проверок игровых настроек."""

from module.game_settings.model import (
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
)


CUSTOM_SHIP_NAMES = GameSettingDefinition(
    key="custom_ship_names",
    location="options",
)
"""Настройка Settings -> Options -> Game Settings -> Custom Ship Names."""

CUSTOM_SHIP_NAMES_REQUIRED_OFF = GameSettingRequirement(
    definition=CUSTOM_SHIP_NAMES,
    expected_state=GameSettingState.OFF,
)
"""Preflight-требование: Custom Ship Names должен быть выключен."""
