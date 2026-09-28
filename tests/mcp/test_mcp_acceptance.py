from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.integrations.contracts import IntegrationState
from azurpilot.integrations.mcp_client import (
    FreshMcpClientResult,
    HttpTransportPolicy,
    accept_fresh_http,
)
from dev_tools import mcp_acceptance


def test_game_acceptance_plan_is_target_neutral() -> None:
    plan = mcp_acceptance.build_plan("a" * 40, "azurpilot-game")

    assert plan.contract_tool == "game_get_contract"
    assert plan.required_read_only_calls == (("game_list_profiles", {}),)
    assert plan.call_plan.required_tools == frozenset(
        {"game_get_contract", "game_list_profiles"}
    )


@pytest.mark.parametrize(
    ("server_name", "version"),
    (("azurpilot-dev", "8.9.0"), ("azurpilot-game", "2.9.0")),
)
def test_acceptance_plan_uses_current_manifest_version(
    monkeypatch: pytest.MonkeyPatch, server_name: str, version: str
) -> None:
    monkeypatch.setattr(
        mcp_acceptance,
        "server_version",
        lambda candidate: version if candidate == server_name else "0.0.0",
    )

    plan = mcp_acceptance.build_plan("a" * 40, server_name)

    from module.mcp_shared.catalog import contract_revision

    assert plan.expected_contract["server_version"] == version
    assert plan.expected_contract["contract_revision"] == contract_revision(
        plan.expected_contract
    )


class _FreshSession:
    def __init__(self, plan, *, fail_tool: str | None = None) -> None:
        self.plan = plan
        self.fail_tool = fail_tool

    async def initialize(self):
        contract = self.plan.expected_contract
        return SimpleNamespace(
            protocol_version="2025-03-26",
            server_info=SimpleNamespace(
                name=contract["server_name"],
                version=contract["server_version"],
            ),
        )

    async def list_tools(self):
        if self.plan.contract_tool == "game_get_contract":
            from module.game_mcp.server import tool_definitions
        else:
            from module.dev_mcp.server import tool_definitions

        return SimpleNamespace(tools=list(tool_definitions()))

    async def call_tool(self, name: str, _arguments: dict[str, object]):
        if name == self.fail_tool:
            return SimpleNamespace(is_error=True, structured_content=None, content=[])
        if name == self.plan.contract_tool:
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "ok": True,
                    "details": {"contract": self.plan.expected_contract},
                },
                content=[],
            )
        return SimpleNamespace(
            is_error=False,
            structured_content={"ok": True},
            content=[],
        )


def test_game_acceptance_calls_profile_list_and_records_it(
    monkeypatch,
) -> None:
    plan = mcp_acceptance.build_plan("a" * 40, "azurpilot-game")
    session = _FreshSession(plan)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_http_session(**_kwargs):
        yield session

    monkeypatch.setattr(
        "azurpilot.integrations.mcp_client._http_session", fake_http_session
    )

    result = asyncio.run(
        accept_fresh_http(
            endpoint="http://127.0.0.1:8776/mcp",
            headers={"Authorization": "Bearer test"},
            plan=plan,
            timeout_seconds=1,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        )
    )

    assert result.state is IntegrationState.READY
    assert result.called_tools == ("game_get_contract", "game_list_profiles")


def test_game_acceptance_fails_when_profile_list_call_fails(monkeypatch) -> None:
    plan = mcp_acceptance.build_plan("a" * 40, "azurpilot-game")
    session = _FreshSession(plan, fail_tool="game_list_profiles")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_http_session(**_kwargs):
        yield session

    monkeypatch.setattr(
        "azurpilot.integrations.mcp_client._http_session", fake_http_session
    )

    result = asyncio.run(
        accept_fresh_http(
            endpoint="http://127.0.0.1:8776/mcp",
            headers={"Authorization": "Bearer test"},
            plan=plan,
            timeout_seconds=1,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        )
    )

    assert result.state is IntegrationState.UNAVAILABLE
    assert result.reason_code == "MCP_FRESH_CLIENT_READ_ONLY_CALL_FAILED"
    assert result.called_tools == ("game_get_contract", "game_list_profiles")


