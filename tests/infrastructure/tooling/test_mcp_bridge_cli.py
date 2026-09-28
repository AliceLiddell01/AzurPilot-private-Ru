from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

import azurpilot.cli as tooling_cli
import azurpilot.integrations.mcp_client as mcp_client_module
import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.cli import build_parser
from azurpilot.tooling.contracts import (
    McpAcceptanceDetails,
    McpBridgeAcceptanceDetails,
    McpBridgeProcessStatus,
    McpBridgeStatusDetails,
    McpBridgeUpstreamStatus,
    ToolingResult,
)
from azurpilot.tooling.mcp import McpService
from azurpilot.tooling.mcp_errors import ToolingError
from azurpilot.tooling.result import OperationState, ResultCode
from dev_tools import mcp_status
from module.mcp_shared import windows_mcp_bridge_contract
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_IDENTITY_PROTOCOL,
    BridgeSourceIdentity,
)


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


def test_bridge_acceptance_checks_legacy_and_modern_sessions_on_each_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = McpService()
    monkeypatch.setattr(mcp_status, "git_source_snapshot", lambda _root: ("a" * 40, "clean"))
    monkeypatch.setattr(
        "module.mcp_shared.local_http_auth.read_local_mcp_bridge_caller_token",
        lambda _root: "bridge-caller-token",
    )
    service._bridge_status_result = lambda _root: SimpleNamespace(  # type: ignore[method-assign]
        ok=True, code=ResultCode.OK, message="Мост готов.", details=object()
    )
    service._bundle = lambda _root: SimpleNamespace(  # type: ignore[method-assign]
        servers={
            name: SimpleNamespace(
                version="1.2.3",
                source_set_digest="b" * 64,
                contract_revision="c" * 64,
                tool_catalog_sha256="d" * 64,
                capability_catalog_sha256="e" * 64,
            )
            for name in ("azurpilot-dev", "azurpilot-game")
        }
    )
    calls: list[tuple[str, str, str]] = []

    async def accept_client(**kwargs):
        endpoint = kwargs["endpoint"]
        plan = kwargs["plan"]
        protocol = "2026-07-28" if kwargs.pop("modern", False) else "2025-11-25"
        server_name = plan.expected_contract["server_name"]
        calls.append((endpoint, server_name, protocol))
        return mcp_client_module.FreshMcpClientResult(
            state=mcp_client_module.IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_ACCEPTANCE_READY",
            initialized=protocol != "2026-07-28",
            protocol_version=protocol,
            server_name=server_name,
            server_version="1.2.3",
            source_revision="a" * 40,
            tool_count=12,
            tool_catalog_sha256="d" * 64,
            capability_catalog_sha256="e" * 64,
            contract_revision="c" * 64,
            called_tools=(
                plan.contract_tool,
                *(name for name, _arguments in plan.required_read_only_calls),
            ),
        )

    async def accept_legacy(**kwargs):
        return await accept_client(**kwargs)

    async def accept_modern(**kwargs):
        kwargs["modern"] = True
        return await accept_client(**kwargs)

    monkeypatch.setattr(mcp_client_module, "accept_fresh_http", accept_legacy)
    monkeypatch.setattr(
        mcp_client_module, "accept_fresh_http_modern", accept_modern
    )

    result = service._bridge_accept(tmp_path)

    assert result.ok is True
    assert calls == [
        ("http://127.0.0.1:8780/dev/mcp", "azurpilot-dev", "2025-11-25"),
        ("http://127.0.0.1:8780/dev/mcp", "azurpilot-dev", "2026-07-28"),
        ("http://127.0.0.1:8780/game/mcp", "azurpilot-game", "2025-11-25"),
        ("http://127.0.0.1:8780/game/mcp", "azurpilot-game", "2026-07-28"),
    ]
    assert tuple(item.protocol_version for item in result.details.routes) == (
        "2025-11-25",
        "2025-11-25",
    )
    assert tuple(item.protocol_version for item in result.details.modern_routes) == (
        "2026-07-28",
        "2026-07-28",
    )


