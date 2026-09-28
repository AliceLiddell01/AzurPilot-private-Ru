from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.tooling.contracts import McpLifecycleDetails, McpReconcileDetails
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.process import ProcessStartError
from azurpilot.tooling.result import ResultCode
from module.mcp_shared.versioning import load_mcp_bundle
from tests.support.paths import REPOSITORY_ROOT


def _runtime_service(bundle, name: str, *, ready: bool) -> dict[str, object]:
    server = bundle.servers[name]
    return {
        "server_name": name,
        "ready": ready,
        "server_version": server.version,
        "source_set_digest": server.source_set_digest,
        "tool_catalog_sha256": server.tool_catalog_sha256,
        "capability_catalog_sha256": server.capability_catalog_sha256,
        "contract_revision": server.contract_revision,
        "source_revision": "a" * 40,
    }


def _runtime_status(
    bundle,
    ready_names: set[str],
    *,
    stale_names: set[str] | None = None,
) -> tuple[str, dict[str, object]]:
    stale = stale_names or set()
    services = [
        _runtime_service(bundle, name, ready=name in ready_names)
        for name in mcp_tooling.MCP_SERVER_NAMES
    ]
    supervisors = {
        name: {
            "code": (
                "LOCAL_MCP_SUPERVISOR_READY"
                if name in ready_names
                else "LOCAL_MCP_SUPERVISOR_STALE"
                if name in stale
                else "LOCAL_MCP_SUPERVISOR_STOPPED"
            )
        }
        for name in mcp_tooling.MCP_SERVER_NAMES
    }
    state = (
        "ready"
        if len(ready_names) == len(mcp_tooling.MCP_SERVER_NAMES)
        else "stale"
        if stale or ready_names
        else "stopped"
    )
    return state, {
        "ok": state == "ready",
        "code": "LOCAL_MCP_SUPERVISOR_READY"
        if state == "ready"
        else "MCP_RUNTIME_CONTRACT_DRIFT"
        if state == "stale"
        else "LOCAL_MCP_SUPERVISOR_STOPPED",
        "services": services,
        "supervisors": supervisors,
    }


def _configure_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    available_credentials: set[str],
    initial_ready: set[str],
):
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    service = mcp_tooling.McpService()
    python = tmp_path / "python.exe"
    python.touch()
    start_calls: list[str] = []
    stopped_names: list[str] = []

    class FakeProcess:
        identity = object()

        @staticmethod
        def poll() -> None:
            return None

    class FakeGitClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def head() -> str:
            return "a" * 40

    def start_process(spec: mcp_tooling.ProcessSpec) -> FakeProcess:
        start_calls.append(spec.argv[-1])
        return FakeProcess()

    service.runner = SimpleNamespace(start=start_process)
    monkeypatch.setattr(service, "_root", lambda _root: REPOSITORY_ROOT)
    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(service.source, "check", lambda _root: bundle)
    monkeypatch.setattr(
        service,
        "_auth_ready",
        lambda _root, names: all(name in available_credentials for name in names),
    )
    monkeypatch.setattr(mcp_tooling, "project_python", lambda _root: python)
    monkeypatch.setattr(mcp_tooling, "GitClient", FakeGitClient)

    def runtime_status(_root: Path, _bundle: object):
        ready_names = (initial_ready - set(stopped_names)) | set(start_calls)
        return _runtime_status(bundle, ready_names)

    monkeypatch.setattr(service, "_runtime_status", runtime_status)
    monkeypatch.setattr(
        service,
        "_supervisor",
        lambda _root, _name: SimpleNamespace(
            status=lambda: {"code": "LOCAL_MCP_SUPERVISOR_READY"},
            port_conflicts=lambda: (),
        ),
    )
    monkeypatch.setattr(
        service,
        "_stop_owned_supervisor",
        lambda _root, name: stopped_names.append(name) or SimpleNamespace(),
    )
    return service, bundle, start_calls, stopped_names


