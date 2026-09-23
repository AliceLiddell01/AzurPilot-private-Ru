"""Независимая read-only acceptance-сессия first-party Dev MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path

from azurpilot.integrations.contracts import IntegrationState
from azurpilot.integrations.mcp_client import (
    FreshMcpClientPlan,
    FreshMcpClientResult,
    McpCallPlan,
    accept_fresh_stdio,
)
from dev_tools.mcp_status import child_environment, git_source_snapshot
from module.dev_mcp.contract import contract_payload
from module.dev_mcp.server import DEV_MCP_ARGS, DEV_MCP_COMMAND, tool_definitions
from tools.paths import REPOSITORY_ROOT

FRESH_ACCEPTANCE_TIMEOUT_SECONDS = 20.0
# Статус runtime требует пользовательский target profile, который не входит в
# tracked checkout. Поэтому fresh client protocol gate проверяет target-neutral
# каталог Smoke, а готовность runtime подтверждается отдельным live workflow.
REQUIRED_READ_ONLY_CALLS = (
    ("dev_list_smoke_capabilities", {}),
)


def build_plan(source_revision: str) -> FreshMcpClientPlan:
    """Собрать только canonical contract/catalog и bounded read-only calls."""

    expected_contract = contract_payload()
    expected_contract["source_revision"] = source_revision
    tool_descriptors = tuple(tool_definitions())
    expected_tools = frozenset(tool.name for tool in tool_descriptors)
    blocked_tools = frozenset(
        tool.name
        for tool in tool_descriptors
        if not getattr(tool.annotations, "read_only_hint", False)
    )
    required_tools = frozenset(
        {"dev_get_contract", *(name for name, _ in REQUIRED_READ_ONLY_CALLS)}
    )
    return FreshMcpClientPlan(
        call_plan=McpCallPlan(
            required_tools=required_tools,
            probe_tool="dev_get_contract",
            arguments={},
            blocked_tools=blocked_tools,
            expected_tools=expected_tools,
        ),
        contract_tool="dev_get_contract",
        expected_contract=expected_contract,
        required_read_only_calls=REQUIRED_READ_ONLY_CALLS,
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
    """Провести одну bounded fresh session без Codex task/session state."""

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
    executable = shutil.which(DEV_MCP_COMMAND)
    if executable is None:
        return FreshMcpClientResult(
            state=IntegrationState.UNAVAILABLE,
            reason_code="MCP_FRESH_CLIENT_COMMAND_UNAVAILABLE",
        )
    result = await accept_fresh_stdio(
        command=executable,
        args=tuple(DEV_MCP_ARGS),
        cwd=str(repository_root),
        environment=child_environment(source_revision),
        plan=build_plan(source_revision),
        timeout_seconds=FRESH_ACCEPTANCE_TIMEOUT_SECONDS,
    )
    if working_tree == "modified" and "working_tree_modified" not in result.diagnostics:
        return replace(
            result,
            diagnostics=(*result.diagnostics, "working_tree_modified")[:16],
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
            f"Fresh MCP client: {result.state.value} "
            f"({result.reason_code}); calls={','.join(result.called_tools) or 'none'}"
        )
    return 0 if result.state.value == "READY" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["REQUIRED_READ_ONLY_CALLS", "accept", "build_plan", "main"]