def test_bridge_status_dto_tracks_the_canonical_endpoint_and_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = dict(windows_mcp_bridge_contract.BRIDGE_ROUTES)
    routes["dev"] = replace(routes["dev"], path="/development/mcp")
    monkeypatch.setattr(
        windows_mcp_bridge_contract, "BRIDGE_ROUTES", MappingProxyType(routes)
    )
    monkeypatch.setattr(windows_mcp_bridge_contract, "BRIDGE_PORT", 8879)

    details = McpBridgeStatusDetails(
        action="status",
        state="ready",
        endpoint=windows_mcp_bridge_contract.bridge_endpoint(),
        routes=tuple(route.path for route in routes.values()),
        caller_authentication="configured",
        process=McpBridgeProcessStatus(),
        upstreams=tuple(
            McpBridgeUpstreamStatus(
                route=family,
                server_name=route.server_name,
                status="ready",
                reason_code="BRIDGE_UPSTREAM_READY",
            )
            for family, route in routes.items()
        ),
        reason_code="BRIDGE_READY",
    )

    assert details.endpoint == "http://127.0.0.1:8879"
    assert details.routes == ("/development/mcp", "/game/mcp")

    with pytest.raises(ValueError, match="каноническому контракту"):
        McpBridgeStatusDetails(
            action="status",
            state="ready",
            endpoint="http://127.0.0.1:8780",
            routes=details.routes,
            caller_authentication="configured",
            process=McpBridgeProcessStatus(),
            upstreams=details.upstreams,
            reason_code="BRIDGE_READY",
        )


def test_bridge_acceptance_human_output_reports_legacy_and_modern_protocols() -> None:
    details = McpBridgeAcceptanceDetails(
        acceptance_state="READY",
        reason_code="WINDOWS_MCP_BRIDGE_ACCEPTANCE_READY",
        routes=tuple(
            McpAcceptanceDetails(
                acceptance_state="READY",
                reason_code="MCP_FRESH_CLIENT_ACCEPTANCE_READY",
                initialized=True,
                protocol_version="2025-11-25",
                server_name=name,
                called_tools=("get_contract",),
            )
            for name in ("azurpilot-dev", "azurpilot-game")
        ),
        modern_routes=tuple(
            McpAcceptanceDetails(
                acceptance_state="READY",
                reason_code="MCP_FRESH_CLIENT_ACCEPTANCE_READY",
                initialized=False,
                protocol_version="2026-07-28",
                server_name=name,
                called_tools=("get_contract",),
            )
            for name in ("azurpilot-dev", "azurpilot-game")
        ),
    )
    result = ToolingResult(
        ok=True,
        code=ResultCode.OK,
        state=OperationState.READY,
        message="Приёмка завершена.",
        details=details,
    )
    stdout = io.StringIO()

    tooling_cli._render_human(
        result,
        stdout,
        io.StringIO(),
        no_color=True,
        verbose=False,
    )

    output = stdout.getvalue()
    assert "Совместимость" in output
    assert "Современный (auto)" in output
    assert "2025-11-25" in output
    assert "2026-07-28" in output
    assert output.count("azurpilot-dev") == 2
    assert output.count("azurpilot-game") == 2


def test_bridge_acceptance_dto_rejects_duplicate_servers_and_inconsistent_ready_state() -> None:
    def route_result(server_name: str, state: str = "READY") -> McpAcceptanceDetails:
        return McpAcceptanceDetails(
            acceptance_state=state,
            reason_code="MCP_FRESH_CLIENT_ACCEPTANCE_READY",
            server_name=server_name,
        )

    with pytest.raises(ValueError, match="каноническим маршрутам"):
        McpBridgeAcceptanceDetails(
            acceptance_state="READY",
            reason_code="WINDOWS_MCP_BRIDGE_ACCEPTANCE_READY",
            routes=(route_result("azurpilot-dev"), route_result("azurpilot-dev")),
            modern_routes=(
                route_result("azurpilot-dev"),
                route_result("azurpilot-game"),
            ),
        )

    with pytest.raises(ValueError, match="должно соответствовать результатам"):
        McpBridgeAcceptanceDetails(
            acceptance_state="READY",
            reason_code="WINDOWS_MCP_BRIDGE_ACCEPTANCE_READY",
            routes=(route_result("azurpilot-dev"), route_result("azurpilot-game")),
            modern_routes=(
                route_result("azurpilot-dev"),
                route_result("azurpilot-game", "UNAVAILABLE"),
            ),
        )


def test_bridge_upstream_dto_rejects_identity_from_another_server_family() -> None:
    identity = BridgeSourceIdentity(
        identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
        server_name="azurpilot-game",
        server_version="1.2.3",
        source_revision="a" * 40,
        source_set_digest="b" * 64,
        contract_revision="c" * 64,
        tool_catalog_sha256="d" * 64,
        capability_catalog_sha256="e" * 64,
    )

    with pytest.raises(ValueError, match="каноническому маршруту"):
        McpBridgeUpstreamStatus(
            route="dev",
            server_name="azurpilot-dev",
            status="ready",
            identity=identity,
            reason_code="BRIDGE_UPSTREAM_READY",
        )


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
