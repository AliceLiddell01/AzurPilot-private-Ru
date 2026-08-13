"""Reliable serialized writes for Event-owned runtime configuration fields."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Callable, Mapping, MutableMapping, TypeVar

from module.config.deep import deep_get, deep_set


_EVENT_CONFIG_WRITE_LOCK = RLock()
_Result = TypeVar("_Result")


def _config_values_match(actual: Any, expected: Any) -> bool:
    """Compare persisted config values after ConfigUpdater type conversion."""
    if actual == expected:
        return True
    if isinstance(actual, datetime) and isinstance(expected, str):
        try:
            return actual == datetime.fromisoformat(expected)
        except ValueError:
            return False
    if isinstance(expected, datetime) and isinstance(actual, str):
        try:
            return expected == datetime.fromisoformat(actual)
        except ValueError:
            return False
    return False


def mutate_event_config(
    config_updater: Any,
    config_name: str,
    mutation: Callable[[MutableMapping[str, Any]], _Result],
    *,
    verify: Callable[[Mapping[str, Any], _Result], bool] | None = None,
) -> _Result:
    """Apply one Event read-modify-write operation and propagate write failures."""
    with _EVENT_CONFIG_WRITE_LOCK:
        config = config_updater.read_file(config_name)
        if not isinstance(config, MutableMapping):
            raise ValueError("Корневой элемент конфигурации должен быть объектом.")

        result = mutation(config)
        config_updater.write_file(config_name, config)

        if verify is not None:
            written = config_updater.read_file(config_name)
            if not isinstance(written, Mapping) or not verify(written, result):
                raise OSError("Проверка записанной конфигурации ивента не пройдена.")
        return result


def update_event_config(
    config_updater: Any,
    config_name: str,
    updates: Mapping[str, Any],
) -> None:
    """Persist exact Event fields without relying on the generic silent-save path."""
    expected = dict(updates)

    def apply(config: MutableMapping[str, Any]) -> None:
        for key, value in expected.items():
            deep_set(config, key, value)

    def verify(config: Mapping[str, Any], _result: None) -> bool:
        return all(
            _config_values_match(deep_get(config, key), value)
            for key, value in expected.items()
        )

    mutate_event_config(config_updater, config_name, apply, verify=verify)
