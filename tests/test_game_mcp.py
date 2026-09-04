from __future__ import annotations

import asyncio
import base64
import json
import shutil
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import CallToolRequestParams, JSONRPCRequest
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

import module.game_mcp.adapter as game_mcp_adapter
from module.application import (
    AdbRestartResult,
    ConfigSnapshot,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    CurrentTaskSnapshot,
    DashboardResource,
    DashboardResources,
    EmulatorRestartResult,
    FleetRefreshPolicy,
    FleetStateObservation,
    FleetStateReadService,
    FleetStateRequest,
    FleetStateResult,
    GameLoginResult,
    GameRuntimeRestartResult,
    InstanceReference,
    InstanceStatus,
    LifecycleOutcome,
    LifecycleResult,
    MediaFrame,
    MoraleFleetState,
    MoraleKnowledge,
    MoraleSelectionState,
    MoraleSlotState,
    PostconditionFailedError,
    ResourceNotFoundError,
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
    TaskOption,
    TaskSummary,
)
from module.application.errors import GameRuntimePhaseError
from module.application.game_control_lock import profile_mutation_lock
from module.application.game_validation import UNKNOWN_TASK
from module.application.instance_identity import runtime_instance_identity
from module.application.storage_models import InstanceIdentity
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.game_mcp.adapter import (
    GAME_MCP_CONTROL_TOOL_NAMES,
    GAME_MCP_TOOL_NAMES,
    GameMcpAdapter,
    GameMcpResponse,
    _result,
)
from module.game_mcp.composition import GameMcpBackend
from module.game_mcp.contract import (
    GAME_MCP_CONTROL_SCOPE,
    GAME_MCP_READ_SCOPE,
    contract_payload,
    tool_catalog_sha256,
)
from module.game_mcp.server import (
    GAME_MCP_ARGS,
    GAME_MCP_COMMAND,
    GAME_MCP_REQUIRED_SCOPE,
    create_server,
    tool_definitions,
)


def _png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x7f\xff"))
        + chunk(b"IEND", b"")
    )


