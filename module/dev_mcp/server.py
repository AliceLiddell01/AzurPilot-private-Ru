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
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES, DevMcpAdapter, DevMcpResponse
from module.dev_mcp.contract import DEV_MCP_API_VERSION, DEV_MCP_REQUIRED_SCOPE
from module.dev_runtime.smoke import SmokeSpec

SERVER_NAME = "azurpilot-dev"
SERVER_VERSION = str(DEV_MCP_API_VERSION)
DEV_MCP_COMMAND = "uv"
DEV_MCP_ARGS = ("run", "--locked", "--no-sync", "python", "-m", "module.dev_mcp")
_NO_ARGUMENT_TOOLS = frozenset(
    {
        "dev_preflight",
        "dev_doctor",
        "dev_get_contract",
        "dev_list_tasks",
        "dev_status",
        "dev_cleanup",
        "dev_recover",
        "dev_get_screenshot",
        "dev_list_smoke_capabilities",
        "dev_get_runtime_status",
        "dev_start_game",
        "dev_stop_game",
        "dev_restart_game",
        "dev_start_emulator",
        "dev_stop_emulator",
        "dev_restart_emulator",
        "dev_restart_adb",
        "dev_list_game_observation_capabilities",
        "dev_list_database_checks",
        "dev_list_database_repairs",
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
_SMOKE_INPUT = SmokeSpec.model_json_schema()
_SMOKE_ID_INPUT = {
    "type": "object",
    "properties": {
        "smoke_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        }
    },
    "required": ["smoke_id"],
    "additionalProperties": False,
}
_SMOKE_EVALUATION_INPUT = {
    "type": "object",
    "properties": {
        "smoke_id": _SMOKE_ID_INPUT["properties"]["smoke_id"],
        "assertion_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 1024},
    },
    "required": ["smoke_id", "assertion_id", "verdict", "rationale"],
    "additionalProperties": False,
}
_GAME_OBSERVATION_INPUT = {
    "type": "object",
    "properties": {
        "session_id": _SESSION_ID,
        "capability_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
        "parameters": {
            "type": "object",
            "maxProperties": 16,
            "patternProperties": {
                r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$": {
                    "type": ["string", "integer", "number", "boolean", "array", "object", "null"],
                },
            },
            "additionalProperties": False,
        },
    },
    "required": ["capability_id"],
    "additionalProperties": False,
}
_SMOKE_CHECKPOINT_INPUT = {
    "type": "object",
    "properties": {
        "smoke_id": _SMOKE_ID_INPUT["properties"]["smoke_id"],
        "checkpoint_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
            "not": {"enum": ["before", "final"]},
        },
    },
    "required": ["smoke_id", "checkpoint_id"],
    "additionalProperties": False,
}
_SMOKE_OBSERVATIONS_INPUT = {
    "type": "object",
    "properties": {
        "smoke_id": _SMOKE_ID_INPUT["properties"]["smoke_id"],
        "checkpoint_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
    },
    "required": ["smoke_id"],
    "additionalProperties": False,
}
_DATABASE_CHECK_INPUT = {
    "type": "object",
    "properties": {
        "session_id": _SESSION_ID,
        "check_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
    },
    "required": ["check_id"],
    "additionalProperties": False,
}
_DATABASE_REPAIR_INPUT = {
    "type": "object",
    "properties": {
        "session_id": _SESSION_ID,
        "repair_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
    },
    "required": ["repair_id"],
    "additionalProperties": False,
}
_CONTROL_ID_INPUT = {
    "type": "object",
    "properties": {
        "control_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": r"^[a-f0-9]{32}$",
        }
    },
    "required": ["control_id"],
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
_CONTROL_START = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_CONTROL_STOP = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_CONTROL_RESTART = ToolAnnotations(
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
        _meta={"securitySchemes": [{"type": "oauth2", "scopes": [DEV_MCP_REQUIRED_SCOPE]}]},
    )


