import os
import logging
import json
import datetime
from typing import List, Dict, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    ImageContent,
    Tool,
)
import base64
import time
import subprocess
import threading
from io import BytesIO

from module.config.config import AzurLaneConfig
from module.config.time_source import now as current_time
from module.config.utils import DEFAULT_CONFIG_NAME, alas_instance
from module.webui.process_manager import ProcessManager
from module.config.mcp_helper import McpConfigHelper
from module.webui.setting import State
from module.persistence.runtime import bootstrap_runtime_storage

try:
    from module.webui.fake_pil_module import remove_fake_pil_module
except ImportError:
    remove_fake_pil_module = None

# 初始化日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("azurpilot-mcp")

# 初始化配置助手
helper = McpConfigHelper()

ToolResponse = List[TextContent | ImageContent]

async def list_tools() -> List[Tool]:
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
            description="Изменить параметр конфигурации указанного экземпляра. Формат пути: task.group.arg",
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

async def _tool_list_instances(arguments: Dict[str, Any]) -> ToolResponse:
    instances = alas_instance()
    return [TextContent(type="text", text=json.dumps(instances, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_status(arguments: Dict[str, Any]) -> ToolResponse:
    instances = alas_instance()
    results = []
    for inst in instances:
        manager = ProcessManager.get_manager(inst)
        results.append({"instance": inst, "running": manager.alive, "state": manager.state})
    return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2, default=str))]


