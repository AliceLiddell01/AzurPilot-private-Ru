from __future__ import annotations

import asyncio
from pathlib import Path

from azurpilot.integrations.contracts import IntegrationState
from azurpilot.integrations.mcp_client import FreshMcpClientResult
from dev_tools import mcp_acceptance


def test_game_acceptance_plan_is_target_neutral() -> None:
    plan = mcp_acceptance.build_plan("a" * 40, "azurpilot-game")

    assert plan.contract_tool == "game_get_contract"
    assert plan.required_read_only_calls == ()
    assert plan.call_plan.required_tools == frozenset({"game_get_contract"})


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
            AssertionError("standalone acceptance не должен запускаться из изменённого источника")
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
        return FreshMcpClientResult(
            state=IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_READY",
            initialized=True,
            source_revision=source_revision,
            called_tools=("dev_get_contract", "dev_list_smoke_capabilities"),
        )

    monkeypatch.setattr(mcp_acceptance, "accept_fresh_http", accept_fresh_http)

    result = asyncio.run(mcp_acceptance.accept(tmp_path, allow_dirty=True))

    assert result.state is IntegrationState.READY
    assert result.reason_code == "MCP_FRESH_CLIENT_READY"
    assert result.diagnostics == ("working_tree_modified", "azurpilot-game:ready")
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
            AssertionError("неизвестная identity источника не должна запускать клиента")
        ),
    )

    result = asyncio.run(mcp_acceptance.accept(tmp_path, allow_dirty=True))

    assert result.state is IntegrationState.INCOMPATIBLE
    assert result.reason_code == "MCP_FRESH_CLIENT_SOURCE_UNKNOWN"
    assert result.diagnostics == ("working_tree_unknown",)
