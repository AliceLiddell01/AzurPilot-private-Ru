from __future__ import annotations

import asyncio
import base64
import json
import shutil
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import anyio
import pytest
from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCRequest
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

from module.application import (
    ConfigSnapshot,
    CurrentTaskSnapshot,
    DashboardResource,
    DashboardResources,
    FleetRefreshPolicy,
    FleetStateObservation,
    FleetStateReadService,
    FleetStateRequest,
    FleetStateResult,
    InstanceReference,
    InstanceStatus,
    MediaFrame,
    MoraleFleetState,
    MoraleKnowledge,
    MoraleSelectionState,
    MoraleSlotState,
    ResourceNotFoundError,
    RuntimeLogTail,
    RuntimeState,
    SchedulerEntry,
    SchedulerQueueSnapshot,
    ServiceUnavailableError,
    TaskArgumentMetadata,
    TaskGroupMetadata,
    TaskMetadata,
    TaskOption,
    TaskSummary,
)
from module.application.game_validation import UNKNOWN_TASK
from module.application.instance_identity import runtime_instance_identity
from module.application.storage_models import InstanceIdentity
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.game_mcp.adapter import GameMcpAdapter, GameMcpResponse, _result
from module.game_mcp.composition import GameMcpBackend
from module.game_mcp.contract import contract_payload
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


def _backend() -> SimpleNamespace:
    return SimpleNamespace(
        instances=_Instances(),
        tasks=_Tasks(),
        read=_Read(),
        fleet_state=_Fleet(),
        morale=_Morale(),
    )


def test_contract_and_tool_catalog_are_game_specific_and_read_only() -> None:
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
    ]
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert all(tool.annotations.destructive_hint is False for tool in tools)
    assert all(tool.annotations.idempotent_hint for tool in tools)
    assert all(
        tool.meta
        == {
            "securitySchemes": [{"type": "oauth2", "scopes": [GAME_MCP_REQUIRED_SCOPE]}]
        }
        for tool in tools
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


def test_output_schemas_are_scoped_to_their_tool_details() -> None:
    expected = {
        "game_get_contract": {"contract", "tool"},
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
        second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_result["value"]["code"] == "GAME_RESOURCES_READY"
    assert second_result["value"]["code"] == "GAME_TASKS_READY"


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
