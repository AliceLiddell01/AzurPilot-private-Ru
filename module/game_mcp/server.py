"""Локальный stdio-сервер standalone Game MCP read plane."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from contextlib import redirect_stdout
from typing import Any

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

from module.formation.model import SUPPORTED_SURFACE_FLEET_INDICES
from module.game_mcp.adapter import (
    GAME_MCP_TOOL_NAMES,
    GameMcpAdapter,
    GameMcpResponse,
)
from module.game_mcp.contract import GAME_MCP_API_VERSION

SERVER_NAME = "azurpilot-game"
SERVER_VERSION = str(GAME_MCP_API_VERSION)
GAME_MCP_COMMAND = "uv"
GAME_MCP_ARGS = ("run", "--locked", "--no-sync", "python", "-m", "module.game_mcp")
GAME_MCP_REQUIRED_SCOPE = "azurpilot:game.read"

_PROFILE_PATTERN = r"[^\s./\\:*?\"<>|\x00-\x1f\x7f](?:[^./\\:*?\"<>|\x00-\x1f\x7f]{0,126}[^\s./\\:*?\"<>|\x00-\x1f\x7f])?"
_NO_ARGUMENT_TOOLS = frozenset(
    {"game_get_contract", "game_list_profiles", "game_list_tasks"}
)
_PROFILE_INPUT = {
    "type": "object",
    "properties": {
        "profile": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": _PROFILE_PATTERN,
        }
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_TASK_INPUT = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": _PROFILE_PATTERN,
        }
    },
    "required": ["task"],
    "additionalProperties": False,
}
_FLEET_SELECTION_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "fleet_indices": {
            "type": "array",
            "items": {
                "type": "integer",
                "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
            },
            "minItems": 1,
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "uniqueItems": True,
        },
    },
    "required": ["profile", "fleet_indices"],
    "additionalProperties": False,
}
_CONFIG_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "task": _TASK_INPUT["properties"]["task"],
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_LOG_INPUT = {
    "type": "object",
    "properties": {
        "profile": _PROFILE_INPUT["properties"]["profile"],
        "lines": {"type": "integer", "minimum": 0, "maximum": 200, "default": 50},
    },
    "required": ["profile"],
    "additionalProperties": False,
}
_JSON_VALUE = {
    "type": ["array", "boolean", "integer", "number", "null", "object", "string"]
}
_PROFILE_OUTPUT = {
    "type": "object",
    "properties": {"profile": {"type": "string", "minLength": 1, "maxLength": 128}},
    "required": ["profile"],
    "additionalProperties": False,
}
_TASK_OUTPUT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "display_name": {"type": "string", "maxLength": 4096},
        "help": {"type": "string", "maxLength": 4096},
    },
    "required": ["name", "display_name", "help"],
    "additionalProperties": False,
}
_RESOURCE_OUTPUT = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "maxLength": 4096},
        "label": {"type": "string", "maxLength": 4096},
        "value": _JSON_VALUE,
        "limit": _JSON_VALUE,
        "total": _JSON_VALUE,
        "last_update": _JSON_VALUE,
    },
    "required": ["key", "label", "value"],
    "additionalProperties": False,
}
_SCHEDULER_ENTRY_OUTPUT = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "maxLength": 4096},
        "next_run": {"type": ["string", "null"], "maxLength": 128},
    },
    "required": ["task", "next_run"],
    "additionalProperties": False,
}
_FLEET_SLOT_OUTPUT = {
    "type": "object",
    "properties": {
        "side": {"type": ["string", "null"], "maxLength": 64},
        "position": {"type": "integer", "minimum": 1, "maximum": 3},
        "occupied": {"type": ["boolean", "null"]},
        "identity_status": {"type": ["string", "null"], "maxLength": 64},
        "raw_name_ocr": {"type": "string", "maxLength": 4096},
        "displayed_name": {"type": "string", "maxLength": 4096},
        "canonical_name": {"type": "string", "maxLength": 4096},
        "canonical_identity": {"type": "string", "maxLength": 128},
        "ship_form": {"type": "string", "maxLength": 64},
    },
    "required": ["side", "position", "occupied", "identity_status"],
    "additionalProperties": False,
}
_FLEET_SNAPSHOT_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "complete": {"type": "boolean"},
        "occupied_count": {"type": "integer", "minimum": 0, "maximum": 6},
        "catalog_fingerprint": {"type": "string", "maxLength": 128},
        "slots": {
            "type": "array",
            "items": _FLEET_SLOT_OUTPUT,
            "maxItems": 6,
        },
    },
    "required": [
        "fleet_index",
        "complete",
        "occupied_count",
        "catalog_fingerprint",
        "slots",
    ],
    "additionalProperties": False,
}
_FLEET_OBSERVATION_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 36, "maxLength": 36},
        "run_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "idempotency_key": {"type": "string", "maxLength": 128},
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "observed_at": {"type": "string", "maxLength": 128},
        "snapshot": _FLEET_SNAPSHOT_OUTPUT,
    },
    "required": [
        "id",
        "run_id",
        "idempotency_key",
        "fleet_index",
        "observed_at",
        "snapshot",
    ],
    "additionalProperties": False,
}
_MORALE_RECOVERY_OUTPUT = {
    "type": "object",
    "properties": {
        "recovery_per_hour": {"type": ["string", "null"]},
        "recovery_ceiling": {"type": ["string", "null"]},
        "source": {"type": "string", "maxLength": 4096},
    },
    "required": ["recovery_per_hour", "recovery_ceiling", "source"],
    "additionalProperties": False,
}
_MORALE_SLOT_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "side": {"type": ["string", "null"], "maxLength": 64},
        "position": {"type": "integer", "minimum": 1, "maximum": 3},
        "occupied": {"type": ["boolean", "null"]},
        "identity_status": {"type": ["string", "null"], "maxLength": 64},
        "knowledge": {"type": "string", "maxLength": 64},
        "baseline": {"type": ["string", "null"]},
        "current": {"type": ["string", "null"]},
        "recovery": {
            "oneOf": [_MORALE_RECOVERY_OUTPUT, {"type": "null"}],
        },
        "observed_at": {"type": ["string", "null"], "maxLength": 128},
        "source": {"type": ["string", "null"], "maxLength": 4096},
        "morale_observation_id": {"type": ["string", "null"], "maxLength": 36},
        "location": {"type": ["string", "null"], "maxLength": 64},
        "dorm_scan_id": {"type": ["string", "null"], "maxLength": 36},
        "canonical_identity": {"type": "string", "maxLength": 128},
        "canonical_name": {"type": "string", "maxLength": 4096},
        "ship_form": {"type": "string", "maxLength": 64},
    },
    "required": [
        "fleet_index",
        "side",
        "position",
        "occupied",
        "identity_status",
        "knowledge",
        "baseline",
        "current",
        "recovery",
        "observed_at",
        "source",
        "morale_observation_id",
        "location",
        "dorm_scan_id",
    ],
    "additionalProperties": False,
}
_MORALE_FLEET_OUTPUT = {
    "type": "object",
    "properties": {
        "fleet_index": {
            "type": "integer",
            "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
        },
        "formation_observation_id": {
            "type": ["string", "null"],
            "maxLength": 36,
        },
        "formation_observed_at": {"type": ["string", "null"], "maxLength": 128},
        "slots": {"type": "array", "items": _MORALE_SLOT_OUTPUT, "maxItems": 6},
    },
    "required": [
        "fleet_index",
        "formation_observation_id",
        "formation_observed_at",
        "slots",
    ],
    "additionalProperties": False,
}
_CONTRACT_OUTPUT = {
    "type": "object",
    "properties": {
        "contract_schema_version": {"type": "integer", "const": 1},
        "product_family": {"type": "string", "maxLength": 128},
        "game_mcp_api_version": {"type": "integer", "const": 1},
        "feature_flags": {
            "type": "object",
            "maxProperties": 32,
            "additionalProperties": {"type": "boolean"},
        },
        "capability_families": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
        "result_states": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 64},
        },
        "read_only_guarantees": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 128},
        },
    },
    "required": [
        "contract_schema_version",
        "product_family",
        "game_mcp_api_version",
        "feature_flags",
        "capability_families",
        "result_states",
        "read_only_guarantees",
    ],
    "additionalProperties": False,
}
_DETAILS_OUTPUT = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "maxLength": 128},
        "contract": _CONTRACT_OUTPUT,
        "profiles": {
            "type": "array",
            "maxItems": 256,
            "items": _PROFILE_OUTPUT,
        },
        "tasks": {"type": "array", "maxItems": 512, "items": _TASK_OUTPUT},
        "profile": {"type": "string", "minLength": 1, "maxLength": 128},
        "running": {"type": "boolean"},
        "state": {"type": "string", "maxLength": 64},
        "resources": {
            "type": "array",
            "maxItems": 256,
            "items": _RESOURCE_OUTPUT,
        },
        "task": {"type": ["string", "null"], "maxLength": 4096},
        "entries": {
            "type": "array",
            "maxItems": 512,
            "items": _SCHEDULER_ENTRY_OUTPUT,
        },
        "selection": {
            "type": "array",
            "minItems": 1,
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "uniqueItems": True,
            "items": {
                "type": "integer",
                "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
            },
        },
        "observations": {
            "type": "array",
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "items": _FLEET_OBSERVATION_OUTPUT,
        },
        "missing_fleet_indices": {
            "type": "array",
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "uniqueItems": True,
            "items": {
                "type": "integer",
                "enum": list(SUPPORTED_SURFACE_FLEET_INDICES),
            },
        },
        "coverage_complete": {"type": "boolean"},
        "snapshots_complete": {"type": "boolean"},
        "fleets": {
            "type": "array",
            "maxItems": len(SUPPORTED_SURFACE_FLEET_INDICES),
            "items": _MORALE_FLEET_OUTPUT,
        },
        "lines": {"type": "array", "maxItems": 200, "items": {"type": "string"}},
        "truncated": {"type": "boolean"},
        "config": {
            "type": "object",
            "maxProperties": 256,
            "additionalProperties": _JSON_VALUE,
        },
        "screenshot": {
            "type": "object",
            "properties": {
                "mime": {"type": "string", "enum": ["image/png", "image/jpeg"]},
                "width": {"type": "integer", "minimum": 1, "maximum": 8192},
                "height": {"type": "integer", "minimum": 1, "maximum": 8192},
                "byte_size": {"type": "integer", "minimum": 1, "maximum": 4194304},
                "sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
            },
            "required": ["mime", "width", "height", "byte_size", "sha256"],
            "additionalProperties": False,
        },
    },
    "maxProperties": 32,
    "additionalProperties": False,
}
_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "code": {"type": "string", "maxLength": 128},
        "message": {"type": "string", "maxLength": 4096},
        "state": {"type": "string", "maxLength": 64},
        "details": _DETAILS_OUTPUT,
    },
    "required": ["ok", "code", "message", "state", "details"],
    "additionalProperties": False,
}
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _tool(name: str, description: str, input_schema: dict[str, Any]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=_OUTPUT,
        annotations=_READ_ONLY,
        _meta={
            "securitySchemes": [{"type": "oauth2", "scopes": [GAME_MCP_REQUIRED_SCOPE]}]
        },
    )


def tool_definitions() -> list[Tool]:
    """Вернуть детерминированный и полностью read-only каталог инструментов."""

    descriptions = {
        "game_get_contract": "Получить стабильный контракт AzurPilot Game MCP read plane.",
        "game_list_profiles": "Перечислить канонические профили AzurPilot без путей и секретов.",
        "game_get_profile_status": "Получить статус выбранного профиля AzurPilot.",
        "game_get_resources": "Получить ограниченный снимок игровых ресурсов выбранного профиля.",
        "game_get_current_task": "Получить текущую задачу запущенного профиля.",
        "game_get_scheduler_queue": "Получить read-only очередь scheduler выбранного профиля.",
        "game_list_tasks": "Получить каталог игровых задач и краткую локализованную справку.",
        "game_get_task_help": "Получить bounded metadata и справку одной игровой задачи.",
        "game_get_fleet_state": "Получить сохранённый Fleet State выбранного профиля без физического сканирования.",
        "game_get_morale": "Получить exact, projected или unknown morale выбранного профиля.",
        "game_get_config": "Получить ограниченный и redacted снимок конфигурации профиля.",
        "game_get_recent_logs": "Получить ограниченный и sanitized tail журнала профиля.",
        "game_get_screenshot": "Получить bounded MCP image content выбранного профиля без ввода.",
    }
    schemas = {
        **{
            name: {"type": "object", "properties": {}, "additionalProperties": False}
            for name in _NO_ARGUMENT_TOOLS
        },
        "game_get_profile_status": _PROFILE_INPUT,
        "game_get_resources": _PROFILE_INPUT,
        "game_get_current_task": _PROFILE_INPUT,
        "game_get_scheduler_queue": _PROFILE_INPUT,
        "game_get_task_help": _TASK_INPUT,
        "game_get_fleet_state": _FLEET_SELECTION_INPUT,
        "game_get_morale": _FLEET_SELECTION_INPUT,
        "game_get_config": _CONFIG_INPUT,
        "game_get_recent_logs": _LOG_INPUT,
        "game_get_screenshot": _PROFILE_INPUT,
    }
    return [
        _tool(name, descriptions[name], schemas[name]) for name in GAME_MCP_TOOL_NAMES
    ]


def _text_call_result(response: dict[str, object]) -> CallToolResult:
    text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=response,
        isError=response.get("ok") is not True,
    )


def _screenshot_call_result(response: GameMcpResponse) -> CallToolResult:
    """Собрать официальный MCP response с native image content."""

    if response.image is None or response.mime_type is None:
        return _text_call_result(response.structured)
    image = ImageContent(
        type="image",
        data=base64.b64encode(response.image).decode("ascii"),
        mimeType=response.mime_type,
    )
    summary = TextContent(
        type="text",
        text=json.dumps(response.structured, ensure_ascii=False, separators=(",", ":")),
    )
    return CallToolResult(
        content=[image, summary],
        structuredContent=response.structured,
        isError=response.structured.get("ok") is not True,
    )


def create_server(
    adapter: GameMcpAdapter | None = None,
    *,
    abandon_on_cancel: bool = False,
) -> Server:
    """Создать MCP Server без сборки backend и без подключения к источникам."""

    bound_adapter = adapter if adapter is not None else GameMcpAdapter()

    async def handle_list_tools(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=tool_definitions())

    async def handle_call_tool(
        _context: Any,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        name = params.name
        arguments = params.arguments

        def call_adapter() -> dict[str, object] | GameMcpResponse:
            with redirect_stdout(sys.stderr):
                return bound_adapter.call(name, arguments)

        response = await anyio.to_thread.run_sync(
            call_adapter,
            abandon_on_cancel=abandon_on_cancel,
        )
        if isinstance(response, GameMcpResponse):
            return _screenshot_call_result(response)
        if isinstance(response, dict):
            return _text_call_result(response)
        return _text_call_result(
            {
                "ok": False,
                "code": "GAME_MCP_INTERNAL_ERROR",
                "message": "Game MCP вернул некорректный ответ",
                "state": "failed",
                "details": {},
            }
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def run_server(adapter: GameMcpAdapter | None = None) -> None:
    """Запустить единственный локальный транспорт Game MCP — stdio."""

    bound_adapter = adapter if adapter is not None else GameMcpAdapter()
    server = create_server(bound_adapter)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(
                    notification_options=NotificationOptions(),
                ),
            )
    finally:
        close = getattr(bound_adapter, "close", None)
        if callable(close):
            close()


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        return


__all__ = (
    "GAME_MCP_ARGS",
    "GAME_MCP_COMMAND",
    "GAME_MCP_REQUIRED_SCOPE",
    "SERVER_NAME",
    "SERVER_VERSION",
    "create_server",
    "main",
    "run_server",
    "tool_definitions",
)
