import base64
import json
import logging
import threading
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from module.application import GameControlService, GameReadService
from module.application.errors import ApplicationError, InvalidRequestError
from module.application.game_models import (
    ConfigSnapshot,
    ConfigUpdateRequest,
    DashboardResources,
    LifecycleOutcome,
    MediaFrame,
    ScheduleTaskRequest,
    thaw_payload,
)
from module.application.legacy_adapters import (
    GeneratedTaskCatalogAdapter,
    LegacyInstanceRuntimeAdapter,
)
from module.application.legacy_game_adapters import (
    LegacyAdbAdapter,
    LegacyConfigAdapter,
    LegacyEmulatorAdapter,
    LegacyProcessManagerAdapter,
    LegacyRuntimeLogAdapter,
    LegacyScreenshotAdapter,
    legacy_current_time,
)
from module.application.services import InstanceQueryService, TaskCatalogService
from module.persistence.runtime import bootstrap_runtime_storage

# Инициализация логирования.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("azurpilot-mcp")

ToolResponse = list[TextContent | ImageContent]


class _LegacyGameBackend:
    """Собрать application services только при первом вызове legacy tool."""

    def __init__(self) -> None:
        metadata = GeneratedTaskCatalogAdapter.from_generated_sources()
        instances = LegacyInstanceRuntimeAdapter()
        config = LegacyConfigAdapter(metadata)
        logs = LegacyRuntimeLogAdapter()
        screenshot = LegacyScreenshotAdapter()
        lifecycle = LegacyProcessManagerAdapter()
        emulator = LegacyEmulatorAdapter()
        adb = LegacyAdbAdapter()

        self.instances = InstanceQueryService(instances)
        self.tasks = TaskCatalogService(metadata)
        self.read = GameReadService(
            instance_reader=instances,
            config_reader=config,
            log_reader=logs,
            screenshot_reader=screenshot,
            scheduler_tasks=metadata,
        )
        self.control = GameControlService(
            instance_reader=instances,
            config_schema=metadata,
            config_writer=config,
            scheduler_tasks=metadata,
            lifecycle=lifecycle,
            emulator=emulator,
            adb=adb,
            clock=legacy_current_time,
        )


_backend: _LegacyGameBackend | None = None
_backend_lock = threading.Lock()


def _get_backend() -> _LegacyGameBackend:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = _LegacyGameBackend()
    return _backend


def _invalid_request(message: str) -> InvalidRequestError:
    return InvalidRequestError(message)


def _required_string(arguments: dict[str, Any], key: str) -> str:
    try:
        value = arguments[key]
    except (KeyError, TypeError):
        raise _invalid_request(
            f"Запрос MCP-инструмента не содержит обязательный параметр «{key}»."
        ) from None
    if not isinstance(value, str) or not value.strip():
        raise _invalid_request(
            f"Параметр MCP-инструмента «{key}» должен быть непустой строкой."
        )
    return value


def _config_request(arguments: dict[str, Any]) -> ConfigUpdateRequest:
    try:
        return ConfigUpdateRequest(
            instance=arguments["instance"],
            task=arguments["task"],
            group=arguments["group"],
            argument=arguments["arg"],
            value=arguments["value"],
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid_request("Запрос изменения конфигурации неполный или некорректный.") from None


def _schedule_request(arguments: dict[str, Any]) -> ScheduleTaskRequest:
    try:
        return ScheduleTaskRequest(
            instance=arguments["instance"],
            task=arguments["task"],
        )
    except (KeyError, TypeError, ValueError):
        raise _invalid_request("Запрос планирования задачи неполный или некорректный.") from None


def _resources_payload(resources: DashboardResources) -> dict[str, object]:
    payload: dict[str, object] = {}
    for resource in resources.items:
        item: dict[str, object] = {
            "label": resource.label,
            "value": thaw_payload(resource.value),
        }
        if resource.limit is not None:
            item["limit"] = thaw_payload(resource.limit)
        if resource.total is not None:
            item["total"] = thaw_payload(resource.total)
        if resource.last_update is not None:
            item["last_update"] = thaw_payload(resource.last_update)
        payload[resource.key] = item
    return payload


def _config_payload(snapshot: ConfigSnapshot) -> object:
    return thaw_payload(snapshot.data)


def _mcp_image(frame: MediaFrame) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(frame.data).decode("ascii"),
        mimeType=frame.media_type,
    )

