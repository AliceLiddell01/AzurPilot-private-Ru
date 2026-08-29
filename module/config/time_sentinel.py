"""Shared sentinel handling for persisted timestamp placeholders.

AzurPilot historically persists both 2020-01-01 and DEFAULT_TIME (2023-01-01)
as "not set" values in configuration. Runtime and WebUI must compare those
values exactly; year-prefix checks would hide legitimate historical timestamps.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from module.config.constants import DEFAULT_TIME


LEGACY_DEFAULT_TIME = datetime(2020, 1, 1, 0, 0)
DEFAULT_TIME_SENTINELS = frozenset({LEGACY_DEFAULT_TIME, DEFAULT_TIME})
DEFAULT_TIME_TEXT = DEFAULT_TIME.strftime("%Y-%m-%d %H:%M:%S")


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("T", " ")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None, microsecond=0)


def is_default_time(value: Any) -> bool:
    """Return True only for the exact persisted "not set" timestamps."""
    parsed = _coerce_datetime(value)
    return parsed in DEFAULT_TIME_SENTINELS if parsed is not None else False
