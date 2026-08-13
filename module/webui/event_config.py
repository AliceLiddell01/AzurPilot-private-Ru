"""Reliable serialized writes for Event-owned runtime configuration fields."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Collection, Mapping, MutableMapping, TypeVar

from module.config.deep import deep_get, deep_set


_EVENT_CONFIG_WRITE_LOCK = RLock()
_Result = TypeVar("_Result")
_Verification = bool | str | Collection[str]


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
    verify: Callable[[Mapping[str, Any], _Result], _Verification] | None = None,
) -> _Result:
    """Apply one Event read-modify-write operation with best-effort rollback."""
    with _EVENT_CONFIG_WRITE_LOCK:
        config = config_updater.read_file(config_name)
        if not isinstance(config, MutableMapping):
            raise ValueError("Корневой элемент конфигурации должен быть объектом.")

        original = deepcopy(config)
        result = mutation(config)
        try:
            config_updater.write_file(config_name, config)

            if verify is not None:
                written = config_updater.read_file(config_name)
                verification = (
                    verify(written, result) if isinstance(written, Mapping) else False
                )
                if verification is not True:
                    if isinstance(verification, str):
                        detail = verification
                    elif isinstance(verification, Collection) and not isinstance(
                        verification, (str, bytes)
                    ):
                        detail = ", ".join(str(item) for item in verification)
                    else:
                        detail = "неизвестные поля"
                    raise OSError(
                        "Проверка записанной конфигурации ивента не пройдена: "
                        f"{detail}."
                    )
        except Exception as write_exc:
            try:
                config_updater.write_file(config_name, original)
            except Exception as rollback_exc:
                raise OSError(
                    "Не удалось сохранить конфигурацию ивента и восстановить "
                    f"исходное состояние: {rollback_exc}"
                ) from write_exc
            raise
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

    def verify(config: Mapping[str, Any], _result: None) -> bool | tuple[str, ...]:
        mismatches = tuple(
            key
            for key, value in expected.items()
            if not _config_values_match(deep_get(config, key), value)
        )
        return mismatches or True

    mutate_event_config(config_updater, config_name, apply, verify=verify)