def tool_definitions() -> list[Tool]:
    """Вернуть явный и стабильный публичный набор инструментов."""

    descriptions = {
        "dev_preflight": "Предварительная проверка только для чтения назначенного development target.",
        "dev_doctor": "Диагностика только для чтения назначенного development target.",
        "dev_get_contract": "Получить стабильный read-only контракт совместимости AzurPilot Dev MCP.",
        "dev_list_tasks": "Динамический каталог планируемых задач development target только для чтения.",
        "dev_plan_session": "Сформировать план задач только для чтения для development target.",
        "dev_start_session": "Запустить DevSession с учётом задач только для выбранных задач development target.",
        "dev_status": "Статус DevSession и политики задач development target только для чтения.",
        "dev_stop_session": (
            "Остановить DevSession; preserve_task_state=true оставляет состояние "
            "планировщика и требует последующей очистки."
        ),
        "dev_cleanup": "Очистить состояние планировщика development target без запуска новой сессии.",
        "dev_recover": "Выполнить существующее безопасное восстановление development target с проверкой владения.",
        "dev_get_evidence": "Получить ограниченную сводку диагностики указанной DevSession.",
        "dev_get_timeline": "Получить ограниченную каноническую хронологию выполнения указанной DevSession.",
        "dev_get_logs": "Получить ограниченный журнал указанной DevSession только в пределах её сессии.",
        "dev_get_screenshot": "Получить текущий кадр активной DevSession как вложение изображения MCP.",
        "dev_list_smoke_capabilities": "Получить реестр поддерживаемых возможностей SmokeSpec только для чтения.",
        "dev_validate_smoke": "Проверить строгий SmokeSpec и предварительные условия без создания SmokeRun.",
        "dev_start_smoke": "Создать замороженный SmokeRun и быстро передать длительное выполнение независимому supervisor.",
        "dev_get_smoke": "Получить ограниченные состояние, ход выполнения, утверждения и сводку целостности SmokeRun.",
        "dev_cancel_smoke": "Сохранить проверенный запрос отмены для конкретного SmokeRun и его supervisor.",
        "dev_get_smoke_evaluation": "Получить замороженную визуальную рубрику и точный сохранённый снимок экрана для внешней оценки.",
        "dev_submit_smoke_evaluation": "Добавить один неизменяемый внешний вердикт к ожидающему SmokeRun.",
        "dev_list_game_observation_capabilities": "Получить каталог game observation capabilities, привязанных к target, только для чтения.",
        "dev_get_game_observation": "Получить ограниченное типизированное observation назначенного game target через application bridge.",
        "dev_capture_smoke_game_checkpoint": "Сохранить объявленный промежуточный game checkpoint конкретного SmokeRun.",
        "dev_get_smoke_game_observations": "Получить ограниченные game observations конкретного SmokeRun и проверить их полноту.",
        "dev_get_database_status": "Получить сводку фиксированной developer-only диагностики PostgreSQL.",
        "dev_list_database_checks": "Получить каталог разрешённых read-only проверок PostgreSQL.",
        "dev_run_database_check": "Запустить одну разрешённую read-only проверку PostgreSQL.",
        "dev_list_database_repairs": "Получить каталог зарегистрированных безопасных восстановлений базы данных.",
        "dev_preview_database_repair": "Проверить наличие зарегистрированного восстановления базы данных без выполнения.",
        "dev_get_runtime_status": "Получить bounded read-only состояние эмулятора, ADB и приложения без создания full Device.",
        "dev_start_game": "Асинхронно запустить приложение через существующий AppControl backend.",
        "dev_stop_game": "Асинхронно остановить приложение через существующий AppControl backend.",
        "dev_restart_game": "Асинхронно перезапустить приложение через существующий AppControl backend.",
        "dev_start_emulator": "Асинхронно запустить эмулятор через существующую Platform abstraction.",
        "dev_stop_emulator": "Асинхронно остановить эмулятор через существующую Platform abstraction.",
        "dev_restart_emulator": "Асинхронно перезапустить эмулятор через существующую Platform abstraction.",
        "dev_restart_adb": "Асинхронно перезапустить ADB только при подтверждённом отсутствии неизвестных устройств.",
        "dev_get_control_operation": "Получить bounded состояние persistent runtime control operation.",
    }
    schemas = {
        **{name: _EMPTY_INPUT for name in _NO_ARGUMENT_TOOLS},
        "dev_plan_session": _TASK_INPUT,
        "dev_start_session": _TASK_INPUT,
        "dev_stop_session": _STOP_INPUT,
        "dev_get_evidence": _SESSION_INPUT,
        "dev_get_timeline": _TIMELINE_INPUT,
        "dev_get_logs": _LOGS_INPUT,
        "dev_validate_smoke": _SMOKE_INPUT,
        "dev_start_smoke": _SMOKE_INPUT,
        "dev_get_smoke": _SMOKE_ID_INPUT,
        "dev_cancel_smoke": _SMOKE_ID_INPUT,
        "dev_get_smoke_evaluation": _SMOKE_ID_INPUT,
        "dev_submit_smoke_evaluation": _SMOKE_EVALUATION_INPUT,
        "dev_get_game_observation": _GAME_OBSERVATION_INPUT,
        "dev_capture_smoke_game_checkpoint": _SMOKE_CHECKPOINT_INPUT,
        "dev_get_smoke_game_observations": _SMOKE_OBSERVATIONS_INPUT,
        "dev_get_database_status": _SESSION_INPUT,
        "dev_run_database_check": _DATABASE_CHECK_INPUT,
        "dev_preview_database_repair": _DATABASE_REPAIR_INPUT,
        "dev_get_control_operation": _CONTROL_ID_INPUT,
    }
    mutating = {"dev_start_session", "dev_stop_session", "dev_cleanup", "dev_recover", "dev_cancel_smoke", "dev_start_smoke"}
    additive = {"dev_get_evidence", "dev_get_logs", "dev_get_screenshot", "dev_submit_smoke_evaluation", "dev_capture_smoke_game_checkpoint"}
    control_annotations = {
        "dev_start_game": _CONTROL_START,
        "dev_start_emulator": _CONTROL_START,
        "dev_stop_game": _CONTROL_STOP,
        "dev_stop_emulator": _CONTROL_STOP,
        "dev_restart_game": _CONTROL_RESTART,
        "dev_restart_emulator": _CONTROL_RESTART,
        "dev_restart_adb": _CONTROL_RESTART,
    }
    return [
        _tool(
            name,
            descriptions[name],
            schemas[name],
            control_annotations.get(name, _MUTATING if name in mutating else _ADDITIVE if name in additive else _READ_ONLY),
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


def create_server(
    adapter: DevMcpAdapter | None = None,
    *,
    abandon_on_cancel: bool = False,
) -> Server:
    """Создать низкоуровневый сервер MCP без побочных эффектов выполнения.

    Remote transport передаёт `abandon_on_cancel=True`, чтобы его HTTP deadline
    не зависел от блокирующего adapter call. Если такой вызов уже начал
    mutating operation, worker может завершить её после ответа `504`; клиент не
    должен повторять неопределённый вызов и обязан сначала перечитать state.
    Local stdio сохраняет ожидание worker по умолчанию.
    """

    bound_adapter = adapter if adapter is not None else DevMcpAdapter()

    async def handle_list_tools(_context: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=tool_definitions())

    async def handle_call_tool(
        _context: Any,
        params: CallToolRequestParams,
    ) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        response = await anyio.to_thread.run_sync(
            bound_adapter.call,
            name,
            arguments,
            abandon_on_cancel=abandon_on_cancel,
        )
        if not isinstance(response, DevMcpResponse):
            safe = response if isinstance(response, dict) else {"ok": False, "code": "DEV_MCP_INVALID_RESULT", "message": "Некорректный ответ Dev MCP", "state": "failed", "session_id": None, "details": {}}
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(safe, ensure_ascii=False, separators=(",", ":")))],
                structuredContent=safe,
                isError=not bool(safe.get("ok")),
            )
        return _screenshot_call_result(response)

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


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
    "DEV_MCP_REQUIRED_SCOPE",
    "SERVER_NAME",
    "SERVER_VERSION",
    "create_server",
    "main",
    "run_server",
    "tool_definitions",
]
