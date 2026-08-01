"""Безопасная одноразовая миграция ``Language`` в deploy.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from deploy.utils import DEPLOY_CONFIG
from module.config.locale import UI_LOCALE


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


@dataclass(frozen=True)
class _LanguageNode:
    key: yaml.ScalarNode
    value: yaml.Node


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
    visited: set[int] = set()

    def visit(current: object) -> None:
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in visited:
                return
            visited.add(identity)

        if isinstance(current, dict):
            for key, child in current.items():
                if key == "Language":
                    found.append(child)
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return found


def _language_nodes(text: str) -> list[_LanguageNode]:
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise DeployLanguageMigrationError(
            "Не удалось разобрать структуру deploy.yaml; исходный файл не изменён."
        ) from exc

    found: list[_LanguageNode] = []
    visited: set[int] = set()

    def visit(node: yaml.Node | None) -> None:
        if node is None:
            return
        identity = id(node)
        if identity in visited:
            return
        visited.add(identity)

        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == "Language":
                    found.append(_LanguageNode(key=key_node, value=value_node))
                visit(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                visit(child)

    visit(root)
    return found


def _newline_style(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"


def _patch_language_node(text: str, node: _LanguageNode) -> str:
    value_node = node.value
    if not isinstance(value_node, yaml.ScalarNode):
        raise DeployLanguageMigrationError(
            "Ключ Language должен содержать простое скалярное значение."
        )
    if value_node.style in ("|", ">"):
        raise DeployLanguageMigrationError(
            "Многострочное значение Language нельзя безопасно заменить точечно."
        )
    if (
        node.key.start_mark.line != value_node.start_mark.line
        or value_node.start_mark.line != value_node.end_mark.line
    ):
        raise DeployLanguageMigrationError(
            "Ключ Language записан в неподдерживаемой многострочной форме."
        )

    key_end = node.key.end_mark.index
    value_start = value_node.start_mark.index
    value_end = value_node.end_mark.index
    if key_end > value_start or ":" not in text[key_end:value_start]:
        raise DeployLanguageMigrationError(
            "Не удалось однозначно определить скалярное значение Language."
        )

    if value_start == value_end:
        return text[:value_start] + f" {UI_LOCALE}" + text[value_end:]
    return text[:value_start] + UI_LOCALE + text[value_end:]


def _patched_text(text: str, parsed: dict[str, Any]) -> tuple[str, object, str]:
    values = _language_values(parsed)
    nodes = _language_nodes(text)
    if len(nodes) > 1:
        raise DeployLanguageMigrationError(
            "Обнаружено несколько узлов Language; исходный файл не изменён."
        )
    if len(nodes) != len(values):
        raise DeployLanguageMigrationError(
            "Структура Language неоднозначна; исходный файл не изменён."
        )

    previous = values[0] if values else None
    if nodes:
        if previous == UI_LOCALE:
            return text, previous, "already_current"
        return _patch_language_node(text, nodes[0]), previous, "replaced"

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