async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_instances",
            description="Перечислить все настроенные экземпляры AzurPilot",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_status",
            description="Получить состояние и подробности state всех экземпляров AzurPilot",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="list_tasks",
            description="Перечислить все имена задач верхнего уровня, например Main и Event",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_task_help",
            description="Получить структуру параметров, локализованное имя и справку указанной задачи",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_name": {"type": "string", "description": "Имя задачи"}
                },
                "required": ["task_name"]
            }
        ),
        Tool(
            name="get_resources",
            description="Получить состояние ресурсов указанного экземпляра: нефть, монеты, самоцветы и другое",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string", "description": "Имя экземпляра"}
                },
                "required": ["instance"]
            }
        ),
        Tool(
            name="get_config",
            description="Получить текущие значения конфигурации указанного экземпляра",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string", "description": "Имя экземпляра"},
                    "task": {"type": "string", "description": "Необязательный фильтр по имени задачи"}
                },
                "required": ["instance"]
            }
        ),
        Tool(
            name="update_config",
            description=(
                "Изменить параметр конфигурации указанного экземпляра. "
                "Формат пути: task.group.arg. Параметры, помеченные "
                "sensitive в generated metadata, скрываются при чтении, а "
                "запись через этот MCP-инструмент запрещена. "
                "Настройте их через WebUI конфигурации экземпляра."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string", "description": "Имя экземпляра"},
                    "task": {"type": "string"},
                    "group": {"type": "string"},
                    "arg": {"type": "string"},
                    "value": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {"type": "object"},
                            {"type": "array"},
                            {"type": "null"}
                        ],
                        "description": "Новое значение параметра"
                    }
                },
                "required": ["instance", "task", "group", "arg", "value"]
            }
        ),
        Tool(
            name="get_recent_logs",
            description="Прочитать последние строки журнала указанного экземпляра; по умолчанию 50 строк",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string"},
                    "lines": {"type": "integer", "default": 50}
                },
                "required": ["instance"]
            }
        ),
        Tool(
            name="start_instance",
            description="Запустить процесс указанного экземпляра AzurPilot",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string"}
                },
                "required": ["instance"]
            }
        ),
        Tool(
            name="stop_instance",
            description="Принудительно остановить работающий экземпляр AzurPilot",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance": {"type": "string"}
                },
                "required": ["instance"]
            }
        ),
        Tool(
            name="get_screenshot",
            description="Получить снимок экрана эмулятора указанного экземпляра в кодировке Base64",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}}, "required": ["instance"]}
        ),
        Tool(
            name="get_current_running_task",
            description="Точно определить подзадачу, выполняемую текущим экземпляром",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}}, "required": ["instance"]}
        ),
        Tool(
            name="get_scheduler_queue",
            description="Получить очередь задач и ожидаемое время их выполнения",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}}, "required": ["instance"]}
        ),
        Tool(
            name="trigger_task",
            description="Немедленно добавить задачу, например Event или Daily, в очередь планировщика",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}, "task": {"type": "string"}}, "required": ["instance", "task"]}
        ),
        Tool(
            name="clear_scheduler_queue",
            description="Очистить текущую очередь; обычно используется при зависании или экстренной остановке всех планов",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}}, "required": ["instance"]}
        ),
        Tool(
            name="restart_emulator",
            description="Перезапустить процесс эмулятора, связанного с указанным экземпляром",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string"}}, "required": ["instance"]}
        ),
        Tool(
            name="restart_adb",
            description="Перезапустить службу ADB для устранения состояния Device Offline",
            inputSchema={"type": "object", "properties": {"instance": {"type": "string", "description": "Необязательно"}}}
        ),
    ]

async def _tool_list_instances(arguments: dict[str, Any]) -> ToolResponse:
    instances = _get_backend().instances.list_instances()
    payload = [item.name for item in instances]
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_status(arguments: dict[str, Any]) -> ToolResponse:
    statuses = _get_backend().instances.list_statuses()
    results = [
        {
            "instance": status.name,
            "running": status.running,
            "state": status.state.value,
        }
        for status in statuses
    ]
    return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2, default=str))]


