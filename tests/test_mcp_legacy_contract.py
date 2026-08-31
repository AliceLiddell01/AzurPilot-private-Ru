from __future__ import annotations

import asyncio
import ast
import base64
import logging
from pathlib import Path

import mcp_server_sse
from module.application import (
    MediaFrame,
    SchedulerQueueClearResult,
    ServiceUnavailableError,
)
from tests.import_inspection import absolute_import_candidates

ROOT = Path(__file__).resolve().parents[1]

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
    for sensitive_path in (
        "Error.OnePushConfig",
        "Error.LlmApiKey",
        "OpsiGeneral.OpsiOnePushConfig",
    ):
        assert sensitive_path in by_name["update_config"].description
    assert "WebUI" in by_name["update_config"].description
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


def test_mcp_rejects_missing_required_arguments_without_raw_exception(caplog):
    caplog.set_level(logging.WARNING, logger=mcp_server_sse.__name__)
    result = asyncio.run(mcp_server_sse.call_tool("get_resources", {}))
    assert "Некорректный запрос" in result[0].text
    assert "KeyError" not in result[0].text
    assert any(
        record.getMessage()
        == "Операция MCP отклонена; инструмент: get_resources; код: invalid_request"
        for record in caplog.records
    )


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
    mcp_path = Path(mcp_server_sse.__file__).resolve()
    source = mcp_path.read_text(encoding="utf-8")
    for forbidden in (
        "ALAS_CONFIG_NAME",
        "McpConfigHelper",
    ):
        assert forbidden not in source
    tree = ast.parse(source, filename=str(mcp_path))
    imported = {
        candidate
        for node in ast.walk(tree)
        for candidate in absolute_import_candidates(ROOT, mcp_path, node)
    }
    for forbidden_root in ("module.webui", "module.config", "module.device"):
        assert not {
            name
            for name in imported
            if name == forbidden_root or name.startswith(f"{forbidden_root}.")
        }, forbidden_root
    assert "module.device.device" not in source
    assert "_get_backend().read" in source
    assert "_get_backend().control" in source
    assert set(mcp_server_sse.TOOL_HANDLERS) == set(LEGACY_TOOL_NAMES)
