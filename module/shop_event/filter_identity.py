"""Валидируемый data-registry identity и fallback-правил EventShop filter token."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Pattern

from module.shop_event.selector import FILTER_REGEX

FILTER_IDENTITY_SCHEMA_VERSION = 2
FILTER_IDENTITY_PATH = Path(__file__).with_name("data") / "filter_identity.json"
_ALLOWED_TOP_LEVEL = frozenset({"schema_version", "entries", "rules"})
_ALLOWED_ENTRY_FIELDS = frozenset({"item_type", "item_id", "filter"})
_ALLOWED_RULE_FIELDS = frozenset(
    {
        "id",
        "item_type",
        "rarity",
        "name_regex",
        "source_path_regex",
        "filter",
        "filter_template",
    }
)


class FilterIdentityDataError(ValueError):
    """Некорректные данные реестра identity EventShop."""


@dataclass(frozen=True)
class _FilterRule:
    id: str
    item_type: int | None
    rarity: int | None
    name_regex: Pattern[str] | None
    source_path_regex: Pattern[str] | None
    filter_value: str
    filter_template: str


def _validate_filter_token(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FilterIdentityDataError(f"{context} содержит пустой filter")
    token = value.strip()
    if FILTER_REGEX.fullmatch(token.lower()) is None:
        raise FilterIdentityDataError(
            f"{context} содержит неподдерживаемый filter: {token!r}"
        )
    return token


def _compile_rule_regex(value: Any, *, context: str) -> Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FilterIdentityDataError(f"{context} должен быть непустой строкой")
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as exc:
        raise FilterIdentityDataError(f"Некорректный {context}: {exc}") from exc


def _template_fields(template: str) -> set[str]:
    try:
        return {
            field_name
            for _, field_name, _, _ in Formatter().parse(template)
            if field_name
        }
    except ValueError as exc:
        raise FilterIdentityDataError(
            f"Некорректный filter_template EventShop: {exc}"
        ) from exc


def _validate_registry_data(
    data: Any,
) -> tuple[dict[tuple[int, int], str], tuple[_FilterRule, ...]]:
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
    rules = data.get("rules")
    if not isinstance(entries, list):
        raise FilterIdentityDataError(
            "Поле entries реестра identity EventShop должно быть списком"
        )
    if not isinstance(rules, list):
        raise FilterIdentityDataError(
            "Поле rules реестра identity EventShop должно быть списком"
        )

    identities: dict[tuple[int, int], str] = {}
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
        if type(item_type) is not int or item_type <= 0:
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит недопустимый item_type"
            )
        if type(item_id) is not int or item_id <= 0:
            raise FilterIdentityDataError(
                f"Запись identity EventShop #{index} содержит недопустимый item_id"
            )
        filter_value = _validate_filter_token(
            raw.get("filter"), context=f"Запись identity EventShop #{index}"
        )
        key = (item_type, item_id)
        if key in identities:
            raise FilterIdentityDataError(
                f"Повторная identity EventShop: item_type={item_type}, item_id={item_id}"
            )
        identities[key] = filter_value

    parsed_rules: list[_FilterRule] = []
    rule_ids: set[str] = set()
    for index, raw in enumerate(rules):
        if not isinstance(raw, Mapping):
            raise FilterIdentityDataError(
                f"Fallback-правило EventShop #{index} должно быть объектом"
            )
        unknown_rule = set(raw) - _ALLOWED_RULE_FIELDS
        if unknown_rule:
            raise FilterIdentityDataError(
                f"Fallback-правило EventShop #{index} содержит неизвестные поля: "
                + ", ".join(sorted(map(str, unknown_rule)))
            )
        rule_id = str(raw.get("id") or "").strip()
        if not rule_id or rule_id in rule_ids:
            raise FilterIdentityDataError(
                f"Fallback-правило EventShop #{index} содержит пустой или повторный id"
            )
        rule_ids.add(rule_id)

        item_type = raw.get("item_type")
        rarity = raw.get("rarity")
        if item_type is not None and (type(item_type) is not int or item_type <= 0):
            raise FilterIdentityDataError(
                f"Fallback-правило {rule_id} содержит недопустимый item_type"
            )
        if rarity is not None and (type(rarity) is not int or rarity < 0):
            raise FilterIdentityDataError(
                f"Fallback-правило {rule_id} содержит недопустимый rarity"
            )
        name_regex = _compile_rule_regex(
            raw.get("name_regex"), context=f"name_regex правила {rule_id}"
        )
        source_path_regex = _compile_rule_regex(
            raw.get("source_path_regex"),
            context=f"source_path_regex правила {rule_id}",
        )
        if item_type is None and rarity is None and name_regex is None and source_path_regex is None:
            raise FilterIdentityDataError(
                f"Fallback-правило {rule_id} не содержит условий сопоставления"
            )

        filter_value_raw = raw.get("filter")
        filter_template_raw = raw.get("filter_template")
        if (filter_value_raw is None) == (filter_template_raw is None):
            raise FilterIdentityDataError(
                f"Fallback-правило {rule_id} должно задавать ровно одно из filter/filter_template"
            )
        filter_value = ""
        filter_template = ""
        if filter_value_raw is not None:
            filter_value = _validate_filter_token(
                filter_value_raw, context=f"Fallback-правило {rule_id}"
            )
        else:
            if not isinstance(filter_template_raw, str) or not filter_template_raw.strip():
                raise FilterIdentityDataError(
                    f"Fallback-правило {rule_id} содержит пустой filter_template"
                )
            filter_template = filter_template_raw.strip()
            fields = _template_fields(filter_template)
            available_groups = set()
            if name_regex is not None:
                available_groups.update(name_regex.groupindex)
            if source_path_regex is not None:
                available_groups.update(source_path_regex.groupindex)
            if not fields or not fields.issubset(available_groups):
                raise FilterIdentityDataError(
                    f"Fallback-правило {rule_id} ссылается на отсутствующие regex-группы"
                )
            sample = {field: "1" for field in fields}
            try:
                rendered = filter_template.format(**sample)
            except (KeyError, ValueError) as exc:
                raise FilterIdentityDataError(
                    f"Некорректный filter_template правила {rule_id}: {exc}"
                ) from exc
            _validate_filter_token(
                rendered, context=f"Fallback-правило {rule_id} после подстановки"
            )

        parsed_rules.append(
            _FilterRule(
                id=rule_id,
                item_type=item_type,
                rarity=rarity,
                name_regex=name_regex,
                source_path_regex=source_path_regex,
                filter_value=filter_value,
                filter_template=filter_template,
            )
        )
    return identities, tuple(parsed_rules)


def validate_filter_identity_data(data: Any) -> dict[tuple[int, int], str]:
    """Проверить весь registry и вернуть exact identity mapping."""
    identities, _ = _validate_registry_data(data)
    return identities


def _read_filter_identity_data(path: Path | str) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FilterIdentityDataError(
            f"Не удалось загрузить реестр identity EventShop {source}: {exc}"
        ) from exc


def load_filter_identities(
    path: Path | str = FILTER_IDENTITY_PATH,
) -> dict[tuple[int, int], str]:
    identities, _ = _validate_registry_data(_read_filter_identity_data(path))
    return identities


@lru_cache(maxsize=1)
def _default_filter_registry() -> tuple[dict[tuple[int, int], str], tuple[_FilterRule, ...]]:
    return _validate_registry_data(_read_filter_identity_data(FILTER_IDENTITY_PATH))


def runtime_filter_token(
    item_type: int,
    item_id: int,
    *,
    name: str = "",
    rarity: int | None = None,
    source_path: str = "",
) -> str:
    """Вернуть filter token по exact identity или declarative fallback-правилам."""
    identities, rules = _default_filter_registry()
    exact = identities.get((item_type, item_id), "")
    if exact:
        return exact

    for rule in rules:
        if rule.item_type is not None and item_type != rule.item_type:
            continue
        if rule.rarity is not None and rarity != rule.rarity:
            continue
        captures: dict[str, str] = {}
        if rule.name_regex is not None:
            match = rule.name_regex.search(name)
            if match is None:
                continue
            captures.update(
                {key: value for key, value in match.groupdict().items() if value is not None}
            )
        if rule.source_path_regex is not None:
            match = rule.source_path_regex.search(source_path)
            if match is None:
                continue
            captures.update(
                {key: value for key, value in match.groupdict().items() if value is not None}
            )
        token = rule.filter_value
        if rule.filter_template:
            try:
                token = rule.filter_template.format(**captures)
            except (KeyError, ValueError):
                continue
        if FILTER_REGEX.fullmatch(token.lower()) is not None:
            return token
    return ""
