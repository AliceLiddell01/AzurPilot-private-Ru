from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES, DevMcpAdapter
from module.dev_mcp.server import (
    DEV_MCP_ARGS,
    DEV_MCP_COMMAND,
    SERVER_NAME,
    create_server,
    tool_definitions,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_INPUT_FIELDS = {
    "profile",
    "instance",
    "config_name",
    "repository_path",
    "policy_file",
    "state_file",
    "python_path",
    "path",
}


def _server_parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=DEV_MCP_COMMAND,
        args=list(DEV_MCP_ARGS),
        cwd=str(_REPOSITORY_ROOT),
    )


def test_tool_definitions_are_strict_and_ap_only() -> None:
    tools = tool_definitions()
    names = [tool.name for tool in tools]

    expected_names = [
        "dev_preflight",
        "dev_doctor",
        "dev_list_tasks",
        "dev_plan_session",
        "dev_start_session",
        "dev_status",
        "dev_stop_session",
        "dev_cleanup",
        "dev_recover",
    ]
    assert names == expected_names
    assert tuple(names) == DEV_MCP_TOOL_NAMES
    assert len(names) == len(set(names))
    assert set(names) == set(expected_names)
    mutating = {"dev_start_session", "dev_stop_session", "dev_cleanup", "dev_recover"}
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        assert not _FORBIDDEN_INPUT_FIELDS.intersection(tool.inputSchema.get("properties", {}))
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.outputSchema is not None
        assert tool.outputSchema["additionalProperties"] is False
        assert tool.annotations.readOnlyHint is (tool.name not in mutating)
        assert tool.annotations.destructiveHint is (tool.name in mutating)
        if tool.name not in {
            "dev_plan_session",
            "dev_start_session",
            "dev_stop_session",
        }:
            assert tool.inputSchema["properties"] == {}
            assert "required" not in tool.inputSchema

    task_schema = next(tool for tool in tools if tool.name == "dev_plan_session").inputSchema
    assert task_schema["required"] == ["root_tasks"]
    assert task_schema["properties"]["root_tasks"]["minItems"] == 1


def test_server_bootstrap_does_not_construct_runtime_manager() -> None:
    factory_calls: list[int] = []

    def factory() -> object:
        factory_calls.append(1)
        return object()

    create_server(DevMcpAdapter(factory))

    assert factory_calls == []


def test_pinned_mcp_client_initializes_and_calls_server() -> None:
    async def scenario() -> None:
        async with (
            stdio_client(_server_parameters()) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == SERVER_NAME
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "dev_preflight",
                "dev_doctor",
                "dev_list_tasks",
                "dev_plan_session",
                "dev_start_session",
                "dev_status",
                "dev_stop_session",
                "dev_cleanup",
                "dev_recover",
            }
            result = await session.call_tool("dev_list_tasks", {})
            assert result.structuredContent is not None
            assert result.structuredContent["ok"] is False
            assert result.structuredContent["code"] == "DEV_TASK_STATE_MISSING"

    asyncio.run(scenario())


async def _raw_request(
    process: asyncio.subprocess.Process,
    payload: dict[str, object],
) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    await process.stdin.drain()
    line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    assert line, "Dev MCP process closed stdout before responding"
    assert line.endswith(b"\n")
    decoded = json.loads(line.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def test_real_subprocess_protocol_has_clean_stdout_and_recovers_after_invalid_call() -> None:
    async def scenario() -> None:
        process = await asyncio.create_subprocess_exec(
            DEV_MCP_COMMAND,
            *DEV_MCP_ARGS,
            cwd=str(_REPOSITORY_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            initialize = await _raw_request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "azurpilot-test", "version": "1"},
                    },
                },
            )
            assert initialize["id"] == 1
            assert initialize["result"]["serverInfo"]["name"] == SERVER_NAME

            assert process.stdin is not None
            process.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
            )
            await process.stdin.drain()

            listed = await _raw_request(
                process,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert listed["id"] == 2
            assert len(listed["result"]["tools"]) == 9

            preflight = await _raw_request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "dev_preflight", "arguments": {}},
                },
            )
            assert preflight["id"] == 3
            assert preflight["result"]["structuredContent"]["ok"] is False

            invalid = await _raw_request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "dev_plan_session",
                        "arguments": {"root_tasks": ["RootTask"], "profile": "alas"},
                    },
                },
            )
            assert invalid["id"] == 4
            assert invalid["result"]["isError"] is True

            after_error = await _raw_request(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "dev_status", "arguments": {}},
                },
            )
            assert after_error["id"] == 5
            assert after_error["result"]["structuredContent"]["code"] == "DEV_NO_SESSION"
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
                pytest.fail("Dev MCP process did not exit after client close")
            assert process.stdout is not None
            assert await process.stdout.read() == b""
            assert process.stderr is not None
            await process.stderr.read()
            assert process.returncode == 0

    asyncio.run(scenario())