async def _tool_list_tasks(arguments: dict[str, Any]) -> ToolResponse:
    tasks = [task.name for task in _get_backend().tasks.list_tasks()]
    return [TextContent(type="text", text=json.dumps(tasks, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_task_help(arguments: dict[str, Any]) -> ToolResponse:
    task_name = _required_string(arguments, "task_name")
    task = _get_backend().tasks.get_task_metadata(task_name)
    details = {
        "task_name": task.name,
        "display_name": task.display_name,
        "help": task.help,
        "groups": {
            group.name: {
                "display_name": group.display_name,
                "help": group.help,
                "arguments": {
                    argument.name: {
                        "display_name": argument.display_name,
                        "help": argument.help,
                        "type": argument.input_type,
                        "default": thaw_payload(argument.default),
                        "options": (
                            {
                                option.value: option.display_name
                                for option in argument.options
                            }
                            if argument.options
                            else None
                        ),
                    }
                    for argument in group.arguments
                },
            }
            for group in task.groups
        },
    }
    return [TextContent(type="text", text=json.dumps(details, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_resources(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    resources = _get_backend().read.get_resources(inst)
    res = _resources_payload(resources)
    return [TextContent(type="text", text=json.dumps(res, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_config(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    task = arguments.get("task")
    snapshot = _get_backend().read.get_config(inst, task)
    data = _config_payload(snapshot)
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]


async def _tool_update_config(arguments: dict[str, Any]) -> ToolResponse:
    result = _get_backend().control.update_config(_config_request(arguments))
    return [
        TextContent(
            type="text",
            text=(
                f"Успешно: параметр {result.request.path} обновлён на "
                f"{thaw_payload(result.request.value)}"
            ),
        )
    ]


async def _tool_get_recent_logs(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    lines_count = arguments.get("lines", 50)
    result = _get_backend().read.get_recent_logs(inst, lines_count)
    return [TextContent(type="text", text=result.text)]


async def _tool_start_instance(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().control.start_instance(inst)
    if result.outcome is LifecycleOutcome.ALREADY_RUNNING:
        return [TextContent(type="text", text=f"Ошибка: {result.instance} уже запущен.")]
    return [TextContent(type="text", text=f"Успешно: {result.instance} запущен")]


async def _tool_stop_instance(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().control.stop_instance(inst)
    if result.outcome is LifecycleOutcome.ALREADY_STOPPED:
        return [TextContent(type="text", text=f"Ошибка: {result.instance} не запущен.")]
    return [TextContent(type="text", text=f"Успешно: {result.instance} остановлен")]


async def _tool_get_screenshot(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    frame = _get_backend().read.get_screenshot(inst)
    return [_mcp_image(frame)]


async def _tool_get_current_running_task(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().read.get_current_running_task(inst)
    return [TextContent(type="text", text=result.task)]


async def _tool_get_scheduler_queue(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().read.get_scheduler_queue(inst)
    queue_data = [
        {"task": entry.task, "next_run": str(thaw_payload(entry.next_run))}
        for entry in result.entries
    ]
    return [TextContent(type="text", text=json.dumps(queue_data, ensure_ascii=False, indent=2))]


async def _tool_trigger_task(arguments: dict[str, Any]) -> ToolResponse:
    result = _get_backend().control.trigger_task(_schedule_request(arguments))
    return [TextContent(type="text", text=f"Успешно: задача {result.request.task} запланирована на немедленный запуск.")]


async def _tool_clear_scheduler_queue(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().control.clear_scheduler_queue(inst)
    if not result.cleared_tasks:
        return [TextContent(type="text", text="Успешно: очередь задач уже пуста.")]
    return [TextContent(type="text", text=f"Успешно: задачи очищены: {', '.join(result.cleared_tasks)}")]


async def _tool_restart_emulator(arguments: dict[str, Any]) -> ToolResponse:
    inst = _required_string(arguments, "instance")
    result = _get_backend().control.restart_emulator(inst)
    return [TextContent(type="text", text=f"Успешно: эмулятор {result.instance} перезапущен")]


async def _tool_restart_adb(arguments: dict[str, Any]) -> ToolResponse:
    instance = _required_string(arguments, "instance")
    _get_backend().control.restart_adb(instance)
    return [TextContent(type="text", text="Успешно: сервис ADB перезапущен.")]


TOOL_HANDLERS = {
    "list_instances": _tool_list_instances,
    "get_status": _tool_get_status,
    "list_tasks": _tool_list_tasks,
    "get_task_help": _tool_get_task_help,
    "get_resources": _tool_get_resources,
    "get_config": _tool_get_config,
    "update_config": _tool_update_config,
    "get_recent_logs": _tool_get_recent_logs,
    "start_instance": _tool_start_instance,
    "stop_instance": _tool_stop_instance,
    "get_screenshot": _tool_get_screenshot,
    "get_current_running_task": _tool_get_current_running_task,
    "get_scheduler_queue": _tool_get_scheduler_queue,
    "trigger_task": _tool_trigger_task,
    "clear_scheduler_queue": _tool_clear_scheduler_queue,
    "restart_emulator": _tool_restart_emulator,
    "restart_adb": _tool_restart_adb,
}


async def call_tool(name: str, arguments: dict[str, Any]) -> ToolResponse:
    try:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
        return await handler(arguments)
    except ApplicationError as exc:
        logger.warning(
            "Операция MCP отклонена; инструмент: %s; код: %s",
            name,
            exc.code,
        )
        return [TextContent(type="text", text=_application_error_text(exc))]
    except Exception as exc:  # noqa: BLE001 - legacy tool boundary has safe fallback.
        logger.error(
            "Ошибка инструмента %s; тип исключения: %s",
            name,
            type(exc).__name__,
        )
        return [TextContent(type="text", text="Внутренняя ошибка MCP-инструмента.")]


def _application_error_text(error: ApplicationError) -> str:
    messages = {
        "invalid_request": "Некорректный запрос MCP-инструмента.",
        "not_found": "Запрошенный ресурс не найден.",
        "instance_not_running": "Ошибка: экземпляр не запущен.",
        "configuration_invalid": "Значение конфигурации не прошло проверку.",
        "service_unavailable": "Операция временно недоступна.",
        "operation_failed": "Операция не выполнена.",
    }
    return messages.get(error.code, "Операция отклонена.")


async def _list_tools(_context: Any, _params: Any) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _call_tool(_context: Any, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


# MCP v2 использует явные low-level callbacks; server/discover регистрируется SDK.
mcp_server = Server(
    "AzurPilot-MCP",
    version="2",
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)

# Инициализация SSE-транспорта с фиксированным endpoint, соответствующим /mcp.
transport = SseServerTransport("/mcp/messages")


async def _run_sse(scope, receive, send):
    logger.info("Найден endpoint /sse. Открывается SSE-соединение...")
    async with transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
        logger.info("SSE-поток подключён. Запускается цикл MCP-сервера...")
        try:
            options = mcp_server.create_initialization_options()
            await mcp_server.run(read_stream, write_stream, options)
        except Exception:
            logger.exception("Ошибка цикла MCP-сервера")
        logger.info("Цикл MCP-сервера завершён.")


def _is_mcp_client_disconnected(error: Exception) -> bool:
    return "BrokenResourceError" in str(type(error)) or "BrokenPipeError" in str(error)


async def _handle_mcp_post(scope, receive, send, method):
    logger.info(f"Найден endpoint /messages. Метод: {method}")
    try:
        await transport.handle_post_message(scope, receive, send)
        logger.info("POST-сообщение MCP обработано.")
    except Exception as e:
        # Обработать типичные ошибки отключения клиента, чтобы сервер не падал.
        if _is_mcp_client_disconnected(e):
            logger.warning("Клиент MCP отключился во время обработки POST-сообщения.")
        else:
            logger.exception("Не удалось обработать сообщение MCP")


async def _send_not_found(send):
    # Для неизвестного маршрута вернуть 404.
    await send({
        'type': 'http.response.start',
        'status': 404,
        'headers': [[b'content-type', b'text/plain']]
    })
    await send({
        'type': 'http.response.body',
        'body': b'Not Found'
    })


async def mcp_asgi_app(scope, receive, send):
    """Чистое ASGI-приложение MCP с расширенным логированием."""
    path = scope.get("path", "")
    method = scope.get("method", "")

    if scope["type"] == "http":
        logger.info(f"Входящий запрос ASGI HTTP: {method} {path}")

        # Маршрутизация по окончанию пути совместима с разными mount path и слешами.
        if path.endswith("/sse"):
            await _run_sse(scope, receive, send)

        elif path.endswith(("/messages", "/messages/")):
            await _handle_mcp_post(scope, receive, send, method)

        else:
            await _send_not_found(send)

def _startup_storage() -> None:
    """Проверить production-хранилище до приёма MCP-запросов."""
    bootstrap_runtime_storage(require_ready=True)
    logger.info("[MCP] PostgreSQL готов к работе")


# Обёртка приложения Starlette.
app = Starlette(
    on_startup=[_startup_storage],
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    ]
)
app.mount("/", mcp_asgi_app)

if __name__ == "__main__":
    import uvicorn
    logger.info("[MCP] Запуск MCP-сервиса AzurPilot (порт: 22268)")
    uvicorn.run(app, host="0.0.0.0", port=22268)