@pytest.mark.parametrize(
    ("available_credentials", "expected_started"),
    (
        ({"azurpilot-dev"}, ("azurpilot-dev",)),
        ({"azurpilot-game"}, ("azurpilot-game",)),
        ({"azurpilot-dev", "azurpilot-game"}, ("azurpilot-dev", "azurpilot-game")),
        (set(), ()),
    ),
    ids=("dev-only", "game-only", "both", "neither"),
)
def test_start_isolates_missing_credentials_and_returns_typed_family_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_credentials: set[str],
    expected_started: tuple[str, ...],
) -> None:
    service, _bundle, start_calls, _stopped = _configure_service(
        monkeypatch,
        tmp_path,
        available_credentials=available_credentials,
        initial_ready=set(),
    )

    if len(available_credentials) == len(mcp_tooling.MCP_SERVER_NAMES):
        result = service.start(REPOSITORY_ROOT)
        assert result.ok
    else:
        with pytest.raises(ToolingError) as error:
            service.start(REPOSITORY_ROOT)
        assert error.value.code is ResultCode.MCP_AUTH_NOT_CONFIGURED
        details = error.value.details
        assert isinstance(details, McpLifecycleDetails)
        by_name = {status.server_name: status for status in details.services}
        for name in mcp_tooling.MCP_SERVER_NAMES:
            if name in available_credentials:
                assert by_name[name].status == "ready"
            else:
                assert by_name[name].status == "not_configured"
                assert by_name[name].reason_code == "MCP_AUTH_NOT_CONFIGURED"
        assert details.readiness_confirmed is False

    assert tuple(start_calls) == expected_started


@pytest.mark.parametrize(
    ("missing_name", "expected_started"),
    (
        ("azurpilot-dev", ("azurpilot-game",)),
        ("azurpilot-game", ("azurpilot-dev",)),
    ),
)
def test_restart_keeps_missing_credential_family_running_and_restarts_healthy_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
    expected_started: tuple[str, ...],
) -> None:
    available = set(mcp_tooling.MCP_SERVER_NAMES) - {missing_name}
    service, _bundle, start_calls, stopped = _configure_service(
        monkeypatch,
        tmp_path,
        available_credentials=available,
        initial_ready=set(mcp_tooling.MCP_SERVER_NAMES),
    )

    with pytest.raises(ToolingError) as error:
        service.restart(REPOSITORY_ROOT)

    assert error.value.code is ResultCode.MCP_AUTH_NOT_CONFIGURED
    assert isinstance(error.value.details, McpLifecycleDetails)
    by_name = {status.server_name: status for status in error.value.details.services}
    healthy_name = next(iter(available))
    assert stopped == [healthy_name]
    assert tuple(start_calls) == expected_started
    assert by_name[healthy_name].status == "ready"
    assert by_name[missing_name].status == "not_configured"
    assert error.value.details.readiness_confirmed is False


@pytest.mark.parametrize(
    ("missing_name", "expected_repaired"),
    (
        ("azurpilot-dev", "azurpilot-game"),
        ("azurpilot-game", "azurpilot-dev"),
    ),
)
def test_reconcile_repairs_healthy_family_when_other_credential_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
    expected_repaired: str,
) -> None:
    available = {expected_repaired}
    service, bundle, start_calls, stopped = _configure_service(
        monkeypatch,
        tmp_path,
        available_credentials=available,
        initial_ready={missing_name},
    )
    first_status = _runtime_status(
        bundle,
        {missing_name},
        stale_names={expected_repaired},
    )
    original_runtime_status = service._runtime_status
    call_count = 0

    def runtime_status(root: Path, current_bundle: object):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return first_status
        return original_runtime_status(root, current_bundle)

    monkeypatch.setattr(service, "_runtime_status", runtime_status)
    monkeypatch.setattr(service, "_session_state", lambda *_args, **_kwargs: "not_observable")

    with pytest.raises(ToolingError) as error:
        service.reconcile(REPOSITORY_ROOT)

    assert error.value.code is ResultCode.MCP_AUTH_NOT_CONFIGURED
    assert isinstance(error.value.details, McpReconcileDetails)
    details = error.value.details
    by_name = {status.server_name: status for status in details.services}
    assert stopped == [expected_repaired]
    assert start_calls == [expected_repaired]
    assert by_name[expected_repaired].status == "ready"
    assert by_name[missing_name].status == "not_configured"
    assert details.restarted_servers == (expected_repaired,)
    assert details.runtime_ready is False
    assert details.mutation_performed is True


