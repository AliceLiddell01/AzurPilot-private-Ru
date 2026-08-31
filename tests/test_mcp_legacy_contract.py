from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

import mcp_server_sse
from module.application import (
    AdbRestartResult,
    ConfigSnapshot,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    CurrentTaskSnapshot,
    DashboardResource,
    DashboardResources,
    EmulatorRestartResult,
    InstanceReference,
    InstanceStatus,
    LifecycleOutcome,
    LifecycleResult,
    MediaFrame,
    OperationFailedError,
    RuntimeLogTail,
    RuntimeState,
    SchedulerEntry,
    SchedulerQueueClearResult,
    SchedulerQueueSnapshot,
    ScheduleTaskRequest,
    ScheduleTaskResult,
    ServiceUnavailableError,
    TaskArgumentMetadata,
    TaskGroupMetadata,
    TaskMetadata,
    TaskSummary,
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


class _ContractInstances:
    def list_instances(self):
        return (InstanceReference("ap"),)

    def list_statuses(self):
        return (InstanceStatus("ap", False, RuntimeState.STOPPED),)


class _ContractTasks:
    def list_tasks(self):
        return (TaskSummary("Main", "Главная", "help"),)

    def get_task_metadata(self, name: str):
        assert name == "Main"
        return TaskMetadata(
            name="Main",
            display_name="Главная",
            help="help",
            groups=(
                TaskGroupMetadata(
                    name="General",
                    display_name="Общее",
                    help="",
                    arguments=(
                        TaskArgumentMetadata(
                            name="Count",
                            display_name="Количество",
                            help="",
                            input_type="number",
                            default=1,
                            options=(),
                        ),
                    ),
                ),
            ),
        )


class _ContractRead:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_resources(self, instance: str):
        self.calls.append(("get_resources", instance))
        return DashboardResources(
            items=(DashboardResource("Oil", "Нефть", 10, limit=100),)
        )

    def get_config(self, instance: str, task: str | None = None):
        self.calls.append(("get_config", instance, task))
        return ConfigSnapshot("ap", task, {"Main": {"Enabled": True}})

    def get_recent_logs(self, instance: str, limit: int):
        self.calls.append(("get_recent_logs", instance, limit))
        return RuntimeLogTail("ap", ("line\n",))

    def get_screenshot(self, instance: str):
        self.calls.append(("get_screenshot", instance))
        return MediaFrame(b"frame", "image/png")

    def get_current_running_task(self, instance: str):
        self.calls.append(("get_current_running_task", instance))
        return CurrentTaskSnapshot("ap", "Main")

    def get_scheduler_queue(self, instance: str):
        self.calls.append(("get_scheduler_queue", instance))
        return SchedulerQueueSnapshot(
            "ap",
            (SchedulerEntry("Main", datetime(2026, 8, 31, 12, tzinfo=UTC)),),
        )


class _ContractControl:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def update_config(self, request: ConfigUpdateRequest):
        self.calls.append(("update_config", request))
        return ConfigUpdateResult(request)

    def start_instance(self, instance: str):
        self.calls.append(("start_instance", instance))
        return LifecycleResult(instance, LifecycleOutcome.STARTED)

    def stop_instance(self, instance: str):
        self.calls.append(("stop_instance", instance))
        return LifecycleResult(instance, LifecycleOutcome.STOPPED)

    def trigger_task(self, request: ScheduleTaskRequest):
        self.calls.append(("trigger_task", request))
        return ScheduleTaskResult(request, datetime(2026, 8, 31, 12, tzinfo=UTC))

    def clear_scheduler_queue(self, instance: str):
        self.calls.append(("clear_scheduler_queue", instance))
        return SchedulerQueueClearResult(instance, ("Main",))

    def restart_emulator(self, instance: str):
        self.calls.append(("restart_emulator", instance))
        return EmulatorRestartResult(instance)

    def restart_adb(self, instance: str | None = None):
        self.calls.append(("restart_adb", instance))
        return AdbRestartResult(instance)


class _ContractBackend:
    def __init__(self) -> None:
        self.instances = _ContractInstances()
        self.tasks = _ContractTasks()
        self.read = _ContractRead()
        self.control = _ContractControl()


def test_legacy_tool_catalog_and_schemas_remain_registered():
    tools = asyncio.run(mcp_server_sse.list_tools())
    assert tuple(tool.name for tool in tools) == LEGACY_TOOL_NAMES
    by_name = {tool.name: tool for tool in tools}
    expected_properties = {
        "list_instances": (),
        "get_status": (),
        "list_tasks": (),
        "get_task_help": ("task_name",),
        "get_resources": ("instance",),
        "get_config": ("instance", "task"),
        "update_config": ("instance", "task", "group", "arg", "value"),
        "get_recent_logs": ("instance", "lines"),
        "start_instance": ("instance",),
        "stop_instance": ("instance",),
        "get_screenshot": ("instance",),
        "get_current_running_task": ("instance",),
        "get_scheduler_queue": ("instance",),
        "trigger_task": ("instance", "task"),
        "clear_scheduler_queue": ("instance",),
        "restart_emulator": ("instance",),
        "restart_adb": ("instance",),
    }
    expected_required = {
        "list_instances": (),
        "get_status": (),
        "list_tasks": (),
        "get_task_help": ("task_name",),
        "get_resources": ("instance",),
        "get_config": ("instance",),
        "update_config": ("instance", "task", "group", "arg", "value"),
        "get_recent_logs": ("instance",),
        "start_instance": ("instance",),
        "stop_instance": ("instance",),
        "get_screenshot": ("instance",),
        "get_current_running_task": ("instance",),
        "get_scheduler_queue": ("instance",),
        "trigger_task": ("instance", "task"),
        "clear_scheduler_queue": ("instance",),
        "restart_emulator": ("instance",),
        "restart_adb": (),
    }
    expected_property_types = {
        "list_instances": {},
        "get_status": {},
        "list_tasks": {},
        "get_task_help": {"task_name": "string"},
        "get_resources": {"instance": "string"},
        "get_config": {"instance": "string", "task": "string"},
        "update_config": {
            "instance": "string",
            "task": "string",
            "group": "string",
            "arg": "string",
            "value": None,
        },
        "get_recent_logs": {"instance": "string", "lines": "integer"},
        "start_instance": {"instance": "string"},
        "stop_instance": {"instance": "string"},
        "get_screenshot": {"instance": "string"},
        "get_current_running_task": {"instance": "string"},
        "get_scheduler_queue": {"instance": "string"},
        "trigger_task": {"instance": "string", "task": "string"},
        "clear_scheduler_queue": {"instance": "string"},
        "restart_emulator": {"instance": "string"},
        "restart_adb": {"instance": "string"},
    }
    for name, properties in expected_properties.items():
        schema = by_name[name].input_schema
        assert schema["type"] == "object"
        assert set(schema.get("properties", {})) == set(properties), name
        assert {
            property_name: property_schema.get("type")
            for property_name, property_schema in schema["properties"].items()
        } == expected_property_types[name], name
        required = tuple(schema.get("required", ()))
        assert required == expected_required[name], name

    assert by_name["update_config"].description == (
        "Изменить параметр конфигурации указанного экземпляра. "
        "Формат пути: task.group.arg. Параметры, помеченные "
        "sensitive в generated metadata, скрываются при чтении, а "
        "запись через этот MCP-инструмент запрещена. "
        "Настройте их через WebUI конфигурации экземпляра."
    )
    assert by_name["get_recent_logs"].input_schema["properties"]["lines"] == {
        "type": "integer",
        "default": 50,
    }
    assert by_name["update_config"].input_schema["properties"]["value"]["oneOf"] == [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "object"},
        {"type": "array"},
        {"type": "null"},
    ]


