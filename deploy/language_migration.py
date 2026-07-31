"""Безопасная одноразовая миграция ``Language`` в deploy.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from deploy.utils import DEPLOY_CONFIG
from module.config.locale import UI_LOCALE

_LANGUAGE_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>Language)(?P<spacing>[ \t]*:[ \t]*)(?P<value>[^\r\n#]*?)(?P<comment>[ \t]+#.*)?(?P<eol>\r\n|\n|\r|$)",
    re.MULTILINE,
)


class DeployLanguageMigrationError(RuntimeError):
    """Файл нельзя безопасно мигрировать без риска потери данных."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DeployLanguageMigrationError(f"Обнаружен дублирующийся ключ YAML: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class LanguageMigrationResult:
    path: str
    changed: bool
    previous_value: object
    current_value: str = UI_LOCALE
    reason: str = ""


def _parse_and_validate(text: str) -> dict[str, Any]:
    try:
        parsed = yaml.load(text, Loader=_UniqueKeyLoader)
    except DeployLanguageMigrationError:
        raise
    except yaml.YAMLError as exc:
        raise DeployLanguageMigrationError(
            "Не удалось разобрать deploy.yaml; исходный файл не изменён."
        ) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise DeployLanguageMigrationError(
            "Корневой элемент deploy.yaml должен быть объектом YAML."
        )
    values = _language_values(parsed)
    if len(values) > 1:
        raise DeployLanguageMigrationError(
            "Обнаружено несколько ключей Language; исходный файл не изменён."
        )
    if values and isinstance(values[0], (dict, list, tuple, set)):
        raise DeployLanguageMigrationError(
            "Ключ Language должен содержать одно скалярное значение."
        )
    return parsed


def _language_values(value: object) -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "Language":
                found.append(child)
            found.extend(_language_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_language_values(child))
    return found


def _newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def _patched_text(text: str, parsed: dict[str, Any]) -> tuple[str, object, str]:
    matches = list(_LANGUAGE_LINE_RE.finditer(text))
    if len(matches) > 1:
        raise DeployLanguageMigrationError(
            "Обнаружено несколько строк Language; исходный файл не изменён."
        )

    values = _language_values(parsed)
    previous = values[0] if values else None
    if len(matches) == 1:
        match = matches[0]
        raw_value = match.group("value").strip().strip("'\"")
        if raw_value.lower() == UI_LOCALE.lower() and previous == UI_LOCALE:
            return text, previous, "already_current"
        replacement = (
            f"{match.group('indent')}Language{match.group('spacing')}{UI_LOCALE}"
            f"{match.group('comment') or ''}{match.group('eol')}"
        )
        return text[:match.start()] + replacement + text[match.end():], previous, "replaced"

    if values:
        raise DeployLanguageMigrationError(
            "Ключ Language найден в неоднозначной YAML-структуре; исходный файл не изменён."
        )

    newline = _newline_style(text)
    had_final_newline = text.endswith(("\n", "\r"))
    if not text:
        return f"Language: {UI_LOCALE}", None, "added"
    prefix = text if had_final_newline else text + newline
    suffix = f"Language: {UI_LOCALE}{newline if had_final_newline else ''}"
    return prefix + suffix, None, "added"


def migrate_deploy_language(file: str = DEPLOY_CONFIG) -> LanguageMigrationResult:
    """Явно мигрировать ``Language``; обычное чтение конфигурации это не вызывает."""
    path = Path(file)
    if not path.exists():
        return LanguageMigrationResult(str(path), False, None, reason="missing_file")

    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DeployLanguageMigrationError(
            f"Не удалось прочитать deploy.yaml: {exc}"
        ) from exc

    parsed = _parse_and_validate(text)
    patched, previous, reason = _patched_text(text, parsed)
    if patched == text:
        return LanguageMigrationResult(str(path), False, previous, reason=reason)

    tmp = to_tmp_file(str(path))
    try:
        file_write(tmp, patched)
        replace_tmp(tmp, str(path))
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise DeployLanguageMigrationError(
            f"Не удалось атомарно обновить Language в deploy.yaml: {exc}"
        ) from exc

    return LanguageMigrationResult(str(path), True, previous, reason=reason)
