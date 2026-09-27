"""Независимая read-only acceptance-сессия first-party Dev/Game MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from azurpilot.integrations.contracts import IntegrationState
from azurpilot.integrations.mcp_client import (
    FreshMcpClientPlan,
    FreshMcpClientResult,
    HttpTransportPolicy,
    McpCallPlan,
    accept_fresh_http,
)
from dev_tools.mcp_status import git_source_snapshot
from module.dev_mcp.contract import contract_payload
from module.dev_mcp.server import tool_definitions as dev_tool_definitions
from module.game_mcp.contract import contract_payload as game_contract_payload
from module.game_mcp.server import tool_definitions as game_tool_definitions
from module.mcp_shared.local_http_auth import (
    LOCAL_HTTP_ENDPOINTS,
    LocalHttpAuthError,
    local_http_headers,
)
from tools.paths import REPOSITORY_ROOT

FRESH_ACCEPTANCE_TIMEOUT_SECONDS = 20.0
# Статус runtime требует пользовательский target profile, который не входит в
# tracked checkout. Поэтому fresh client protocol gate проверяет target-neutral
# каталог Smoke, а готовность runtime подтверждается отдельным live workflow.
REQUIRED_READ_ONLY_CALLS = (
    ("dev_list_smoke_capabilities", {}),
)
GAME_REQUIRED_READ_ONLY_CALLS = (("game_list_profiles", {}),)


def build_plan(
    source_revision: str,
    server_name: str = "azurpilot-dev",
) -> FreshMcpClientPlan:
    """Собрать только канонические contract/catalog и ограниченные read-only calls."""

    if server_name == "azurpilot-dev":
        expected_contract = contract_payload()
        tool_descriptors = tuple(dev_tool_definitions())
        contract_tool = "dev_get_contract"
        required_read_only_calls = REQUIRED_READ_ONLY_CALLS
    elif server_name == "azurpilot-game":
        expected_contract = game_contract_payload()
        tool_descriptors = tuple(game_tool_definitions())
        contract_tool = "game_get_contract"
        required_read_only_calls = GAME_REQUIRED_READ_ONLY_CALLS
    else:  # pragma: no cover - closed server catalog
        raise ValueError("Неизвестный first-party MCP server")
    expected_contract["source_revision"] = source_revision
    expected_tools = frozenset(tool.name for tool in tool_descriptors)
    blocked_tools = frozenset(
        tool.name
        for tool in tool_descriptors
        if not getattr(tool.annotations, "read_only_hint", False)
    )
    required_tools = frozenset(
        {
            contract_tool,
            *(name for name, _ in required_read_only_calls),
        }
    )
    return FreshMcpClientPlan(
        call_plan=McpCallPlan(
            required_tools=required_tools,
            probe_tool=contract_tool,
            arguments={},
            blocked_tools=blocked_tools,
            expected_tools=expected_tools,
        ),
        contract_tool=contract_tool,
        expected_contract=expected_contract,
        required_read_only_calls=required_read_only_calls,
    )


def _result_payload(result: FreshMcpClientResult) -> dict[str, object]:
    return {
        "state": result.state.value,
        "reason_code": result.reason_code,
        "initialized": result.initialized,
        "protocol_version": result.protocol_version,
        "server_name": result.server_name,
        "server_version": result.server_version,
        "source_revision": result.source_revision,
        "tool_count": result.tool_count,
        "tool_catalog_sha256": result.tool_catalog_sha256,
        "capability_catalog_sha256": result.capability_catalog_sha256,
        "contract_revision": result.contract_revision,
        "called_tools": list(result.called_tools),
        "diagnostics": list(result.diagnostics),
    }


async def accept(
    repository_root: Path, *, allow_dirty: bool = False
) -> FreshMcpClientResult:
    """Провести одну ограниченную fresh session без Codex task/session state."""

    source_revision, working_tree = git_source_snapshot(repository_root)
    if working_tree == "modified" and not allow_dirty:
        return FreshMcpClientResult(
            state=IntegrationState.INCOMPATIBLE,
            reason_code="MCP_FRESH_CLIENT_SOURCE_NOT_CLEAN",
            diagnostics=("working_tree_modified",),
        )
    if working_tree not in {"clean", "modified"}:
        return FreshMcpClientResult(
            state=IntegrationState.INCOMPATIBLE,
            reason_code="MCP_FRESH_CLIENT_SOURCE_UNKNOWN",
            diagnostics=("working_tree_unknown",),
        )
    results: dict[str, FreshMcpClientResult] = {}
    for server_name in ("azurpilot-dev", "azurpilot-game"):
        try:
            headers = local_http_headers(repository_root, server_name)
        except LocalHttpAuthError:
            results[server_name] = FreshMcpClientResult(
                state=IntegrationState.UNAVAILABLE,
                reason_code="MCP_PROJECT_LOCAL_CREDENTIAL_UNAVAILABLE",
                diagnostics=(f"{server_name}:credential_unavailable",),
            )
            continue
        results[server_name] = await accept_fresh_http(
            endpoint=LOCAL_HTTP_ENDPOINTS[server_name],
            headers=headers,
            plan=build_plan(source_revision, server_name),
            timeout_seconds=FRESH_ACCEPTANCE_TIMEOUT_SECONDS,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        )
    result = results["azurpilot-dev"]
    game_result = results["azurpilot-game"]
    called_tools = tuple(
        dict.fromkeys((*result.called_tools, *game_result.called_tools))
    )
    if result.state is IntegrationState.READY and game_result.state is not IntegrationState.READY:
        result = replace(
            game_result,
            called_tools=called_tools,
            diagnostics=(
                "azurpilot-dev:ready",
                *game_result.diagnostics,
            )[:16],
        )
    elif result.state is IntegrationState.READY:
        result = replace(
            result,
            called_tools=called_tools,
            diagnostics=(
                *result.diagnostics,
                "azurpilot-game:ready",
            )[:16],
        )
    else:
        result = replace(
            result,
            called_tools=called_tools,
            diagnostics=(
                *result.diagnostics,
                f"azurpilot-game:{game_result.state.value.lower()}",
            )[:16],
        )
    if working_tree == "modified" and "working_tree_modified" not in result.diagnostics:
        return replace(
            result,
            diagnostics=("working_tree_modified", *result.diagnostics)[:16],
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    result = asyncio.run(accept(args.repository_root.resolve()))
    payload = _result_payload(result)
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"Свежий MCP-клиент: {result.state.value} "
            f"({result.reason_code}); вызовы={','.join(result.called_tools) or 'нет'}"
        )
    return 0 if result.state.value == "READY" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "GAME_REQUIRED_READ_ONLY_CALLS",
    "REQUIRED_READ_ONLY_CALLS",
    "accept",
    "build_plan",
    "main",
]
