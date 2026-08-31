from __future__ import annotations

import asyncio
import base64
import inspect

import mcp_server_sse
from module.application import (
    MediaFrame,
    SchedulerQueueClearResult,
    ServiceUnavailableError,
)

LEGACY_TOOL_NAMES = (
    "list_instances",
    "get_status",
    "list_tasks",
    "get_task_help",
    "get_resources",
    "get_config",
    "update_config",
    "get_recent_logs",
    "start_instance",
    "stop_instance",
    "get_screenshot",
    "get_current_running_task",
    "get_scheduler_queue",
    "trigger_task",
    "clear_scheduler_queue",
    "restart_emulator",
    "restart_adb",
)


def test_legacy_tool_catalog_and_schemas_remain_registered():
    tools = asyncio.run(mcp_server_sse.list_tools())
    assert tuple(tool.name for tool in tools) == LEGACY_TOOL_NAMES
    by_name = {tool.name: tool for tool in tools}
    assert by_name["update_config"].input_schema["required"] == [
        "instance",
        "task",
        "group",
        "arg",
        "value",
    ]
    assert by_name["get_recent_logs"].input_schema["properties"]["lines"] == {
        "type": "integer",
        "default": 50,
    }
    assert "required" not in by_name["restart_adb"].input_schema
    assert by_name["restart_adb"].input_schema["properties"]["instance"]["type"] == "string"


def test_mcp_image_conversion_is_the_only_base64_media_boundary():
    content = mcp_server_sse._mcp_image(MediaFrame(b"frame", "image/png"))
    assert content.mime_type == "image/png"
    assert base64.b64decode(content.data) == b"frame"


def test_mcp_delegates_errors_to_safe_application_text(monkeypatch):
    class BrokenRead:
        def get_recent_logs(self, instance: str, limit: int):
            raise ServiceUnavailableError("C:/private/log token=secret")

    class Backend:
        read = BrokenRead()

    monkeypatch.setattr(mcp_server_sse, "_get_backend", lambda: Backend())
    result = asyncio.run(
        mcp_server_sse.call_tool("get_recent_logs", {"instance": "ap"})
    )
    assert len(result) == 1
    assert "временно недоступна" in result[0].text
    assert "private" not in result[0].text
    assert "secret" not in result[0].text


def test_mcp_rejects_missing_required_arguments_without_raw_exception():
    result = asyncio.run(mcp_server_sse.call_tool("get_resources", {}))
    assert "Некорректный запрос" in result[0].text
    assert "KeyError" not in result[0].text


def test_mcp_clear_scheduler_queue_has_explicit_empty_queue_message(monkeypatch):
    class Control:
        def clear_scheduler_queue(self, instance: str) -> SchedulerQueueClearResult:
            return SchedulerQueueClearResult(instance=instance, cleared_tasks=())

    class Backend:
        control = Control()

    monkeypatch.setattr(mcp_server_sse, "_get_backend", lambda: Backend())
    result = asyncio.run(
        mcp_server_sse.call_tool(
            "clear_scheduler_queue",
            {"instance": "ap"},
        )
    )

    assert result[0].text == "Успешно: очередь задач уже пуста."


def test_mcp_handlers_are_thin_and_do_not_reintroduce_legacy_owners():
    source = inspect.getsource(mcp_server_sse)
    for forbidden in (
        "ALAS_CONFIG_NAME",
        "McpConfigHelper",
        "from module.webui",
        "from module.config",
        "module.device.device",
    ):
        assert forbidden not in source
    assert "_get_backend().read" in source
    assert "_get_backend().control" in source
    assert set(mcp_server_sse.TOOL_HANDLERS) == set(LEGACY_TOOL_NAMES)
