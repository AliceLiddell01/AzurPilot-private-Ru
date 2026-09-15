"""Канонизация MCP-каталогов и transport-neutral contract identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy


def canonical_json(value: object) -> str:
    """Вернуть детерминированное JSON-представление bounded metadata."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    """Вычислить SHA-256 UTF-8 текста."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_dump(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _model_dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_dump(item) for item in value]
    return value


def tool_descriptor(tool: object) -> dict[str, object]:
    """Оставить только публичные поля MCP ``Tool`` для hash identity."""

    dumped = _model_dump(tool)
    if not isinstance(dumped, dict):
        raise TypeError("MCP Tool должен сериализоваться в mapping")
    return deepcopy(dumped)


def tool_catalog_payload(tools: Iterable[object]) -> list[dict[str, object]]:
    """Собрать отсортированный каталог инструментов вместе со схемами."""

    descriptors = [tool_descriptor(tool) for tool in tools]
    names = [descriptor.get("name") for descriptor in descriptors]
    if any(
        not isinstance(name, str) or not name or name != name.strip()
        for name in names
    ) or len(set(names)) != len(names):
        raise ValueError("MCP catalog содержит некорректные или повторные имена")
    return sorted(descriptors, key=lambda descriptor: str(descriptor["name"]))


def tool_catalog_sha256_from_tools(tools: Iterable[object]) -> str:
    """Хешировать имена, input/output schemas, annotations и metadata."""

    return sha256_text(canonical_json(tool_catalog_payload(tools)))


def tool_descriptor_hashes_from_tools(tools: Iterable[object]) -> dict[str, str]:
    """Вернуть hash каждого descriptor для доказательства additive changes."""

    return {
        str(descriptor["name"]): sha256_text(canonical_json(descriptor))
        for descriptor in tool_catalog_payload(tools)
    }


def tool_names_from_tools(tools: Iterable[object]) -> tuple[str, ...]:
    """Вернуть детерминированные имена из каталога."""

    return tuple(
        str(item["name"])
        for item in tool_catalog_payload(tools)
    )


def capability_catalog_sha256(
    payload: Mapping[str, object],
) -> str:
    """Хешировать capability contract без transport/source metadata."""

    fields = {
        key: payload[key]
        for key in (
            "authorization_scopes",
            "feature_flags",
            "capability_families",
            "result_outcomes",
            "result_states",
            "read_only_guarantees",
            "control_guarantees",
        )
        if key in payload
    }
    return sha256_text(canonical_json(fields))


def contract_revision(payload: Mapping[str, object]) -> str:
    """Получить transport-neutral revision публичного backend-контракта."""

    identity = {
        key: payload[key]
        for key in (
            "contract_schema_version",
            "product_family",
            "server_name",
            "server_version",
            "dev_mcp_api_version",
            "game_mcp_api_version",
            "smoke_spec_schema_version",
            "smoke_result_schema_version",
            "tool_count",
            "tool_catalog_sha256",
            "capability_catalog_sha256",
            "authorization_scopes",
            "feature_flags",
            "capability_families",
            "result_outcomes",
            "result_states",
            "read_only_guarantees",
            "control_guarantees",
        )
        if key in payload
    }
    return sha256_text(canonical_json(identity))


__all__ = [
    "canonical_json",
    "capability_catalog_sha256",
    "contract_revision",
    "sha256_text",
    "tool_catalog_payload",
    "tool_catalog_sha256_from_tools",
    "tool_descriptor_hashes_from_tools",
    "tool_descriptor",
    "tool_names_from_tools",
]