def test_dev_acceptance_keeps_its_read_only_call(monkeypatch) -> None:
    plan = mcp_acceptance.build_plan("a" * 40, "azurpilot-dev")
    session = _FreshSession(plan)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_http_session(**_kwargs):
        yield session

    monkeypatch.setattr(
        "azurpilot.integrations.mcp_client._http_session", fake_http_session
    )

    result = asyncio.run(
        accept_fresh_http(
            endpoint="http://127.0.0.1:8775/mcp",
            headers={"Authorization": "Bearer test"},
            plan=plan,
            timeout_seconds=1,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        )
    )

    assert plan.call_plan.required_tools == frozenset(
        {"dev_get_contract", "dev_list_smoke_capabilities"}
    )
    assert result.state is IntegrationState.READY
    assert result.called_tools == (
        "dev_get_contract",
        "dev_list_smoke_capabilities",
    )


def test_combined_acceptance_reports_game_family_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        mcp_acceptance, "git_source_snapshot", lambda _root: ("a" * 40, "clean")
    )
    monkeypatch.setattr(
        mcp_acceptance, "local_http_headers", lambda *_args: {"Authorization": "Bearer test"}
    )

    async def accept_by_family(**kwargs) -> FreshMcpClientResult:
        plan = kwargs["plan"]
        if plan.contract_tool == "game_get_contract":
            return FreshMcpClientResult(
                state=IntegrationState.UNAVAILABLE,
                reason_code="MCP_FRESH_CLIENT_READ_ONLY_CALL_FAILED",
                called_tools=("game_get_contract", "game_list_profiles"),
                diagnostics=("game_list_profiles:transport_error",),
            )
        return FreshMcpClientResult(
            state=IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_ACCEPTANCE_READY",
            called_tools=("dev_get_contract", "dev_list_smoke_capabilities"),
        )

    monkeypatch.setattr(mcp_acceptance, "accept_fresh_http", accept_by_family)

    result = asyncio.run(mcp_acceptance.accept(tmp_path))

    assert result.state is IntegrationState.UNAVAILABLE
    assert result.reason_code == "MCP_FRESH_CLIENT_READ_ONLY_CALL_FAILED"
    assert result.called_tools == (
        "dev_get_contract",
        "dev_list_smoke_capabilities",
        "game_get_contract",
        "game_list_profiles",
    )
    assert "azurpilot-dev:ready" in result.diagnostics
    assert "game_list_profiles:transport_error" in result.diagnostics


def test_combined_acceptance_preserves_unknown_credential_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        mcp_acceptance, "git_source_snapshot", lambda _root: ("a" * 40, "clean")
    )

    def unknown_credential(*_args, **_kwargs):
        raise mcp_acceptance.LocalHttpAuthUnknownError("LOCAL_MCP_AUTH_UNKNOWN")

    monkeypatch.setattr(mcp_acceptance, "local_http_headers", unknown_credential)
    monkeypatch.setattr(
        mcp_acceptance,
        "accept_fresh_http",
        lambda **_kwargs: pytest.fail("при неизвестном состоянии учётных данных новый HTTP-сеанс должен блокироваться"),
    )

    result = asyncio.run(mcp_acceptance.accept(tmp_path))

    assert result.state is IntegrationState.UNKNOWN
    assert result.reason_code == "MCP_PROJECT_LOCAL_CREDENTIAL_UNKNOWN"
    assert "azurpilot-dev:credential_unknown" in result.diagnostics


def test_standalone_acceptance_still_fails_closed_for_dirty_source(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        mcp_acceptance,
        "git_source_snapshot",
        lambda _root: ("a" * 40, "modified"),
    )
    monkeypatch.setattr(
        mcp_acceptance,
        "accept_fresh_http",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("автономная приёмка не должна запускаться из изменённого исходного дерева")
        ),
    )

    result = asyncio.run(mcp_acceptance.accept(tmp_path))

    assert result.state is IntegrationState.INCOMPATIBLE
    assert result.reason_code == "MCP_FRESH_CLIENT_SOURCE_NOT_CLEAN"
    assert result.diagnostics == ("working_tree_modified",)


