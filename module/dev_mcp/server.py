"""Canonical local stdio server для AzurPilot Dev MCP."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, ToolAnnotations

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES, DevMcpAdapter

SERVER_NAME = "azurpilot-dev"
SERVER_VERSION = "1"
DEV_MCP_COMMAND = "uv"
DEV_MCP_ARGS = ("run", "--locked", "--no-sync", "python", "-m", "module.dev_mcp")
_NO_ARGUMENT_TOOLS = frozenset(
    {
        "dev_preflight",
        "dev_doctor",
        "dev_list_tasks",
        "dev_status",
        "dev_cleanup",
        "dev_recover",
    }
)

_EMPTY_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}
_TASK_INPUT = {
    "type": "object",
    "properties": {
        "root_tasks": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "excluded_tasks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_tasks"],
    "additionalProperties": False,
}
_STOP_INPUT = {
    "type": "object",
    "properties": {"preserve_task_state": {"type": "boolean"}},
    "additionalProperties": False,
}
_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "code": {"type": "string"},
        "message": {"type": "string"},
        "state": {"type": "string"},
        "session_id": {"type": ["string", "null"]},
        "details": {"type": "object"},
    },
    "required": ["ok", "code", "message", "state", "session_id", "details"],
    "additionalProperties": False,
}
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def _tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    annotations: ToolAnnotations,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=_OUTPUT,
        annotations=annotations,
    )


def tool_definitions() -> list[Tool]:
    """Вернуть явный и стабильный публичный набор tools."""

    descriptions = {
        "dev_preflight": "Read-only preflight фиксированного Dev Runtime профиля ap.",
        "dev_doctor": "Read-only диагностика фиксированного Dev Runtime профиля ap.",
        "dev_list_tasks": "Read-only динамический каталог schedulable tasks профиля ap.",
        "dev_plan_session": "Сформировать read-only task-aware план для профиля ap.",
        "dev_start_session": "Запустить task-aware DevSession только для выбранных tasks профиля ap.",
        "dev_status": "Read-only статус DevSession и task policy профиля ap.",
        "dev_stop_session": (
            "Остановить DevSession профиля ap; preserve_task_state=true оставляет scheduler-state "
            "и требует последующего cleanup."
        ),
        "dev_cleanup": "Очистить task-aware scheduler-state профиля ap без запуска новой сессии.",
        "dev_recover": "Выполнить существующее exact-owned безопасное восстановление профиля ap.",
    }
    schemas = {
        **{name: _EMPTY_INPUT for name in _NO_ARGUMENT_TOOLS},
        "dev_plan_session": _TASK_INPUT,
        "dev_start_session": _TASK_INPUT,
        "dev_stop_session": _STOP_INPUT,
    }
    mutating = {"dev_start_session", "dev_stop_session", "dev_cleanup", "dev_recover"}
    return [
        _tool(
            name,
            descriptions[name],
            schemas[name],
            _MUTATING if name in mutating else _READ_ONLY,
        )
        for name in DEV_MCP_TOOL_NAMES
    ]


def create_server(adapter: DevMcpAdapter | None = None) -> Server:
    """Создать low-level MCP server без runtime side effects."""

    server = Server(SERVER_NAME, version=SERVER_VERSION)
    bound_adapter = adapter if adapter is not None else DevMcpAdapter()

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, object]:
        return await anyio.to_thread.run_sync(bound_adapter.call, name, arguments)

    return server


async def run_server(adapter: DevMcpAdapter | None = None) -> None:
    """Запустить единственный transport — локальный stdio."""

    server = create_server(adapter)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
            ),
        )


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


__all__ = [
    "DEV_MCP_ARGS",
    "DEV_MCP_COMMAND",
    "SERVER_NAME",
    "SERVER_VERSION",
    "create_server",
    "main",
    "run_server",
    "tool_definitions",
]
