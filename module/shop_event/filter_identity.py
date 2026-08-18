"""Валидируемый реестр точных identity → EventShop filter token."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

FILTER_IDENTITY_SCHEMA_VERSION = 1
FILTER_IDENTITY_PATH = Path(__file__).with_name("data") / "filter_identity.json"
_ALLOWED_TOP_LEVEL = frozenset({"schema_version", "entries"})
_ALLOWED_ENTRY_FIELDS = frozenset({"item_type", "item_id", "filter"})


class FilterIdentityDataError(ValueError):
    """Некорректные данные реестра identity EventShop."""


def validate_filter_identity_data(data: Any) -> dict[tuple[int, int], str]:
    if not isinstance(data, Mapping):
        raise FilterIdentityDataError("Реестр identity EventShop должен быть объектом")
    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise FilterIdentityDataError(
            "Реестр identity EventShop содержит неизвестные поля: "
            + ", ".join(sorted(map(str, unknown)))
        )
    version = data.get("schema_version")
    if type(version) is not int or version != FILTER_IDENTITY_SCHEMA_VERSION:
        raise FilterIdentityDataError(
            f"Неподдерживаемая версия реестра identity EventShop: {version!r}"
        )
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise FilterIdentityDataError("Поле entries реестра identity EventShop должно быть списком")

    result: dict[tuple[int, int], str] = {}
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} должна быть объектом"
            )
        unknown_entry = set(raw) - _ALLOWED_ENTRY_FIELDS
        if unknown_entry:
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит неизвестные поля: "
                + ", ".join(sorted(map(str, unknown_entry)))
            )
        item_type = raw.get("item_type")
        item_id = raw.get("item_id")
        filter_value = raw.get("filter")
        if type(item_type) is not int or item_type <= 0:
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит недопустимый item_type"
            )
        if type(item_id) is not int or item_id <= 0:
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит недопустимый item_id"
            )
        if not isinstance(filter_value, str) or not filter_value.strip():
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит пустой filter"
            )
        key = (item_type, item_id)
        if key in result:
            raise FilterIdentityDataError(
                f"Повторная identity EventShop: item_type={item_type}, item_id={item_id}"
            )
        result[key] = filter_value.strip()
    return result


def load_filter_identities(
    path: Path | str = FILTER_IDENTITY_PATH,
) -> dict[tuple[int, int], str]:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FilterIdentityDataError(
            f"Не удалось загрузить реестр identity EventShop {source}: {exc}"
        ) from exc
    return validate_filter_identity_data(data)


@lru_cache(maxsize=1)
def _default_filter_identities() -> dict[tuple[int, int], str]:
    return load_filter_identities()


def runtime_filter_token(item_type: int, item_id: int) -> str:
    """Вернуть точный filter token из data-registry или пустую строку."""
    return _default_filter_identities().get((item_type, item_id), "")