async def _tool_list_tasks(arguments: Dict[str, Any]) -> ToolResponse:
    tasks = helper.get_tasks()
    return [TextContent(type="text", text=json.dumps(tasks, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_task_help(arguments: Dict[str, Any]) -> ToolResponse:
    task_name = arguments["task_name"]
    details = helper.get_task_details(task_name)
    return [TextContent(type="text", text=json.dumps(details, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_resources(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    config = AzurLaneConfig(inst)
    res = helper.get_dashboard_resources(config.data)
    return [TextContent(type="text", text=json.dumps(res, ensure_ascii=False, indent=2, default=str))]


async def _tool_get_config(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    task = arguments.get("task")
    config = AzurLaneConfig(inst)
    data = config.data.get(task, {}) if task else config.data
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2, default=str))]


async def _tool_update_config(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    task = arguments["task"]
    group = arguments["group"]
    arg = arguments["arg"]
    value = arguments["value"]
    config = AzurLaneConfig(inst)
    path = f"{task}.{group}.{arg}"
    config.cross_set(path, value)
    config.save()
    return [TextContent(type="text", text=f"Success: Updated {path} to {value}")]


async def _tool_get_recent_logs(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    lines_count = arguments.get("lines", 50)

    # AzurPilot 日志命名规则通常是 YYYY-MM-DD_实例名.txt
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    log_file = f"./log/{date_str}_{inst}.txt"

    if not os.path.exists(log_file):
        # 尝试不带实例名的通用日志
        log_file_alt = f"./log/{date_str}_alas.txt"
        if os.path.exists(log_file_alt):
            log_file = log_file_alt

    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                # 对于超大文件，使用 tail 逻辑更安全
                # 为了简单这里仍使用 readlines，但限制读取范围
                content = f.readlines()
                content = content[-lines_count:]
            return [TextContent(type="text", text="".join(content))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading log: {str(e)}")]
    return [TextContent(type="text", text=f"Log file not found: {log_file}")]


async def _tool_start_instance(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    manager = ProcessManager.get_manager(inst)
    if manager.alive:
        return [TextContent(type="text", text=f"Error: {inst} is already running.")]
    from module.submodule.utils import get_config_mod
    func = get_config_mod(inst)
    manager.start(func=func)
    return [TextContent(type="text", text=f"Success: Started {inst} ({func})")]


async def _tool_stop_instance(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    manager = ProcessManager.get_manager(inst)
    if not manager.alive:
        return [TextContent(type="text", text=f"Error: {inst} is not running.")]
    manager.stop()
    return [TextContent(type="text", text=f"Success: Stopped {inst}")]


async def _tool_get_screenshot(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    if "ALAS_CONFIG_NAME" not in os.environ:
        os.environ["ALAS_CONFIG_NAME"] = inst
    if remove_fake_pil_module:
        remove_fake_pil_module()

    from module.device.device import Device
    from PIL import Image
    try:
        import PIL.JpegImagePlugin  # noqa: F401  # 确保 JPEG 编码器已注册。
    except ImportError:
        pass

    try:
        config = AzurLaneConfig(inst)
        device = Device(config)
        image = device.screenshot()
        image_pil = Image.fromarray(image)

        buffered = BytesIO()
        image_pil.save(buffered, format="JPEG")
        img_data = base64.b64encode(buffered.getvalue()).decode("utf-8")
        return [ImageContent(type="image", data=img_data, mimeType="image/jpeg")]
    except Exception as e:
        import traceback
        error_msg = f"Error getting screenshot: {str(e)}\n{traceback.format_exc()}"
        return [TextContent(type="text", text=error_msg)]


async def _tool_get_current_running_task(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    manager = ProcessManager.get_manager(inst)
    if not manager.alive:
        return [TextContent(type="text", text="Error: Instance is not running.")]
    task = "Unknown"

    date_str = datetime.date.today().strftime("%Y-%m-%d")
    log_file = f"./log/{date_str}_{inst}.txt"
    if not os.path.exists(log_file):
        log_file = f"./log/{date_str}_alas.txt"

    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                for line in reversed(lines):
                    import re
                    # 适配现代 AzurPilot 日志格式: 调度器: 开始任务 `TaskName`
                    m = re.search(r"调度器: 开始任务\s*[`'\" ](.*?)[`'\" ]", line)
                    if not m:
                        # 适配旧版或特殊格式: <<< Run task TaskName >>>
                        m = re.search(r"<<<\s*Run task\s*(.*?)\s*>>>", line)

                    if m:
                        task = m.group(1)
                        break
        except:
            pass
    return [TextContent(type="text", text=task)]


async def _tool_get_scheduler_queue(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    config = AzurLaneConfig(inst)
    queue_data = []
    for task_name in config.data:
        if task_name in ["Alas", "Error", "MUMU", "MumuPlayer12", "EmulatorManagement", "Dashboard"]:
            continue
        scheduler = config.data.get(task_name, {}).get("Scheduler", {})
        if scheduler.get("Enable", False):
            next_run = scheduler.get("NextRun", "2050-01-01 00:00:00")
            queue_data.append({"task": task_name, "next_run": str(next_run)})
    queue_data.sort(key=lambda x: str(x["next_run"]))
    return [TextContent(type="text", text=json.dumps(queue_data, ensure_ascii=False, indent=2))]


async def _tool_trigger_task(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    task = arguments["task"]
    config = AzurLaneConfig(inst)
    config.cross_set(f"{task}.Scheduler.Enable", True)
    now = current_time()
    config.cross_set(f"{task}.Scheduler.NextRun", str(now))
    config.save()
    return [TextContent(type="text", text=f"Success: Task {task} scheduled for immediately.")]


async def _tool_clear_scheduler_queue(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    config = AzurLaneConfig(inst)
    cleared = []
    for task_name in config.data:
        scheduler = config.data.get(task_name, {}).get("Scheduler", {})
        if scheduler.get("Enable", False):
            config.cross_set(f"{task_name}.Scheduler.Enable", False)
            cleared.append(task_name)
    if cleared:
        config.save()
    return [TextContent(type="text", text=f"Success: Cleared tasks: {', '.join(cleared)}")]


async def _tool_restart_emulator(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments["instance"]
    if "ALAS_CONFIG_NAME" not in os.environ:
        os.environ["ALAS_CONFIG_NAME"] = inst
    manager = ProcessManager.get_manager(inst)
    if remove_fake_pil_module:
        remove_fake_pil_module()

    from module.device.device import Device
    try:
        config = AzurLaneConfig(inst)
        device = Device(config)
        device.emulator_stop()
        time.sleep(60)
        device.emulator_start()
        return [TextContent(type="text", text=f"Success: Restarted emulator for {inst}")]
    except Exception as e:
        import traceback
        error_msg = f"Error restarting emulator: {str(e)}\n{traceback.format_exc()}"
        return [TextContent(type="text", text=error_msg)]


async def _tool_restart_adb(arguments: Dict[str, Any]) -> ToolResponse:
    inst = arguments.get("instance", DEFAULT_CONFIG_NAME)
    try:
        # 尝试从 deploy.yaml 获取 ADB 路径
        adb_path = State.deploy_config.AdbExecutable
        if adb_path:
            adb_path = adb_path.replace('\\', '/')

        if not adb_path or not os.path.exists(adb_path):
            # 回退到 connection_attr 的查找逻辑
            adb_search_list = [
                './.venv/Scripts/adb.exe',
                './.venv/bin/adb',
                './bin/adb/adb.exe',
            ]
            for path in adb_search_list:
                if os.path.exists(path):
                    adb_path = os.path.abspath(path)
                    break
            else:
                adb_path = "adb"

        subprocess.run([adb_path, "kill-server"], check=False)
        subprocess.run([adb_path, "start-server"], check=False)
        return [TextContent(type="text", text=f"Success: Restarted ADB service using {adb_path}.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


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


async def call_tool(name: str, arguments: Dict[str, Any]) -> ToolResponse:
    try:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
        return await handler(arguments)
    except Exception as e:
        logger.exception(f"Ошибка инструмента {name}")
        return [TextContent(type="text", text=f"Ошибка: {str(e)}")]


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

# SSE 传输层初始化 - 固定端点（与 /mcp 挂载点匹配）
transport = SseServerTransport("/mcp/messages")


async def _run_sse(scope, receive, send):
    logger.info("Найден endpoint /sse. Открывается SSE-соединение...")
    async with transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
        logger.info("SSE-поток подключён. Запускается цикл MCP-сервера...")
        try:
            options = mcp_server.create_initialization_options()
            await mcp_server.run(read_stream, write_stream, options)
        except Exception as e:
            logger.error(f"Ошибка цикла MCP-сервера: {e}", exc_info=True)
        logger.info("Цикл MCP-сервера завершён.")


def _is_mcp_client_disconnected(error: Exception) -> bool:
    return "BrokenResourceError" in str(type(error)) or "BrokenPipeError" in str(error)


async def _handle_mcp_post(scope, receive, send, method):
    logger.info(f"Найден endpoint /messages. Метод: {method}")
    try:
        await transport.handle_post_message(scope, receive, send)
        logger.info("POST-сообщение MCP обработано.")
    except Exception as e:
        # 捕获常见的断开连接错误，避免服务器崩溃
        if _is_mcp_client_disconnected(e):
            logger.warning("Клиент MCP отключился во время обработки POST-сообщения.")
        else:
            logger.error(f"Не удалось обработать сообщение MCP: {e}", exc_info=True)


async def _send_not_found(send):
    # 未匹配路由，返回 404
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
    """MCP 服务的纯 ASGI 应用，带增强日志记录。"""
    path = scope.get("path", "")
    method = scope.get("method", "")

    if scope["type"] == "http":
        logger.info(f"Входящий запрос ASGI HTTP: {method} {path}")

        # 路由逻辑 - 使用末尾匹配以兼容各种挂载路径和斜线组合
        if path.endswith("/sse"):
            await _run_sse(scope, receive, send)

        elif path.endswith("/messages") or path.endswith("/messages/"):
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