def test_legacy_mcp_success_responses_route_all_tools_through_application(monkeypatch):
    backend = _ContractBackend()

    def call(name: str, arguments: dict[str, object]):
        result = asyncio.run(mcp_server_sse.call_tool(name, arguments))
        assert len(result) == 1, name
        return result[0]

    monkeypatch.setattr(mcp_server_sse, "_get_backend", lambda: backend)
    assert json.loads(call("list_instances", {}).text) == ["ap"]
    assert json.loads(call("get_status", {}).text) == [
        {"instance": "ap", "running": False, "state": 2}
    ]
    assert json.loads(call("list_tasks", {}).text) == ["Main"]
    assert json.loads(call("get_task_help", {"task_name": "Main"}).text)[
        "task_name"
    ] == "Main"
    assert json.loads(call("get_resources", {"instance": "ap"}).text) == {
        "Oil": {"label": "Нефть", "value": 10, "limit": 100}
    }
    assert json.loads(call("get_config", {"instance": "ap"}).text) == {
        "Main": {"Enabled": True}
    }

    update_content = call(
        "update_config",
        {
            "instance": "ap",
            "task": "Main",
            "group": "General",
            "arg": "Count",
            "value": 2,
        },
    )
    assert update_content.text == (
        "Успешно: параметр Main.General.Count обновлён на 2"
    )
    assert backend.control.calls[-1][1] == ConfigUpdateRequest(
        "ap", "Main", "General", "Count", 2
    )

    assert call("get_recent_logs", {"instance": "ap"}).text == "line\n"
    # Безопасный response contract намеренно не раскрывает внутреннюю func.
    assert call("start_instance", {"instance": "ap"}).text == "Успешно: ap запущен"
    assert call("stop_instance", {"instance": "ap"}).text == "Успешно: ap остановлен"

    screenshot = call("get_screenshot", {"instance": "ap"})
    assert screenshot.type == "image"
    assert screenshot.mime_type == "image/png"
    assert base64.b64decode(screenshot.data) == b"frame"

    assert call("get_current_running_task", {"instance": "ap"}).text == "Main"
    assert json.loads(call("get_scheduler_queue", {"instance": "ap"}).text) == [
        {"task": "Main", "next_run": "2026-08-31 12:00:00+00:00"}
    ]
    assert call("trigger_task", {"instance": "ap", "task": "Main"}).text == (
        "Успешно: задача Main запланирована на немедленный запуск."
    )
    assert call("clear_scheduler_queue", {"instance": "ap"}).text == (
        "Успешно: задачи очищены: Main"
    )
    assert call("restart_emulator", {"instance": "ap"}).text == (
        "Успешно: эмулятор ap перезапущен"
    )
    assert call("restart_adb", {"instance": "ap"}).text == (
        "Успешно: сервис ADB перезапущен."
    )

    assert backend.read.calls == [
        ("get_resources", "ap"),
        ("get_config", "ap", None),
        ("get_recent_logs", "ap", 50),
        ("get_screenshot", "ap"),
        ("get_current_running_task", "ap"),
        ("get_scheduler_queue", "ap"),
    ]
    assert backend.control.calls[1:] == [
        ("start_instance", "ap"),
        ("stop_instance", "ap"),
        ("trigger_task", ScheduleTaskRequest("ap", "Main")),
        ("clear_scheduler_queue", "ap"),
        ("restart_emulator", "ap"),
        ("restart_adb", "ap"),
    ]