def test_sync_acceptance_checks_the_dirty_candidate_with_a_fresh_client(
    tmp_path: Path, monkeypatch
) -> None:
    source_revision = "a" * 40
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        mcp_acceptance,
        "git_source_snapshot",
        lambda _root: (source_revision, "modified"),
    )
    monkeypatch.setattr(mcp_acceptance, "local_http_headers", lambda *_args: {"Authorization": "Bearer test"})

    async def accept_fresh_http(**kwargs) -> FreshMcpClientResult:
        calls.append(kwargs)
        called_tools = (
            ("game_get_contract", "game_list_profiles")
            if kwargs["endpoint"] == mcp_acceptance.LOCAL_HTTP_ENDPOINTS["azurpilot-game"]
            else ("dev_get_contract", "dev_list_smoke_capabilities")
        )
        return FreshMcpClientResult(
            state=IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_READY",
            initialized=True,
            source_revision=source_revision,
            called_tools=called_tools,
        )

    monkeypatch.setattr(mcp_acceptance, "accept_fresh_http", accept_fresh_http)

    result = asyncio.run(mcp_acceptance.accept(tmp_path, allow_dirty=True))

    assert result.state is IntegrationState.READY
    assert result.reason_code == "MCP_FRESH_CLIENT_READY"
    assert result.diagnostics == ("working_tree_modified", "azurpilot-game:ready")
    assert result.called_tools == (
        "dev_get_contract",
        "dev_list_smoke_capabilities",
        "game_get_contract",
        "game_list_profiles",
    )
    assert len(calls) == 2
    assert calls[0]["endpoint"] == mcp_acceptance.LOCAL_HTTP_ENDPOINTS["azurpilot-dev"]
    assert calls[0]["plan"].expected_contract["source_revision"] == source_revision


def test_working_tree_marker_is_kept_when_diagnostics_reach_the_limit(
    tmp_path: Path, monkeypatch
) -> None:
    source_revision = "a" * 40
    diagnostics = tuple(f"existing_{index}" for index in range(16))
    monkeypatch.setattr(
        mcp_acceptance,
        "git_source_snapshot",
        lambda _root: (source_revision, "modified"),
    )
    monkeypatch.setattr(mcp_acceptance, "local_http_headers", lambda *_args: {"Authorization": "Bearer test"})

    async def accept_fresh_http(**_kwargs) -> FreshMcpClientResult:
        return FreshMcpClientResult(
            state=IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_READY",
            initialized=True,
            source_revision=source_revision,
            called_tools=("dev_get_contract", "dev_list_smoke_capabilities"),
            diagnostics=diagnostics,
        )

    monkeypatch.setattr(mcp_acceptance, "accept_fresh_http", accept_fresh_http)

    result = asyncio.run(mcp_acceptance.accept(tmp_path, allow_dirty=True))

    assert result.diagnostics[0] == "working_tree_modified"
    assert len(result.diagnostics) == 16
    assert result.diagnostics[1:] == diagnostics[:15]


def test_sync_acceptance_fails_closed_when_git_snapshot_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        mcp_acceptance,
        "git_source_snapshot",
        lambda _root: ("unknown", "unknown"),
    )
    monkeypatch.setattr(
        mcp_acceptance,
        "accept_fresh_http",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("при неизвестной идентичности источника клиент запускаться не должен")
        ),
    )

    result = asyncio.run(mcp_acceptance.accept(tmp_path, allow_dirty=True))

    assert result.state is IntegrationState.INCOMPATIBLE
    assert result.reason_code == "MCP_FRESH_CLIENT_SOURCE_UNKNOWN"
    assert result.diagnostics == ("working_tree_unknown",)