def _empty_snapshot(fleet_index: int) -> FormationFleetSnapshot:
    slots = tuple(
        FormationFleetSlotObservation(side=side, position=position, occupied=False)
        for side, position in (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
    )
    return FormationFleetSnapshot(fleet_index, slots, "0" * 64)


def _fleet_result(instance: str, indices: tuple[int, ...]) -> FleetStateResult:
    _, instance_id = runtime_instance_identity(instance)
    observations = tuple(
        FleetStateObservation(
            id=UUID(int=index),
            run_id=UUID(int=index + 100),
            instance_id=instance_id,
            idempotency_key=f"test:{instance}:{index}",
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
            snapshot=_empty_snapshot(index),
        )
        for index in indices
    )
    selection = FleetSelection(indices)
    return FleetStateResult(
        FleetStateRequest(selection, FleetRefreshPolicy.NEVER),
        observations,
        (),
    )


def _morale_result(indices: tuple[int, ...]) -> MoraleSelectionState:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    fleets: list[MoraleFleetState] = []
    for fleet_index in indices:
        slots = tuple(
            MoraleSlotState(
                fleet_index=fleet_index,
                side=side,
                position=position,
                occupied=None,
                identity_status=None,
                canonical_identity=None,
                canonical_name=None,
                ship_form=None,
                knowledge=MoraleKnowledge.UNKNOWN,
            )
            for side, position in (
                (FormationFleetSide.MAIN, 1),
                (FormationFleetSide.MAIN, 2),
                (FormationFleetSide.MAIN, 3),
                (FormationFleetSide.VANGUARD, 1),
                (FormationFleetSide.VANGUARD, 2),
                (FormationFleetSide.VANGUARD, 3),
            )
        )
        fleets.append(MoraleFleetState(fleet_index, None, None, slots))
    return MoraleSelectionState(FleetSelection(indices), tuple(fleets), now)


class _Instances:
    def __init__(self) -> None:
        self.status_calls: list[str] = []

    def list_instances(self) -> tuple[InstanceReference, ...]:
        return (InstanceReference("alpha"), InstanceReference("beta"))

    def get_status(self, profile: str) -> InstanceStatus:
        self.status_calls.append(profile)
        return InstanceStatus(
            profile,
            running=profile == "alpha",
            state=RuntimeState.RUNNING if profile == "alpha" else RuntimeState.STOPPED,
        )


class _Tasks:
    def list_tasks(self) -> tuple[TaskSummary, ...]:
        return (TaskSummary("Main", "Главная", "Запуск"),)

    def get_task_metadata(self, name: str) -> TaskMetadata:
        if name != "Main":
            raise ResourceNotFoundError("Задача не найдена.")
        return TaskMetadata(
            "Main",
            "Главная",
            "Справка",
            (
                TaskGroupMetadata(
                    "General",
                    "Общие",
                    "Параметры",
                    (
                        TaskArgumentMetadata(
                            "ApiKey",
                            "Ключ",
                            "Секрет",
                            "input",
                            "secret-default",
                            (TaskOption("safe", "Безопасно"),),
                        ),
                    ),
                ),
            ),
        )


class _Read:
    def __init__(self) -> None:
        self.profiles: list[str] = []

    def get_resources(self, profile: str) -> DashboardResources:
        self.profiles.append(profile)
        return DashboardResources(
            (DashboardResource("Oil", "Нефть", 10 if profile == "alpha" else 20),)
        )

    def get_current_running_task(self, profile: str) -> CurrentTaskSnapshot:
        self.profiles.append(profile)
        return CurrentTaskSnapshot(profile, f"Task-{profile}")

    def get_scheduler_queue(self, profile: str) -> SchedulerQueueSnapshot:
        self.profiles.append(profile)
        return SchedulerQueueSnapshot(
            profile,
            (SchedulerEntry("Main", datetime(2026, 9, 1, tzinfo=UTC)),),
        )

    def get_config(self, profile: str, task: str | None = None) -> ConfigSnapshot:
        self.profiles.append(profile)
        return ConfigSnapshot(
            profile,
            task,
            {
                "ProfileValue": profile,
                "Password": "raw-secret",
                "Main": {"ApiKey": "raw"},
            },
        )

    def get_recent_logs(self, profile: str, limit: int) -> RuntimeLogTail:
        self.profiles.append(profile)
        if limit == 0:
            return RuntimeLogTail(profile, ())
        return RuntimeLogTail(
            profile,
            (
                (
                    "\x1b[31msecret token=raw-token oauth_token=oauth-sentinel "
                    "database_password=db-sentinel session_id=session-sentinel "
                    "LlmApiKey=llm-sentinel OAuthToken=oauth-camel-sentinel "
                    "ClientSecret=client-sentinel \\\\server\\share\\private.log "
                    "ratio=N/A date=2026/09/02\x1b[0m\n"
                ),
                'Traceback (most recent call last): File "C:\\private\\run.py"\n',
            )[-limit:],
        )

    def get_screenshot(self, profile: str) -> MediaFrame:
        self.profiles.append(profile)
        return MediaFrame(_png_1x1(), "image/png")


class _Fleet:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def state_read_only(
        self, profile: str, selection: FleetSelection
    ) -> FleetStateResult:
        self.calls.append((profile, selection.fleet_indices))
        return _fleet_result(profile, selection.fleet_indices)


class _Morale:
    def state_read_only(
        self, profile: str, selection: FleetSelection
    ) -> MoraleSelectionState:
        return _morale_result(selection.fleet_indices)


class _Control:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start_instance(self, profile: str) -> LifecycleResult:
        self.calls.append(("start", profile))
        return LifecycleResult(profile, LifecycleOutcome.STARTED)

    def stop_instance(self, profile: str) -> LifecycleResult:
        self.calls.append(("stop", profile))
        return LifecycleResult(profile, LifecycleOutcome.STOPPED)

    def trigger_task(self, request: ScheduleTaskRequest) -> ScheduleTaskResult:
        self.calls.append(("trigger", request.instance))
        return ScheduleTaskResult(
            request,
            datetime(2026, 9, 1, tzinfo=UTC),
            verified=True,
        )

    def clear_scheduler_queue(self, profile: str) -> SchedulerQueueClearResult:
        self.calls.append(("clear", profile))
        return SchedulerQueueClearResult(profile, ("Main",), verified=True)

    def update_config(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        self.calls.append(("update", request.instance))
        return ConfigUpdateResult(request, verified=True)

    def restart_emulator(self, profile: str) -> EmulatorRestartResult:
        self.calls.append(("emulator", profile))
        return EmulatorRestartResult(profile)

    def restart_runtime(self, profile: str) -> GameRuntimeRestartResult:
        self.calls.append(("runtime", profile))
        return GameRuntimeRestartResult(
            profile,
            emulator_verified=True,
            adb_ready=True,
            game_running=True,
            game_foreground=True,
        )

    def login_runtime(self, profile: str) -> GameLoginResult:
        self.calls.append(("login", profile))
        return GameLoginResult(
            profile,
            verified=True,
            adb_ready=True,
            game_running=True,
            game_foreground=True,
            logged_in=True,
            main=True,
        )

    def restart_adb(self, profile: str) -> AdbRestartResult:
        self.calls.append(("adb", profile))
        return AdbRestartResult(profile)


def _backend() -> SimpleNamespace:
    return SimpleNamespace(
        instances=_Instances(),
        tasks=_Tasks(),
        read=_Read(),
        control=_Control(),
        fleet_state=_Fleet(),
        morale=_Morale(),
    )


def test_contract_and_tool_catalog_are_game_specific_and_scope_separated() -> None:
    contract = contract_payload()
    assert contract["game_mcp_api_version"] == 1
    assert {
        "ready",
        "running",
        "stopped",
        "warning",
        "updating",
        "partial",
        "unknown",
        "not_running",
        "unavailable",
        "failed",
    } <= set(contract["result_states"])
    assert contract["authorization_scopes"] == [
        "azurpilot:game.read",
        "azurpilot:game.control",
    ]
    assert contract["tool_count"] == len(GAME_MCP_TOOL_NAMES) == 22
    assert contract["tool_catalog_sha256"] == tool_catalog_sha256(GAME_MCP_TOOL_NAMES)
    assert contract["feature_flags"]["read_only"] is False
    assert contract["feature_flags"]["control_plane"] is True
    assert "dev_mcp_api_version" not in contract
    assert "runtime_control" not in contract["feature_flags"]

    tools = tool_definitions()
    assert [tool.name for tool in tools] == [
        "game_get_contract",
        "game_list_profiles",
        "game_get_profile_status",
        "game_get_resources",
        "game_get_current_task",
        "game_get_scheduler_queue",
        "game_list_tasks",
        "game_get_task_help",
        "game_get_fleet_state",
        "game_get_morale",
        "game_get_config",
        "game_get_recent_logs",
        "game_get_screenshot",
        "game_start_profile",
        "game_stop_profile",
        "game_trigger_task",
        "game_clear_scheduler_queue",
        "game_update_config",
        "game_restart_emulator",
        "game_restart_runtime",
        "game_login_runtime",
        "game_restart_adb",
    ]
    control_tools = [
        tool for tool in tools if tool.name in GAME_MCP_CONTROL_TOOL_NAMES
    ]
    read_tools = [
        tool for tool in tools if tool.name not in GAME_MCP_CONTROL_TOOL_NAMES
    ]
    assert all(tool.annotations.read_only_hint for tool in read_tools)
    assert all(tool.annotations.destructive_hint is False for tool in read_tools)
    assert all(tool.annotations.idempotent_hint for tool in read_tools)
    assert all(
        tool.meta
        == {
            "securitySchemes": [{"type": "oauth2", "scopes": [GAME_MCP_REQUIRED_SCOPE]}]
        }
        for tool in read_tools
    )
    assert all(tool.annotations.read_only_hint is False for tool in control_tools)
    assert all(tool.annotations.open_world_hint is False for tool in control_tools)
    expected_control_annotations = {
        "game_start_profile": (False, True),
        "game_stop_profile": (True, True),
        "game_trigger_task": (False, False),
        "game_clear_scheduler_queue": (True, True),
        "game_update_config": (True, False),
        "game_restart_emulator": (True, False),
        "game_restart_runtime": (True, False),
        "game_login_runtime": (True, False),
        "game_restart_adb": (True, False),
    }
    assert {
        tool.name: (
            tool.annotations.destructive_hint,
            tool.annotations.idempotent_hint,
        )
        for tool in control_tools
    } == expected_control_annotations
    assert all(
        tool.meta == {
            "securitySchemes": [
                {"type": "oauth2", "scopes": ["azurpilot:game.control"]}
            ]
        }
        for tool in control_tools
    )
    assert all(tool.output_schema["additionalProperties"] is False for tool in tools)
    assert all(
        tool.output_schema["properties"]["details"]["additionalProperties"] is False
        for tool in tools
    )
    assert not any("database" in tool.name or "dev_" in tool.name for tool in tools)

    for tool in tools:
        assert tool.input_schema["additionalProperties"] is False
        assert tool.description


def test_tool_catalog_fingerprint_is_order_independent_and_rejects_duplicates() -> None:
    expected = tool_catalog_sha256(GAME_MCP_TOOL_NAMES)
    assert tool_catalog_sha256(reversed(GAME_MCP_TOOL_NAMES)) == expected
    assert tool_catalog_sha256((*GAME_MCP_TOOL_NAMES, "game_future_tool")) != expected
    with pytest.raises(ValueError, match="повторные"):
        tool_catalog_sha256((*GAME_MCP_TOOL_NAMES, GAME_MCP_TOOL_NAMES[0]))


def test_contract_reports_bounded_request_context_without_token_data() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    local = adapter.call("game_get_contract")
    assert local["details"]["request_context"] == {
        "transport": "local_stdio",
        "authenticated": False,
        "local_authority": True,
        "granted_scopes": [],
        "read_allowed": True,
        "control_allowed": True,
    }

    remote = adapter.call(
        "game_get_contract",
        scopes=(GAME_MCP_READ_SCOPE, GAME_MCP_CONTROL_SCOPE),
    )
    assert remote["details"]["request_context"] == {
        "transport": "remote_http",
        "authenticated": True,
        "local_authority": False,
        "granted_scopes": [GAME_MCP_READ_SCOPE, GAME_MCP_CONTROL_SCOPE],
        "read_allowed": True,
        "control_allowed": True,
    }
    assert "token" not in json.dumps(remote, ensure_ascii=False).casefold()


def test_output_schemas_are_scoped_to_their_tool_details() -> None:
    expected = {
        "game_get_contract": {"contract", "request_context", "tool"},
        "game_list_profiles": {"profiles", "tool"},
        "game_get_profile_status": {"profile", "running", "state", "tool"},
        "game_get_resources": {"profile", "resources", "tool"},
        "game_get_current_task": {"profile", "task", "tool"},
        "game_get_scheduler_queue": {"entries", "profile", "tool"},
        "game_list_tasks": {"tasks", "tool"},
        "game_get_task_help": {"task", "tool"},
        "game_get_fleet_state": {
            "coverage_complete",
            "missing_fleet_indices",
            "observations",
            "profile",
            "selection",
            "snapshots_complete",
            "tool",
        },
        "game_get_morale": {
            "fleets",
            "profile",
            "projected_at",
            "selection",
            "tool",
        },
        "game_get_config": {"config", "profile", "task", "tool"},
        "game_get_recent_logs": {"lines", "profile", "tool", "truncated"},
        "game_get_screenshot": {"profile", "screenshot", "tool"},
        "game_start_profile": {"outcome", "profile", "tool"},
        "game_stop_profile": {"outcome", "profile", "tool"},
        "game_trigger_task": {
            "profile",
            "scheduled_at",
            "task",
            "tool",
            "verified",
        },
        "game_clear_scheduler_queue": {
            "cleared_count",
            "cleared_tasks",
            "profile",
            "tool",
            "verified",
        },
        "game_update_config": {
            "argument",
            "group",
            "profile",
            "task",
            "tool",
            "verified",
        },
        "game_restart_emulator": {"profile", "tool", "verified"},
        "game_restart_runtime": {
            "adb_ready",
            "emulator_verified",
            "game_foreground",
            "game_running",
            "phase",
            "profile",
            "tool",
            "verified",
        },
        "game_login_runtime": {
            "adb_ready",
            "game_foreground",
            "game_running",
            "logged_in",
            "main",
            "phase",
            "profile",
            "tool",
            "verified",
        },
        "game_restart_adb": {"profile", "tool", "verified"},
    }
    actual = {
        tool.name: set(tool.output_schema["properties"]["details"]["properties"])
        for tool in tool_definitions()
    }

    assert actual == expected


def test_structured_content_conforms_to_each_advertised_output_schema() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    calls = (
        ("game_get_contract", {}),
        ("game_list_profiles", {}),
        ("game_get_profile_status", {"profile": "alpha"}),
        ("game_get_resources", {"profile": "alpha"}),
        ("game_get_current_task", {"profile": "alpha"}),
        ("game_get_scheduler_queue", {"profile": "alpha"}),
        ("game_list_tasks", {}),
        ("game_get_task_help", {"task": "Main"}),
        ("game_get_fleet_state", {"profile": "alpha", "fleet_indices": [1]}),
        ("game_get_morale", {"profile": "alpha", "fleet_indices": [1]}),
        ("game_get_config", {"profile": "alpha"}),
        ("game_get_recent_logs", {"profile": "alpha", "lines": 2}),
        ("game_get_screenshot", {"profile": "alpha"}),
        ("game_start_profile", {"profile": "alpha"}),
        ("game_stop_profile", {"profile": "alpha"}),
        ("game_trigger_task", {"profile": "alpha", "task": "Main"}),
        ("game_clear_scheduler_queue", {"profile": "alpha"}),
        (
            "game_update_config",
            {
                "profile": "alpha",
                "task": "Main",
                "group": "General",
                "argument": "Mode",
                "value": "safe",
            },
        ),
        ("game_restart_emulator", {"profile": "alpha"}),
        ("game_restart_runtime", {"profile": "alpha"}),
        ("game_login_runtime", {"profile": "alpha"}),
        ("game_restart_adb", {"profile": "alpha"}),
    )
    tools = {tool.name: tool for tool in tool_definitions()}
    task_help: dict[str, object] | None = None

    for name, arguments in calls:
        response = adapter.call(name, arguments)
        structured = (
            response.structured if isinstance(response, GameMcpResponse) else response
        )
        assert isinstance(structured, dict)
        errors = list(
            Draft202012Validator(tools[name].output_schema).iter_errors(structured)
        )
        assert not errors, f"{name}: {errors[0].message if errors else ''}"
        if name == "game_get_task_help":
            task_help = structured

    assert task_help is not None
    malformed = json.loads(json.dumps(task_help, ensure_ascii=False))
    malformed["details"]["task"]["unexpected"] = True
    validator = Draft202012Validator(tools["game_get_task_help"].output_schema)
    assert not validator.is_valid(malformed)


def test_profile_selector_allows_internal_spaces_and_rejects_unsafe_edges() -> None:
    backend = _backend()
    backend.instances.list_instances = lambda: (InstanceReference("alpha beta"),)
    adapter = GameMcpAdapter(lambda: backend)

    profiles = adapter.call("game_list_profiles")
    assert profiles["details"]["profiles"] == [{"profile": "alpha beta"}]
    for tool, arguments in (
        ("game_get_profile_status", {"profile": "alpha beta"}),
        ("game_get_resources", {"profile": "alpha beta"}),
        ("game_get_current_task", {"profile": "alpha beta"}),
        ("game_get_scheduler_queue", {"profile": "alpha beta"}),
        (
            "game_get_fleet_state",
            {"profile": "alpha beta", "fleet_indices": [1]},
        ),
        ("game_get_morale", {"profile": "alpha beta", "fleet_indices": [1]}),
        ("game_get_config", {"profile": "alpha beta"}),
        ("game_get_recent_logs", {"profile": "alpha beta", "lines": 1}),
        ("game_get_screenshot", {"profile": "alpha beta"}),
    ):
        result = adapter.call(tool, arguments)
        structured = result.structured if isinstance(result, GameMcpResponse) else result
        assert structured["code"] not in {
            "GAME_MCP_INVALID_REQUEST",
            "GAME_UNKNOWN_PROFILE",
        }

    profile_schema = next(
        tool.input_schema["properties"]["profile"]
        for tool in tool_definitions()
        if tool.name == "game_get_resources"
    )
    validator = Draft202012Validator(profile_schema)
    assert validator.is_valid("alpha beta")
    for unsafe in (" alpha", "alpha ", "alpha/../beta", "alpha\x00beta", "alpha\nbeta"):
        assert not validator.is_valid(unsafe)


def test_result_sequence_bounds_preserve_data_or_fail_explicitly() -> None:
    for count in (255, 256, 257, 512):
        result = _result(
            ok=True,
            code="TEST_RESULT",
            message="ok",
            state="ready",
            details={"items": list(range(count))},
        )
        assert result["code"] == "TEST_RESULT"
        assert result["details"]["items"] == list(range(count))

    overflow = _result(
        ok=True,
        code="TEST_RESULT",
        message="ok",
        state="ready",
        details={"items": list(range(513))},
    )
    assert overflow["code"] == "GAME_RESULT_LIMIT_EXCEEDED"
    assert overflow["ok"] is False

    oversized = _result(
        ok=True,
        code="TEST_RESULT",
        message="ok",
        state="ready",
        details={"items": ["x" * 4096] * 512},
    )
    assert oversized["code"] == "GAME_RESPONSE_TOO_LARGE"
    assert oversized["ok"] is False
    assert oversized["details"] == {}

    nested: object = "leaf"
    for _ in range(10):
        nested = {"nested": nested}
    depth_limited = _result(
        ok=True,
        code="TEST_RESULT",
        message="ok",
        state="ready",
        details={"nested": nested},
    )
    assert depth_limited["code"] == "TEST_RESULT"
    assert "<вложенность скрыта>" in json.dumps(
        depth_limited, ensure_ascii=False
    )


def test_server_construction_and_contract_are_lazy() -> None:
    calls: list[int] = []

    def factory() -> object:
        calls.append(1)
        return object()

    adapter = GameMcpAdapter(factory)
    create_server(adapter)
    assert calls == []
    contract = adapter.call("game_get_contract")
    assert contract["ok"] is True
    assert calls == []


def test_adapter_hides_backend_factory_failures() -> None:
    def failing_factory() -> object:
        raise RuntimeError("private backend path")

    adapter = GameMcpAdapter(failing_factory)
    result = adapter.call("game_list_profiles")

    assert result["code"] == "GAME_SERVICE_UNAVAILABLE"
    assert result["details"] == {"tool": "game_list_profiles"}
    assert "private backend path" not in json.dumps(result, ensure_ascii=False)


def test_control_scope_is_checked_before_backend_factory_and_arguments() -> None:
    factory_calls: list[bool] = []

    def factory() -> object:
        factory_calls.append(True)
        return _backend()

    adapter = GameMcpAdapter(factory)
    result = adapter.call(
        "game_start_profile",
        {"profile": "alpha"},
        scopes=(GAME_MCP_REQUIRED_SCOPE,),
    )

    assert result == {
        "ok": False,
        "code": "GAME_MCP_UNAUTHORIZED",
        "message": "Недостаточно полномочий для этого инструмента Game MCP.",
        "state": "failed",
        "details": {"tool": "game_start_profile"},
    }
    assert factory_calls == []


def test_control_tools_return_typed_bounded_results() -> None:
    backend = _backend()
    adapter = GameMcpAdapter(lambda: backend)

    assert adapter.call("game_start_profile", {"profile": "alpha"})["code"] == (
        "GAME_PROFILE_STARTED"
    )
    assert adapter.call("game_stop_profile", {"profile": "alpha"})["code"] == (
        "GAME_PROFILE_STOPPED"
    )
    task = adapter.call(
        "game_trigger_task", {"profile": "alpha", "task": "Main"}
    )
    assert task["code"] == "GAME_TASK_SCHEDULED"
    assert task["details"]["verified"] is True
    cleared = adapter.call("game_clear_scheduler_queue", {"profile": "alpha"})
    assert cleared["code"] == "GAME_SCHEDULER_QUEUE_CLEARED"
    updated = adapter.call(
        "game_update_config",
        {
            "profile": "alpha",
            "task": "Main",
            "group": "General",
            "argument": "Mode",
            "value": "safe",
        },
    )
    assert updated["code"] == "GAME_CONFIG_UPDATED"
    updated_json = json.dumps(updated, ensure_ascii=False)
    assert "value" not in updated_json
    assert "safe" not in updated_json
    assert adapter.call("game_restart_emulator", {"profile": "alpha"})["code"] == (
        "GAME_EMULATOR_RESTARTED"
    )
    runtime = adapter.call("game_restart_runtime", {"profile": "alpha"})
    assert runtime["code"] == "GAME_RUNTIME_RESTARTED"
    assert runtime["details"]["game_foreground"] is True
    login = adapter.call("game_login_runtime", {"profile": "alpha"})
    assert login["code"] == "GAME_RUNTIME_LOGIN_CONFIRMED"
    assert login["details"]["logged_in"] is True
    assert login["details"]["main"] is True
    assert adapter.call("game_restart_adb", {"profile": "alpha"})["code"] == (
        "GAME_ADB_RESTARTED"
    )


def test_runtime_failure_preserves_existing_code_and_reports_safe_phase() -> None:
    backend = _backend()

    def fail_runtime(profile: str) -> GameRuntimeRestartResult:
        raise GameRuntimePhaseError(
            "game_start",
            PostconditionFailedError("internal foreground detail"),
        )

    backend.control.restart_runtime = fail_runtime
    result = GameMcpAdapter(lambda: backend).call(
        "game_restart_runtime",
        {"profile": "alpha"},
    )

    assert result == {
        "ok": False,
        "code": "GAME_POSTCONDITION_FAILED",
        "message": "Эмулятор перезапущен, но запуск игры не подтверждён ожидаемым состоянием.",
        "state": "failed",
        "details": {"phase": "game_start", "tool": "game_restart_runtime"},
    }
    assert "internal foreground detail" not in json.dumps(result, ensure_ascii=False)
    errors = list(
        Draft202012Validator(
            next(
                tool.output_schema
                for tool in tool_definitions()
                if tool.name == "game_restart_runtime"
            )
        ).iter_errors(result)
    )
    assert not errors


def test_login_runtime_failure_reports_login_phase_without_internal_detail() -> None:
    backend = _backend()

    def fail_login(profile: str) -> GameLoginResult:
        raise GameRuntimePhaseError(
            "login",
            PostconditionFailedError("internal main UI detail"),
        )

    backend.control.login_runtime = fail_login
    result = GameMcpAdapter(lambda: backend).call(
        "game_login_runtime",
        {"profile": "alpha"},
    )

    assert result == {
        "ok": False,
        "code": "GAME_POSTCONDITION_FAILED",
        "message": "Вход в игру не подтверждён главным экраном.",
        "state": "failed",
        "details": {"phase": "login", "tool": "game_login_runtime"},
    }
    assert "internal main UI detail" not in json.dumps(result, ensure_ascii=False)


def test_control_result_without_authoritative_verification_fails_closed() -> None:
    backend = _backend()
    backend.control.update_config = lambda request: ConfigUpdateResult(request)
    result = GameMcpAdapter(lambda: backend).call(
        "game_update_config",
        {
            "profile": "alpha",
            "task": "Main",
            "group": "General",
            "argument": "Mode",
            "value": "safe",
        },
    )

    assert result["code"] == "GAME_POSTCONDITION_FAILED"


def test_adapter_rejects_calls_after_close_without_recreating_backend() -> None:
    backend = _backend()
    factory_calls: list[bool] = []
    disposed: list[bool] = []
    backend.dispose = lambda: disposed.append(True)

    def factory() -> object:
        factory_calls.append(True)
        return backend

    adapter = GameMcpAdapter(factory)
    assert adapter.call("game_list_tasks")["code"] == "GAME_TASKS_READY"

    adapter.close()

    assert disposed == [True]
    assert (
        adapter.call("game_list_tasks")["code"] == "GAME_SERVICE_UNAVAILABLE"
    )
    assert (
        adapter.call("game_get_contract")["code"] == "GAME_SERVICE_UNAVAILABLE"
    )
    assert factory_calls == [True]

    adapter.close()
    assert disposed == [True]


def test_adapter_is_stateless_and_profile_reads_are_isolated() -> None:
    backend = _backend()
    adapter = GameMcpAdapter(lambda: backend)

    profiles = adapter.call("game_list_profiles")
    assert profiles["details"]["profiles"] == [
        {"profile": "alpha"},
        {"profile": "beta"},
    ]
    assert (
        adapter.call("game_get_resources", {"profile": "alpha"})["details"][
            "resources"
        ][0]["value"]
        == 10
    )
    assert (
        adapter.call("game_get_resources", {"profile": "beta"})["details"]["resources"][
            0
        ]["value"]
        == 20
    )
    assert (
        adapter.call("game_get_current_task", {"profile": "alpha"})["details"]["task"]
        == "Task-alpha"
    )
    assert (
        adapter.call("game_get_current_task", {"profile": "beta"})["details"]["task"]
        == "Task-beta"
    )
    assert (
        adapter.call("game_get_resources", {"profile": "alpha"})["details"][
            "resources"
        ][0]["value"]
        == 10
    )
    assert (
        adapter.call("game_get_fleet_state", {"profile": "beta", "fleet_indices": [2]})[
            "details"
        ]["profile"]
        == "beta"
    )
    assert backend.fleet_state.calls == [("beta", (2,))]


def test_adapter_does_not_hold_lifecycle_lock_during_dispatch() -> None:
    backend = _backend()
    entered = Event()
    release = Event()
    original_get_resources = backend.read.get_resources

    def blocking_get_resources(profile: str) -> DashboardResources:
        entered.set()
        release.wait(5)
        return original_get_resources(profile)

    backend.read.get_resources = blocking_get_resources
    adapter = GameMcpAdapter(lambda: backend)
    first_result: dict[str, object] = {}
    second_result: dict[str, object] = {}
    second_done = Event()

    def first_call() -> None:
        first_result["value"] = adapter.call(
            "game_get_resources", {"profile": "alpha"}
        )

    def second_call() -> None:
        second_result["value"] = adapter.call("game_list_tasks")
        second_done.set()

    first_thread = Thread(target=first_call)
    second_thread = Thread(target=second_call)
    first_thread.start()
    try:
        assert entered.wait(5)
        second_thread.start()
        assert second_done.wait(5)
    finally:
        release.set()
        first_thread.join(timeout=5)
        if second_thread.ident is not None:
            second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_result["value"]["code"] == "GAME_RESOURCES_READY"
    assert second_result["value"]["code"] == "GAME_TASKS_READY"


def test_adapter_serializes_mutations_per_profile() -> None:
    backend = _backend()
    entered = Event()
    release = Event()
    second_entered = Event()
    state_lock = Lock()
    invocation_count = 0
    active_count = 0
    max_active = 0

    class _SerializedControl(_Control):
        def start_instance(self, profile: str) -> LifecycleResult:
            nonlocal active_count, invocation_count, max_active
            with state_lock:
                invocation_count += 1
                invocation = invocation_count
                active_count += 1
                max_active = max(max_active, active_count)
            if invocation == 1:
                entered.set()
                release.wait(5)
            else:
                second_entered.set()
            with state_lock:
                active_count -= 1
            return LifecycleResult(profile, LifecycleOutcome.STARTED)

    backend.control = _SerializedControl()
    adapter = GameMcpAdapter(lambda: backend)
    results: list[dict[str, object]] = []

    def call_start() -> None:
        results.append(adapter.call("game_start_profile", {"profile": "alpha"}))

    first_thread = Thread(target=call_start)
    second_thread = Thread(target=call_start)
    first_thread.start()
    try:
        assert entered.wait(5)
        second_thread.start()
        assert not second_entered.wait(0.2)
    finally:
        release.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(results) == 2
    assert all(result["code"] == "GAME_PROFILE_STARTED" for result in results)
    assert max_active == 1


def test_independent_adapters_share_mutation_lock_for_one_profile(
    tmp_path: Path,
) -> None:
    backend = _backend()
    entered = Event()
    release = Event()
    second_entered = Event()
    invocation_count = 0
    state_lock = Lock()

    class _BlockingControl(_Control):
        def start_instance(self, profile: str) -> LifecycleResult:
            nonlocal invocation_count
            with state_lock:
                invocation_count += 1
                invocation = invocation_count
            if invocation == 1:
                entered.set()
                assert release.wait(5)
            else:
                second_entered.set()
            return LifecycleResult(profile, LifecycleOutcome.STARTED)

    backend.control = _BlockingControl()
    first_adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
    second_adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
    results: list[dict[str, object]] = []

    def call_start(adapter: GameMcpAdapter) -> None:
        results.append(adapter.call("game_start_profile", {"profile": "alpha"}))

    first_thread = Thread(target=call_start, args=(first_adapter,))
    second_thread = Thread(target=call_start, args=(second_adapter,))
    first_thread.start()
    try:
        assert entered.wait(5)
        second_thread.start()
        assert not second_entered.wait(0.2)
    finally:
        release.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert len(results) == 2
    assert all(result["code"] == "GAME_PROFILE_STARTED" for result in results)


def test_adapter_returns_busy_when_mutation_lock_times_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(game_mcp_adapter, "_MUTATION_LOCK_TIMEOUT_SECONDS", 0.01)
    backend = _backend()
    entered = Event()
    release = Event()
    calls: list[str] = []

    class _BlockingControl(_Control):
        def start_instance(self, profile: str) -> LifecycleResult:
            calls.append(profile)
            entered.set()
            release.wait(5)
            return LifecycleResult(profile, LifecycleOutcome.STARTED)

    backend.control = _BlockingControl()
    adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
    first_result: dict[str, object] = {}

    def call_start() -> None:
        first_result.update(
            adapter.call("game_start_profile", {"profile": "alpha"})
        )

    first_thread = Thread(target=call_start)
    first_thread.start()
    try:
        assert entered.wait(5)
        busy = adapter.call("game_start_profile", {"profile": "alpha"})
        assert busy["code"] == "GAME_RESOURCE_BUSY"
        assert calls == ["alpha"]
    finally:
        release.set()
        first_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert first_result["code"] == "GAME_PROFILE_STARTED"


def test_server_cancellation_does_not_retry_started_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = _backend()
        entered = Event()
        release = Event()
        finished = Event()
        calls: list[str] = []

        class _BlockingControl(_Control):
            def start_instance(self, profile: str) -> LifecycleResult:
                calls.append(profile)
                entered.set()
                assert release.wait(5)
                try:
                    return LifecycleResult(profile, LifecycleOutcome.STARTED)
                finally:
                    finished.set()

        backend.control = _BlockingControl()
        adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
        handler_entry = create_server(
            adapter,
            abandon_on_cancel=True,
            redirect_legacy_stdout=False,
        ).get_request_handler("tools/call")
        assert handler_entry is not None

        task = asyncio.create_task(
            handler_entry.handler(
                None,
                CallToolRequestParams(
                    name="game_start_profile",
                    arguments={"profile": "alpha"},
                ),
            )
        )
        assert await anyio.to_thread.run_sync(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release.set()
        assert await anyio.to_thread.run_sync(finished.wait, 5)
        assert calls == ["alpha"]

    asyncio.run(scenario())


def test_server_cancellation_while_waiting_for_lock_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(game_mcp_adapter, "_MUTATION_LOCK_TIMEOUT_SECONDS", 0.05)

    async def scenario() -> None:
        backend = _backend()
        lookup_started = Event()
        calls: list[str] = []

        class _ObservedInstances(_Instances):
            def list_instances(self) -> tuple[InstanceReference, ...]:
                lookup_started.set()
                return super().list_instances()

        class _CountingControl(_Control):
            def start_instance(self, profile: str) -> LifecycleResult:
                calls.append(profile)
                return LifecycleResult(profile, LifecycleOutcome.STARTED)

        backend.instances = _ObservedInstances()
        backend.control = _CountingControl()
        adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
        handler_entry = create_server(
            adapter,
            abandon_on_cancel=True,
            redirect_legacy_stdout=False,
        ).get_request_handler("tools/call")
        assert handler_entry is not None

        with profile_mutation_lock("alpha", repository_root=tmp_path):
            task = asyncio.create_task(
                handler_entry.handler(
                    None,
                    CallToolRequestParams(
                        name="game_start_profile",
                        arguments={"profile": "alpha"},
                    ),
                )
            )
            assert await anyio.to_thread.run_sync(lookup_started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await anyio.sleep(0.1)
            assert calls == []

    asyncio.run(scenario())


def test_adapter_serializes_mutations_for_different_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backend = _backend()
    first_entered = Event()
    second_requested = Event()
    release = Event()
    state_lock = Lock()
    active_count = 0
    max_active = 0

    class _ParallelControl(_Control):
        def start_instance(self, profile: str) -> LifecycleResult:
            nonlocal active_count, max_active
            with state_lock:
                active_count += 1
                max_active = max(max_active, active_count)
                first_entered.set()
            release.wait(5)
            with state_lock:
                active_count -= 1
            return LifecycleResult(profile, LifecycleOutcome.STARTED)

    backend.control = _ParallelControl()
    adapter = GameMcpAdapter(lambda: backend, mutation_lock_root=tmp_path)
    results: list[dict[str, object]] = []

    original_lock = game_mcp_adapter.profile_mutation_lock

    def observed_lock(profile: str, **kwargs: object):
        if profile == "beta":
            second_requested.set()
        return original_lock(profile, **kwargs)

    monkeypatch.setattr(game_mcp_adapter, "profile_mutation_lock", observed_lock)

    def call_start(profile: str) -> None:
        results.append(adapter.call("game_start_profile", {"profile": profile}))

    first_thread = Thread(target=call_start, args=("alpha",))
    second_thread = Thread(target=call_start, args=("beta",))
    first_thread.start()
    try:
        assert first_entered.wait(5)
        second_thread.start()
        assert second_requested.wait(5)
    finally:
        release.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

    threads = [
        first_thread,
        second_thread,
    ]
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(result["code"] == "GAME_PROFILE_STARTED" for result in results)
    assert max_active == 1


def test_adapter_rejects_bad_selectors_unknown_profiles_and_unknown_tasks() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    for arguments in (
        {"profile": " alpha"},
        {"profile": "alpha/../beta"},
        {"profile": "alpha", "extra": True},
        {"profile": "gamma"},
    ):
        result = adapter.call("game_get_resources", arguments)
        expected = (
            "GAME_MCP_INVALID_REQUEST"
            if arguments["profile"] != "gamma"
            else "GAME_UNKNOWN_PROFILE"
        )
        assert result["code"] == expected

    assert (
        adapter.call("game_get_config", {"profile": "alpha", "task": "Missing"})["code"]
        == "GAME_UNKNOWN_TASK"
    )
    assert adapter.call("unknown", {})["code"] == "GAME_MCP_UNKNOWN_TOOL"
    assert (
        adapter.call("game_get_morale", {"profile": "alpha", "fleet_indices": [1, 1]})[
            "code"
        ]
        == "GAME_MCP_INVALID_REQUEST"
    )
    assert (
        adapter.call("game_get_recent_logs", {"profile": "alpha", "lines": True})[
            "code"
        ]
        == "GAME_MCP_INVALID_REQUEST"
    )


def test_adapter_redacts_config_values() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    config = adapter.call("game_get_config", {"profile": "alpha"})
    config_json = json.dumps(config, ensure_ascii=False)
    assert "raw-secret" not in config_json
    assert '"Password": "<скрыто>"' in config_json
    assert '"ApiKey": "raw"' not in config_json


def test_adapter_redacts_task_help_values() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    task_help = adapter.call("game_get_task_help", {"task": "Main"})
    task_help_json = json.dumps(task_help, ensure_ascii=False)
    assert '"value": "<скрыто>"' in task_help_json
    assert '"value": "safe"' not in task_help_json


def test_adapter_sanitizes_logs_without_secrets_or_paths() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    logs = adapter.call("game_get_recent_logs", {"profile": "alpha", "lines": 2})
    logs_json = json.dumps(logs, ensure_ascii=False)
    assert "raw-token" not in logs_json
    for sentinel in (
        "oauth-sentinel",
        "db-sentinel",
        "session-sentinel",
        "llm-sentinel",
        "oauth-camel-sentinel",
        "client-sentinel",
        "private.log",
    ):
        assert sentinel not in logs_json
    assert "Traceback" not in logs_json
    assert "ratio=N/A" in logs_json
    assert "date=2026/09/02" in logs_json
    log_lines = logs["details"]["lines"]
    assert all("private.log" not in line for line in log_lines)
    assert all(r"C:\private\run.py" not in line for line in log_lines)
    assert "\x1b" not in logs_json


def test_adapter_limits_public_log_lines_to_the_advertised_bound() -> None:
    backend = _backend()
    backend.read.get_recent_logs = lambda profile, _limit: RuntimeLogTail(
        profile,
        tuple(f"line-{index}\n" for index in range(250)),
    )
    adapter = GameMcpAdapter(lambda: backend)

    result = adapter.call("game_get_recent_logs", {"profile": "alpha", "lines": 200})

    assert result["details"]["lines"] == [
        f"line-{index}\n" for index in range(50, 250)
    ]
    assert result["details"]["truncated"] is True


@pytest.mark.parametrize(
    "entries",
    [
        (object(),),
        tuple(
            SchedulerEntry(f"Task-{index}", datetime(2026, 9, 1, tzinfo=UTC))
            for index in range(513)
        ),
    ],
)
def test_adapter_rejects_malformed_scheduler_entries(entries: tuple[object, ...]) -> None:
    backend = _backend()
    snapshot = SchedulerQueueSnapshot("alpha", (SchedulerEntry("Main", datetime(2026, 9, 1, tzinfo=UTC)),))
    object.__setattr__(snapshot, "entries", entries)
    backend.read.get_scheduler_queue = lambda _profile: snapshot

    result = GameMcpAdapter(lambda: backend).call(
        "game_get_scheduler_queue", {"profile": "alpha"}
    )

    assert result["code"] == "GAME_SERVICE_UNAVAILABLE"


def test_game_mcp_backend_dispose_invalidates_lazy_persistence_services() -> None:
    class _Persistence:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    persistence = _Persistence()
    backend = GameMcpBackend(
        instance_reader=object(),
        task_catalog=object(),
        config_reader=object(),
        log_reader=object(),
        screenshot_reader=object(),
        persistence_factory=lambda _environment: persistence,
    )
    fleet_state = backend.fleet_state
    morale = backend.morale
    assert backend._get_persistence() is persistence

    backend.dispose()

    assert persistence.disposed is True
    with pytest.raises(ServiceUnavailableError):
        _ = backend.fleet_state
    with pytest.raises(ServiceUnavailableError):
        _ = backend.morale
    with pytest.raises(ServiceUnavailableError):
        fleet_state._uow_factory()
    with pytest.raises(ServiceUnavailableError):
        morale._uow_factory()


def test_adapter_preserves_unknown_morale_state() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    morale = adapter.call("game_get_morale", {"profile": "alpha", "fleet_indices": [1]})
    assert morale["code"] == "GAME_DATA_UNKNOWN"
    assert morale["state"] == "unknown"
    slot = morale["details"]["fleets"][0]["slots"][0]
    assert slot["knowledge"] == "unknown"
    assert slot["baseline"] is None


def test_adapter_preserves_unknown_current_task_state() -> None:
    backend = _backend()
    backend.read.get_current_running_task = lambda _profile: CurrentTaskSnapshot(
        "alpha", UNKNOWN_TASK
    )
    adapter = GameMcpAdapter(lambda: backend)

    result = adapter.call("game_get_current_task", {"profile": "alpha"})

    assert result["ok"] is True
    assert result["code"] == "GAME_DATA_UNKNOWN"
    assert result["state"] == "unknown"
    assert result["details"] == {"profile": "alpha", "task": UNKNOWN_TASK}


def test_adapter_does_not_claim_empty_fleet_snapshots_are_complete() -> None:
    backend = _backend()

    def empty_state(_profile: str, selection: FleetSelection) -> FleetStateResult:
        return FleetStateResult(
            FleetStateRequest(selection, FleetRefreshPolicy.NEVER),
            (),
            selection.fleet_indices,
        )

    backend.fleet_state.state_read_only = empty_state
    adapter = GameMcpAdapter(lambda: backend)

    result = adapter.call(
        "game_get_fleet_state", {"profile": "alpha", "fleet_indices": [1]}
    )

    assert result["code"] == "GAME_DATA_UNKNOWN"
    assert result["details"]["snapshots_complete"] is False


def test_screenshot_response_is_bounded_native_image_content() -> None:
    adapter = GameMcpAdapter(lambda: _backend())
    response = adapter.call("game_get_screenshot", {"profile": "alpha"})
    assert isinstance(response, GameMcpResponse)
    assert response.mime_type == "image/png"
    assert response.image == _png_1x1()
    assert response.structured["details"]["profile"] == "alpha"
    encoded = json.dumps(response.structured, ensure_ascii=False)
    assert base64.b64encode(response.image).decode("ascii") not in encoded


def test_fleet_state_read_service_does_not_register_or_commit() -> None:
    _, instance_id = runtime_instance_identity("alpha")
    observation = FleetStateObservation(
        id=uuid4(),
        run_id=uuid4(),
        instance_id=instance_id,
        idempotency_key="test:alpha:1",
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        snapshot=_empty_snapshot(1),
    )

    class _Uow:
        def __init__(self) -> None:
            self.commits = 0
            self.registers = 0
            self.instances = self
            self.fleet_state = self

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def resolve(self, **_kwargs: object) -> InstanceIdentity:
            return InstanceIdentity(instance_id, "alpha")

        def latest(
            self, _instance_id: UUID, _selection: FleetSelection
        ) -> tuple[FleetStateObservation, ...]:
            return (observation,)

        def register(self, *_args: object, **_kwargs: object) -> bool:
            self.registers += 1
            return True

        def commit(self) -> None:
            self.commits += 1

    uow = _Uow()
    result = FleetStateReadService(lambda: uow).state_read_only(
        "alpha", FleetSelection.one(1)
    )
    assert result.observations == (observation,)
    assert uow.commits == 0
    assert uow.registers == 0


def test_stdio_entrypoint_exposes_game_contract_and_tools() -> None:
    if shutil.which(GAME_MCP_COMMAND) is None:
        pytest.skip("uv недоступен в окружении локального stdio acceptance")

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=GAME_MCP_COMMAND,
            args=list(GAME_MCP_ARGS),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        with anyio.fail_after(60):
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                assert (await session.list_tools()).tools[0].name == "game_get_contract"
                result = await session.call_tool("game_get_contract", {})
                assert result.structured_content["code"] == "GAME_MCP_CONTRACT_READY"
                assert (
                    result.structured_content["details"]["contract"][
                        "game_mcp_api_version"
                    ]
                    == 1
                )
                assert result.is_error is False

    asyncio.run(scenario())


def test_stdio_entrypoint_accepts_2026_self_describing_requests_without_initialize() -> None:
    if shutil.which(GAME_MCP_COMMAND) is None:
        pytest.skip("uv недоступен в окружении локального stdio acceptance")

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=GAME_MCP_COMMAND,
            args=list(GAME_MCP_ARGS),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        request_meta = {
            PROTOCOL_VERSION_META_KEY: "2026-07-28",
            CLIENT_INFO_META_KEY: {"name": "game-mcp-regression", "version": "1"},
            CLIENT_CAPABILITIES_META_KEY: {},
        }

        async def request(
            read_stream: Any,
            write_stream: Any,
            request_id: int,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            await write_stream.send(
                SessionMessage(
                    JSONRPCRequest(
                        jsonrpc="2.0",
                        id=request_id,
                        method=method,
                        params=params,
                    )
                )
            )
            while True:
                envelope = await read_stream.receive()
                message = (
                    envelope.message
                    if isinstance(envelope, SessionMessage)
                    else envelope
                )
                if getattr(message, "id", None) == request_id:
                    break
            assert getattr(message, "error", None) is None
            result = getattr(message, "result", None)
            if hasattr(result, "model_dump"):
                result = result.model_dump(by_alias=True)
            assert isinstance(result, dict)
            return result

        with anyio.fail_after(60):
            async with stdio_client(parameters) as (read_stream, write_stream):
                tools_result = await request(
                    read_stream,
                    write_stream,
                    1,
                    "tools/list",
                    {"_meta": request_meta},
                )
                assert tools_result["tools"][0]["name"] == "game_get_contract"
                call_result = await request(
                    read_stream,
                    write_stream,
                    2,
                    "tools/call",
                    {
                        "name": "game_get_contract",
                        "arguments": {},
                        "_meta": request_meta,
                    },
                )
                assert call_result["structuredContent"]["code"] == "GAME_MCP_CONTRACT_READY"

    asyncio.run(scenario())