@pytest.mark.parametrize(
    ("name", "arguments"),
    (
        ("get_task_help", {}),
        ("get_resources", {}),
        ("get_config", {}),
        ("update_config", {}),
        ("get_recent_logs", {}),
        ("start_instance", {}),
        ("stop_instance", {}),
        ("get_screenshot", {}),
        ("get_current_running_task", {}),
        ("get_scheduler_queue", {}),
        ("trigger_task", {}),
        ("clear_scheduler_queue", {}),
        ("restart_emulator", {}),
    ),
)
def test_legacy_mcp_rejects_missing_required_arguments(name, arguments):
    result = asyncio.run(mcp_server_sse.call_tool(name, arguments))

    assert "Некорректный запрос" in result[0].text
    assert "KeyError" not in result[0].text


def test_legacy_mcp_rejects_unscoped_adb_restart_without_running_adapter(monkeypatch):
    calls: list[str | None] = []

    class Control:
        def restart_adb(self, instance: str | None = None):
            calls.append(instance)
            raise OperationFailedError("host-global ADB restart is forbidden")

    class Backend:
        control = Control()

    monkeypatch.setattr(mcp_server_sse, "_get_backend", lambda: Backend())
    result = asyncio.run(mcp_server_sse.call_tool("restart_adb", {}))

    assert result[0].text == "Операция не выполнена."
    assert calls == [None]


@pytest.mark.parametrize("name", LEGACY_TOOL_NAMES)
def test_legacy_mcp_sanitizes_application_errors_for_every_tool(monkeypatch, name):
    async def broken(_arguments):
        raise ServiceUnavailableError("C:/private token=secret")

    monkeypatch.setitem(mcp_server_sse.TOOL_HANDLERS, name, broken)
    result = asyncio.run(mcp_server_sse.call_tool(name, {}))

    assert result[0].text == "Операция временно недоступна."
    assert "private" not in result[0].text
    assert "secret" not in result[0].text


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
