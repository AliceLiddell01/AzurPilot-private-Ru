from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import azurpilot.cli as tooling_cli
import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.cli import build_parser
from azurpilot.tooling.contracts import ToolingResult
from azurpilot.tooling.mcp import McpService
from azurpilot.tooling.mcp_errors import ToolingError
from azurpilot.tooling.result import OperationState, ResultCode
from dev_tools import mcp_status


def test_cli_exposes_mcp_bridge_accept_and_configure_routes() -> None:
    parser = build_parser()

    bridge_accept = parser.parse_args(["mcp", "bridge", "accept", "--json"])
    assert bridge_accept.mcp_command == "bridge"
    assert bridge_accept.mcp_bridge_command == "accept"
    assert bridge_accept.json is True

    bridge_configure = parser.parse_args(
        ["mcp", "bridge", "configure", "--stdin-token"]
    )
    assert bridge_configure.mcp_bridge_command == "configure"
    assert bridge_configure.stdin_token is True


def test_cli_routes_bridge_actions_and_reads_configured_token_only_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class McpStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def bridge(
            self,
            action: str,
            root: str | None,
            *,
            caller_token: str | None = None,
        ) -> ToolingResult:
            self.calls.append((action, caller_token))
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Мост Windows MCP готов.",
            )

    mcp = McpStub()
    monkeypatch.setattr(tooling_cli.sys, "stdin", io.StringIO("a" * 48 + "\n"))
    result = tooling_cli._dispatch(
        build_parser().parse_args(["mcp", "bridge", "configure", "--stdin-token"]),
        SimpleNamespace(mcp=mcp),
    )

    assert result.ok is True
    assert mcp.calls == [("configure", "a" * 48)]


def test_windows_bridge_rejects_non_windows_platform_before_repository_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = McpService()
    monkeypatch.setattr(mcp_tooling, "_supports_windows_mcp_bridge", lambda: False)

    with pytest.raises(ToolingError) as error:
        service.bridge("status")

    assert error.value.code is ResultCode.TOOLING_CAPABILITY_UNSUPPORTED
    assert str(error.value) == "Мост Windows MCP доступен только в Windows."


def test_bridge_acceptance_requires_clean_committed_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = McpService()
    monkeypatch.setattr(
        mcp_status, "git_source_snapshot", lambda _root: ("a" * 40, "modified")
    )

    with pytest.raises(ToolingError) as error:
        service._bridge_accept(tmp_path)

    assert error.value.code is ResultCode.MCP_PLUGIN_RUNTIME_INCOMPATIBLE
    assert "чистой рабочей копии" in str(error.value)


def test_bridge_restart_targets_only_the_bridge_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from module.mcp_shared.local_http_supervisor import WINDOWS_MCP_BRIDGE_SERVICE

    service = McpService()
    monkeypatch.setattr(mcp_tooling, "_supports_windows_mcp_bridge", lambda: True)
    monkeypatch.setattr(service, "_root", lambda _root: tmp_path)
    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(mcp_tooling, "project_python", lambda _root: python)

    configured_services: list[tuple[object, ...]] = []

    class BridgeSupervisor:
        def __init__(self, *, services: tuple[object, ...]) -> None:
            configured_services.append(services)

        def stop_result(self):
            return SimpleNamespace(ok=True, outcome=SimpleNamespace(value="stopped"))

    monkeypatch.setattr(
        service,
        "_bridge_supervisor",
        lambda _root: BridgeSupervisor(services=(WINDOWS_MCP_BRIDGE_SERVICE,)),
    )

    backend_pids = {"azurpilot-dev": 321, "azurpilot-game": 654}
    states = iter(("stopped", "conflict", "conflict", "ready"))
    statuses = 0

    def bridge_status(_root, *, action):
        nonlocal statuses
        statuses += 1
        state = next(states)
        return SimpleNamespace(
            ok=state == "ready",
            details=SimpleNamespace(
                action=action,
                state=state,
                caller_authentication="configured",
                process=SimpleNamespace(process_pid=987),
                upstreams=backend_pids.copy(),
            ),
        )

    monkeypatch.setattr(service, "_bridge_status_result", bridge_status)
    started_specs = []

    class BridgeProcess:
        identity = object()

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        service.runner,
        "start",
        lambda spec: started_specs.append(spec) or BridgeProcess(),
    )

    result = service.bridge("restart")

    assert result.ok is True
    assert configured_services == [(WINDOWS_MCP_BRIDGE_SERVICE,)]
    assert len(started_specs) == 1
    assert started_specs[0].argv[4:6] == ("--service", WINDOWS_MCP_BRIDGE_SERVICE.name)
    assert result.details.upstreams == backend_pids
    assert statuses == 4
