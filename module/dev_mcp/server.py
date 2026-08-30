"""Канонический локальный stdio-сервер для AzurPilot Dev MCP."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any

import anyio
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES, DevMcpAdapter, DevMcpResponse

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
        "dev_get_screenshot",
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
_SESSION_ID = {
    "type": ["string", "null"],
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    "maxLength": 128,
}
_SESSION_INPUT = {
    "type": "object",
    "properties": {"session_id": _SESSION_ID},
    "additionalProperties": False,
}
_TIMELINE_INPUT = {
    "type": "object",
    "properties": {
        "session_id": _SESSION_ID,
        "after_sequence": {"type": "integer", "minimum": 0, "maximum": 10**12},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}
_LOGS_INPUT = {
    "type": "object",
    "properties": {
        "session_id": _SESSION_ID,
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
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
_ADDITIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
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
    """Вернуть явный и стабильный публичный набор инструментов."""

    descriptions = {
        "dev_preflight": "Предварительная проверка только для чтения фиксированного профиля Dev Runtime ap.",
        "dev_doctor": "Диагностика только для чтения фиксированного профиля Dev Runtime ap.",
        "dev_list_tasks": "Динамический каталог планируемых задач профиля ap только для чтения.",
        "dev_plan_session": "Сформировать план задач только для чтения для профиля ap.",
        "dev_start_session": "Запустить DevSession с учётом задач только для выбранных задач профиля ap.",
        "dev_status": "Статус DevSession и политики задач профиля ap только для чтения.",
        "dev_stop_session": (
            "Остановить DevSession профиля ap; preserve_task_state=true оставляет состояние "
            "планировщика и требует последующей очистки."
        ),
        "dev_cleanup": "Очистить состояние планировщика профиля ap без запуска новой сессии.",
        "dev_recover": "Выполнить существующее exact-owned безопасное восстановление профиля ap.",
        "dev_get_evidence": "Получить ограниченную сводку диагностики указанной DevSession.",
        "dev_get_timeline": "Получить ограниченную каноническую хронологию выполнения указанной DevSession.",
        "dev_get_logs": "Получить ограниченный журнал указанной DevSession только в пределах её сессии.",
        "dev_get_screenshot": "Получить текущий кадр активной DevSession как вложение изображения MCP.",
    }
    schemas = {
        **{name: _EMPTY_INPUT for name in _NO_ARGUMENT_TOOLS},
        "dev_plan_session": _TASK_INPUT,
        "dev_start_session": _TASK_INPUT,
        "dev_stop_session": _STOP_INPUT,
        "dev_get_evidence": _SESSION_INPUT,
        "dev_get_timeline": _TIMELINE_INPUT,
        "dev_get_logs": _LOGS_INPUT,
    }
    mutating = {"dev_start_session", "dev_stop_session", "dev_cleanup", "dev_recover"}
    additive = {"dev_get_evidence", "dev_get_logs", "dev_get_screenshot"}
    return [
        _tool(
            name,
            descriptions[name],
            schemas[name],
            _MUTATING if name in mutating else _ADDITIVE if name in additive else _READ_ONLY,
        )
        for name in DEV_MCP_TOOL_NAMES
    ]


def _screenshot_call_result(response: DevMcpResponse) -> CallToolResult:
    """Собрать официальный ответ MCP с вложением изображения и метаданными."""

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
        isError=not bool(response.structured.get("ok")),
    )


def create_server(adapter: DevMcpAdapter | None = None) -> Server:
    """Создать низкоуровневый сервер MCP без побочных эффектов выполнения."""

    server = Server(SERVER_NAME, version=SERVER_VERSION)
    bound_adapter = adapter if adapter is not None else DevMcpAdapter()

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return tool_definitions()

    @server.call_tool()
    async def handle_call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, object] | CallToolResult:
        response = await anyio.to_thread.run_sync(bound_adapter.call, name, arguments)
        if not isinstance(response, DevMcpResponse):
            return response
        return _screenshot_call_result(response)

    return server


async def run_server(adapter: DevMcpAdapter | None = None) -> None:
    """Запустить единственный транспорт — локальный stdio."""

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
