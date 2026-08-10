"""WebUI-only visibility policy for local configuration instances."""

from collections.abc import Iterable


WEBUI_HIDDEN_INSTANCE_NAMES = frozenset({"ap", "game_settings_snapshot"})


def is_webui_hidden_instance(name: str) -> bool:
    """Return whether an internal config name must stay outside user-facing WebUI."""
    return name in WEBUI_HIDDEN_INSTANCE_NAMES


def visible_webui_instances(instances: Iterable[str]) -> list[str]:
    """Filter internal smoke/legacy-state configs from user-facing instance lists."""
    return [name for name in instances if not is_webui_hidden_instance(name)]
