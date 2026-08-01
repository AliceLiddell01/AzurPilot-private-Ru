"""Persisted monthly state for Operation Siren Data Logger automation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any

from module.config.deep import deep_get, deep_set
from module.config.utils import get_os_next_reset

DATA_LOGGER_NAME = "Operation Siren Data Logger"
DATA_LOGGER_ITEM_NAME = "LoggerUnlockT1"
DATA_LOGGER_INTENT_PATH = "OpsiExplore.OpsiExplore.SpecialRadar"
DATA_LOGGER_STORAGE_PATH = "OpsiExplore.Storage.Storage"
DATA_LOGGER_VALID_UNTIL_KEY = "OperationSirenDataLoggerValidUntil"
DATA_LOGGER_RETRY_PENDING_KEY = "OperationSirenDataLoggerRetryPending"
DATA_LOGGER_RETRY_REASON_KEY = "OperationSirenDataLoggerRetryReason"
DATA_LOGGER_RETRY_CYCLE_KEY = "OperationSirenDataLoggerRetryCycle"


class DataLoggerShopState(Enum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    UNKNOWN = "unknown"


class DataLoggerStorageState(Enum):
    ACTIVATED = "activated"
    ALREADY_ACTIVATED = "already_activated"
    UNKNOWN = "unknown"
    ENTER_TIMEOUT = "enter_timeout"


@dataclass(frozen=True)
class DataLoggerShopResult:
    state: DataLoggerShopState
    reason: str = ""
    purchased: bool = False


def data_logger_cycle_key(next_reset=None) -> str:
    """Return the canonical key for the current Operation Siren month."""
    if next_reset is None:
        next_reset = get_os_next_reset()
    return next_reset.replace(microsecond=0).isoformat(sep=" ")


def data_logger_intent_enabled(config) -> bool:
    return bool(config.cross_get(keys=DATA_LOGGER_INTENT_PATH, default=False))


def data_logger_storage_from_data(data: dict[str, Any]) -> dict[str, Any]:
    storage = deep_get(data, keys=DATA_LOGGER_STORAGE_PATH, default={})
    return storage if isinstance(storage, dict) else {}


def data_logger_is_active_from_data(data: dict[str, Any], next_reset=None) -> bool:
    storage = data_logger_storage_from_data(data)
    return storage.get(DATA_LOGGER_VALID_UNTIL_KEY) == data_logger_cycle_key(next_reset)


def data_logger_is_active(config, next_reset=None) -> bool:
    return data_logger_is_active_from_data(config.data, next_reset=next_reset)


def data_logger_retry_pending(config, next_reset=None) -> bool:
    storage = data_logger_storage_from_data(config.data)
    return bool(storage.get(DATA_LOGGER_RETRY_PENDING_KEY, False)) and (
        storage.get(DATA_LOGGER_RETRY_CYCLE_KEY)
        == data_logger_cycle_key(next_reset)
    )


def _updated_storage(config) -> dict[str, Any]:
    return deepcopy(data_logger_storage_from_data(config.data))


def data_logger_mark_active(config, next_reset=None) -> str:
    cycle_key = data_logger_cycle_key(next_reset)
    storage = _updated_storage(config)
    storage[DATA_LOGGER_VALID_UNTIL_KEY] = cycle_key
    storage.pop(DATA_LOGGER_RETRY_PENDING_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_REASON_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_CYCLE_KEY, None)
    config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)
    return cycle_key


def data_logger_set_retry(config, reason: str) -> None:
    storage = _updated_storage(config)
    storage[DATA_LOGGER_RETRY_PENDING_KEY] = True
    storage[DATA_LOGGER_RETRY_REASON_KEY] = str(reason)
    storage[DATA_LOGGER_RETRY_CYCLE_KEY] = data_logger_cycle_key()
    config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)


def data_logger_clear_retry(config) -> None:
    storage = _updated_storage(config)
    changed = False
    for key in (
        DATA_LOGGER_RETRY_PENDING_KEY,
        DATA_LOGGER_RETRY_REASON_KEY,
        DATA_LOGGER_RETRY_CYCLE_KEY,
    ):
        if key in storage:
            storage.pop(key)
            changed = True
    if changed:
        config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)


def install_data_logger_scheduler_bridge() -> None:
    """Make legacy scheduler checks consume monthly state, not user intent.

    ``AzurLaneConfig.opsi_task_delay`` historically reads the visible
    ``OpsiExplore_SpecialRadar`` attribute.  Keep that persisted value as the
    user's automation intent, but temporarily expose the validated monthly
    state while the scheduler method executes.  The persisted configuration
    and the visible switch are restored unchanged.
    """
    from module.config.config import AzurLaneConfig

    original = AzurLaneConfig.opsi_task_delay
    if getattr(original, "_data_logger_monthly_state_bridge", False):
        return

    @wraps(original)
    def wrapped(config, *args, **kwargs):
        previous = deep_get(
            config.data,
            keys=DATA_LOGGER_INTENT_PATH,
            default=False,
        )
        deep_set(
            config.data,
            keys=DATA_LOGGER_INTENT_PATH,
            value=data_logger_is_active_from_data(config.data),
        )
        try:
            return original(config, *args, **kwargs)
        finally:
            deep_set(
                config.data,
                keys=DATA_LOGGER_INTENT_PATH,
                value=previous,
            )

    wrapped._data_logger_monthly_state_bridge = True
    AzurLaneConfig.opsi_task_delay = wrapped


install_data_logger_scheduler_bridge()
