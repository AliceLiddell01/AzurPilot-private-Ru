"""Единый реестр полей снимка игровых ресурсов."""

RESOURCE_NAME_MAP = {
    "Oil": "oil",
    "Coin": "coin",
    "Gem": "gem",
    "Pt": "pt",
    "Cube": "cube",
    "Core": "core",
    "Medal": "medal",
    "Merit": "merit",
    "GuildCoin": "guild_coin",
    "ActionPoint": "action_point",
    "YellowCoin": "yellow_coin",
    "PurpleCoin": "purple_coin",
}
RESOURCE_FIELDS = tuple(RESOURCE_NAME_MAP.values())

__all__ = ["RESOURCE_FIELDS", "RESOURCE_NAME_MAP"]
