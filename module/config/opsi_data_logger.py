"""Persisted monthly state for Operation Siren Data Logger automation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from module.config.deep import deep_get
from module.config.time_source import now as current_time
from module.config.utils import get_os_next_reset, server_timezone

DATA_LOGGER_NAME = "Operation Siren Data Logger"
DATA_LOGGER_ITEM_NAME = "LoggerUnlockT1"
DATA_LOGGER_INTENT_PATH = "OpsiExplore.OpsiExplore.SpecialRadar"
DATA_LOGGER_STORAGE_PATH = "OpsiExplore.Storage.Storage"
DATA_LOGGER_CYCLE_KEY = "OperationSirenDataLoggerCycle"
# Legacy key written by early versions of this branch. New writes use
# DATA_LOGGER_CYCLE_KEY because a local "valid until" timestamp is not stable
# when the host changes UTC offset (DST, timezone or system-time changes).
DATA_LOGGER_VALID_UNTIL_KEY = "OperationSirenDataLoggerValidUntil"
DATA_LOGGER_RETRY_PENDING_KEY = "OperationSirenDataLoggerRetryPending"
DATA_LOGGER_RETRY_REASON_KEY = "OperationSirenDataLoggerRetryReason"
DATA_LOGGER_RETRY_CYCLE_KEY = "OperationSirenDataLoggerRetryCycle"
DATA_LOGGER_RETRY_COUNT_KEY = "OperationSirenDataLoggerRetryCount"

# The fifth unresolved attempt is persisted and paused until the next
# Operation Siren monthly reset instead of retrying for the rest of the month.
DATA_LOGGER_MAX_FAILURES_PER_CYCLE = 5


class DataLoggerShopState(Enum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    UNKNOWN = "unknown"


class DataLoggerStorageState(Enum):
    ACTIVATED = "activated"
    ABSENT = "absent"
    # Deprecated compatibility value; the current lifecycle never emits it.
    # Absence alone is not proof that the item was activated.
    ALREADY_ACTIVATED = "already_activated"
    UNKNOWN = "unknown"
    ENTER_TIMEOUT = "enter_timeout"


@dataclass(frozen=True)
class DataLoggerShopResult:
    state: DataLoggerShopState
    reason: str = ""
    purchased: bool = False


def _server_now(value: datetime | None = None) -> datetime:
    """Return a naive datetime in the selected game server's fixed timezone.

    A timezone-aware ``value`` is treated as an absolute instant. A naive value
    is treated as an already converted server-local time, which is useful for
    deterministic tests and migrations.
    """
    if value is not None and value.tzinfo is None:
        return value
    if value is None:
        value = current_time(timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return (value + server_timezone()).replace(tzinfo=None)


def data_logger_cycle_key(
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> str:
    """Return a stable identifier for the current Operation Siren month.

    The canonical identity is the calendar month on the game server, not a
    local reset timestamp. This keeps the value stable across DST and local
    timezone changes.

    ``next_reset`` is accepted for compatibility with existing callers/tests.
    It represents the next monthly boundary, so the preceding calendar month
    is returned. Production code should normally omit it.
    """
    if server_now is not None:
        value = _server_now(server_now)
    elif next_reset is not None:
        value = next_reset.replace(day=1) - timedelta(days=1)
    else:
        value = _server_now()
    return f"{value.year:04d}-{value.month:02d}"


def data_logger_intent_enabled(config) -> bool:
    return bool(config.cross_get(keys=DATA_LOGGER_INTENT_PATH, default=False))


def data_logger_storage_from_data(data: dict[str, Any]) -> dict[str, Any]:
    storage = deep_get(data, keys=DATA_LOGGER_STORAGE_PATH, default={})
    return storage if isinstance(storage, dict) else {}


def _legacy_valid_until_matches(storage: dict[str, Any]) -> bool:
    """Recognize the exact legacy local-reset value for a one-way migration."""
    legacy_value = storage.get(DATA_LOGGER_VALID_UNTIL_KEY)
    if not isinstance(legacy_value, str):
        return False
    expected = get_os_next_reset().replace(microsecond=0).isoformat(sep=" ")
    return legacy_value == expected


def data_logger_is_active_from_data(
    data: dict[str, Any],
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> bool:
    storage = data_logger_storage_from_data(data)
    cycle_key = data_logger_cycle_key(next_reset, server_now=server_now)
    if storage.get(DATA_LOGGER_CYCLE_KEY) == cycle_key:
        return True
    return next_reset is None and server_now is None and _legacy_valid_until_matches(storage)


def data_logger_is_active(
    config,
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> bool:
    return data_logger_is_active_from_data(
        config.data,
        next_reset=next_reset,
        server_now=server_now,
    )


def data_logger_retry_pending(
    config,
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> bool:
    storage = data_logger_storage_from_data(config.data)
    return bool(storage.get(DATA_LOGGER_RETRY_PENDING_KEY, False)) and (
        storage.get(DATA_LOGGER_RETRY_CYCLE_KEY)
        == data_logger_cycle_key(next_reset, server_now=server_now)
    )


def data_logger_retry_count(
    config,
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> int:
    """Return the number of unresolved lifecycle attempts in this server cycle."""
    storage = data_logger_storage_from_data(config.data)
    cycle_key = data_logger_cycle_key(next_reset, server_now=server_now)
    if storage.get(DATA_LOGGER_RETRY_CYCLE_KEY) != cycle_key:
        return 0
    try:
        count = int(storage.get(DATA_LOGGER_RETRY_COUNT_KEY, 0))
    except (TypeError, ValueError):
        return 0
    return max(count, 0)


def _updated_storage(config) -> dict[str, Any]:
    return deepcopy(data_logger_storage_from_data(config.data))


def data_logger_mark_active(
    config,
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> str:
    cycle_key = data_logger_cycle_key(next_reset, server_now=server_now)
    storage = _updated_storage(config)
    storage[DATA_LOGGER_CYCLE_KEY] = cycle_key
    storage.pop(DATA_LOGGER_VALID_UNTIL_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_PENDING_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_REASON_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_CYCLE_KEY, None)
    storage.pop(DATA_LOGGER_RETRY_COUNT_KEY, None)
    config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)
    return cycle_key


def data_logger_set_retry(
    config,
    reason: str,
    next_reset: datetime | None = None,
    *,
    server_now: datetime | None = None,
) -> int:
    """Persist one unresolved attempt and return its per-cycle failure count."""
    cycle_key = data_logger_cycle_key(next_reset, server_now=server_now)
    storage = _updated_storage(config)
    if storage.get(DATA_LOGGER_RETRY_CYCLE_KEY) == cycle_key:
        try:
            previous_count = max(
                int(storage.get(DATA_LOGGER_RETRY_COUNT_KEY, 0)),
                0,
            )
        except (TypeError, ValueError):
            previous_count = 0
    else:
        previous_count = 0

    failure_count = previous_count + 1
    storage[DATA_LOGGER_RETRY_PENDING_KEY] = True
    storage[DATA_LOGGER_RETRY_REASON_KEY] = str(reason)
    storage[DATA_LOGGER_RETRY_CYCLE_KEY] = cycle_key
    storage[DATA_LOGGER_RETRY_COUNT_KEY] = failure_count
    config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)
    return failure_count


def data_logger_clear_retry(config) -> None:
    storage = _updated_storage(config)
    changed = False
    for key in (
        DATA_LOGGER_RETRY_PENDING_KEY,
        DATA_LOGGER_RETRY_REASON_KEY,
        DATA_LOGGER_RETRY_CYCLE_KEY,
        DATA_LOGGER_RETRY_COUNT_KEY,
    ):
        if key in storage:
            storage.pop(key)
            changed = True
    if changed:
        config.cross_set(keys=DATA_LOGGER_STORAGE_PATH, value=storage)
