"""Каноническое представление payload для идемпотентных операций."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from uuid import UUID


def normalize_payload(value: object) -> object:
    """Нормализовать поддерживаемые доменные значения без зависимости от repr."""

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return normalize_payload(value.value)
    if isinstance(value, dict):
        return {str(key): normalize_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize_payload(item) for item in value]
    return value


def payload_digest(value: object) -> str:
    """Вернуть SHA-256 единого канонического JSON-представления."""

    encoded = json.dumps(
        normalize_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = ["normalize_payload", "payload_digest"]