def test_unconfirmed_process_cleanup_aborts_next_family_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _bundle, _start_calls, _stopped = _configure_service(
        monkeypatch,
        tmp_path,
        available_credentials=set(mcp_tooling.MCP_SERVER_NAMES),
        initial_ready=set(),
    )
    attempted: list[str] = []

    def uncertain_start(spec: mcp_tooling.ProcessSpec) -> object:
        attempted.append(spec.argv[-1])
        raise ProcessStartError(
            code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
            message="Не удалось подтвердить создание процесса.",
            spawn_state="unknown",
            cleanup_state="unknown",
        )

    service.runner = SimpleNamespace(start=uncertain_start)

    with pytest.raises(ProcessStartError) as error:
        service.start(REPOSITORY_ROOT)

    assert error.value.spawn_state == "unknown"
    assert error.value.cleanup_state == "unknown"
    assert attempted == ["azurpilot-dev"]


@pytest.mark.parametrize(
    ("available_credentials", "expected_attempted", "expected_authentication"),
    (
        (
            set(mcp_tooling.MCP_SERVER_NAMES),
            ["azurpilot-dev", "azurpilot-game"],
            "configured",
        ),
        ({"azurpilot-game"}, ["azurpilot-game"], "unavailable"),
    ),
    ids=("runtime-unavailable", "authentication-unavailable"),
)
def test_late_port_conflict_keeps_conflict_status_after_partial_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_credentials: set[str],
    expected_attempted: list[str],
    expected_authentication: str,
) -> None:
    service, bundle, _start_calls, _stopped = _configure_service(
        monkeypatch,
        tmp_path,
        available_credentials=available_credentials,
        initial_ready=set(),
    )
    attempted: list[str] = []

    def start_family(spec: mcp_tooling.ProcessSpec) -> object:
        name = spec.argv[-1]
        attempted.append(name)
        if name == "azurpilot-dev":
            raise ProcessStartError(
                code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                message="Процесс не был создан.",
                spawn_state="not_spawned",
                cleanup_state="absent",
            )
        return SimpleNamespace(poll=lambda: None, identity=object())

    service.runner = SimpleNamespace(start=start_family)
    initial = _runtime_status(bundle, set())
    conflicted = _runtime_status(bundle, {"azurpilot-game"})[1]
    conflicted["code"] = "TOOLING_PORT_CONFLICT"
    conflicted["supervisors"]["azurpilot-dev"]["code"] = "TOOLING_PORT_CONFLICT"
    status_calls = 0

    def runtime_status(_root: Path, _bundle: object):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return initial
        return "conflict", conflicted

    monkeypatch.setattr(service, "_runtime_status", runtime_status)

    with pytest.raises(ToolingError) as error:
        service.start(REPOSITORY_ROOT)

    assert error.value.code is ResultCode.TOOLING_PORT_CONFLICT
    assert isinstance(error.value.details, McpLifecycleDetails)
    by_name = {status.server_name: status for status in error.value.details.services}
    assert by_name["azurpilot-dev"].status == "conflict"
    assert by_name["azurpilot-dev"].reason_code == "TOOLING_PORT_CONFLICT"
    assert by_name["azurpilot-dev"].authentication == expected_authentication
    assert by_name["azurpilot-game"].status == "ready"
    assert attempted == expected_attempted

