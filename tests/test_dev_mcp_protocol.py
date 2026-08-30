from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from module.dev_mcp.adapter import DEV_MCP_TOOL_NAMES, DevMcpAdapter, DevMcpResponse
from module.dev_mcp.server import (
    DEV_MCP_ARGS,
    DEV_MCP_COMMAND,
    DEV_MCP_REQUIRED_SCOPE,
    SERVER_NAME,
    _screenshot_call_result,
    create_server,
    tool_definitions,
)
from tests.dev_mcp_contract_helpers import EXPECTED_CONTRACT

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
        "dev_get_contract",
        "dev_list_tasks",
        "dev_plan_session",
        "dev_start_session",
        "dev_status",
        "dev_stop_session",
        "dev_cleanup",
        "dev_recover",
        "dev_get_evidence",
        "dev_get_timeline",
        "dev_get_logs",
        "dev_get_screenshot",
        "dev_list_smoke_capabilities",
        "dev_validate_smoke",
        "dev_start_smoke",
        "dev_get_smoke",
        "dev_cancel_smoke",
        "dev_get_smoke_evaluation",
        "dev_submit_smoke_evaluation",
    ]
    assert names == expected_names
    assert tuple(names) == DEV_MCP_TOOL_NAMES
    assert len(names) == len(set(names))
    assert set(names) == set(expected_names)
    mutating = {
        "dev_start_session",
        "dev_stop_session",
        "dev_cleanup",
        "dev_recover",
        "dev_cancel_smoke",
        "dev_start_smoke",
    }
    additive = {"dev_get_evidence", "dev_get_logs", "dev_get_screenshot", "dev_submit_smoke_evaluation"}
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        assert not _FORBIDDEN_INPUT_FIELDS.intersection(tool.inputSchema.get("properties", {}))
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.outputSchema is not None
        assert tool.securitySchemes == [{"type": "oauth2", "scopes": [DEV_MCP_REQUIRED_SCOPE]}]
        assert tool.outputSchema["additionalProperties"] is False
        assert tool.annotations.readOnlyHint is (tool.name not in (mutating | additive))
        assert tool.annotations.destructiveHint is (tool.name in mutating)
        assert tool.annotations.idempotentHint is (tool.name not in (mutating | additive))
        if tool.name not in {
            "dev_plan_session",
            "dev_start_session",
            "dev_stop_session",
            "dev_get_evidence",
            "dev_get_timeline",
            "dev_get_logs",
            "dev_validate_smoke",
            "dev_start_smoke",
            "dev_get_smoke",
            "dev_cancel_smoke",
            "dev_get_smoke_evaluation",
            "dev_submit_smoke_evaluation",
        }:
            assert tool.inputSchema["properties"] == {}
            assert "required" not in tool.inputSchema

    for name in additive:
        tool = next(tool for tool in tools if tool.name == name)
        assert tool.annotations.readOnlyHint is False
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is False

    task_schema = next(tool for tool in tools if tool.name == "dev_plan_session").inputSchema
    assert task_schema["required"] == ["root_tasks"]
    assert task_schema["properties"]["root_tasks"]["minItems"] == 1
    evidence_schema = next(tool for tool in tools if tool.name == "dev_get_evidence").inputSchema
    assert set(evidence_schema["properties"]) == {"session_id"}
    timeline_schema = next(tool for tool in tools if tool.name == "dev_get_timeline").inputSchema
    assert timeline_schema["properties"]["limit"]["maximum"] == 200
    logs_schema = next(tool for tool in tools if tool.name == "dev_get_logs").inputSchema
    assert logs_schema["properties"]["cursor"]["maxLength"] == 2048
    evaluation_schema = next(tool for tool in tools if tool.name == "dev_submit_smoke_evaluation").inputSchema
    assert set(evaluation_schema["properties"]) == {"smoke_id", "assertion_id", "verdict", "rationale"}
    assert evaluation_schema["required"] == ["smoke_id", "assertion_id", "verdict", "rationale"]
    assert "external_agent" not in evaluation_schema["properties"]


def test_server_bootstrap_does_not_construct_runtime_manager() -> None:
    factory_calls: list[int] = []

    def factory() -> object:
        factory_calls.append(1)
        return object()

    create_server(DevMcpAdapter(factory))

    assert factory_calls == []


def test_screenshot_response_uses_mcp_image_content_without_json_base64() -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    image_data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x7f\xff"))
        + chunk(b"IEND", b"")
    )
    response = _screenshot_call_result(
        DevMcpResponse(
            {
                "ok": True,
                "code": "DEV_SCREENSHOT_READY",
                "message": "Снимок экрана готов",
                "state": "running_owned",
                "session_id": "session-1",
                "details": {
                    "screenshot": {
                        "screenshot_id": "shot-1",
                        "timestamp": "2026-08-30T00:00:00+00:00",
                        "mime": "image/png",
                        "width": 1,
                        "height": 1,
                        "byte_size": len(image_data),
                        "sha256": hashlib.sha256(image_data).hexdigest(),
                    }
                },
            },
            image_data,
            "image/png",
        )
    )

    assert response.isError is False
    assert response.structuredContent is not None
    assert response.structuredContent["details"]["screenshot"]["mime"] == "image/png"
    assert response.content[0].type == "image"
    assert response.content[0].mimeType == "image/png"
    assert base64.b64decode(response.content[0].data) == image_data
    assert response.content[1].type == "text"
    assert "base64" not in response.content[1].text
    assert response.content[0].data not in response.content[1].text


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
                "dev_get_contract",
                "dev_list_tasks",
                "dev_plan_session",
                "dev_start_session",
                "dev_status",
                "dev_stop_session",
                "dev_cleanup",
                "dev_recover",
                "dev_get_evidence",
                "dev_get_timeline",
            "dev_get_logs",
            "dev_get_screenshot",
            "dev_list_smoke_capabilities",
            "dev_validate_smoke",
            "dev_start_smoke",
            "dev_get_smoke",
            "dev_cancel_smoke",
            "dev_get_smoke_evaluation",
            "dev_submit_smoke_evaluation",
        }
            result = await session.call_tool("dev_list_tasks", {})
            assert result.structuredContent is not None
            assert isinstance(result.structuredContent["ok"], bool)
            assert result.structuredContent["code"] in {
                "DEV_TASK_STATE_MISSING",
                "DEV_TASK_CATALOG_READY",
            }
            contract = await session.call_tool("dev_get_contract", {})
            assert contract.structuredContent is not None
            assert contract.structuredContent["ok"] is True
            assert contract.structuredContent["details"]["contract"] == EXPECTED_CONTRACT

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
            assert len(listed["result"]["tools"]) == len(DEV_MCP_TOOL_NAMES)

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
            assert isinstance(preflight["result"]["structuredContent"]["ok"], bool)
            assert isinstance(preflight["result"]["structuredContent"]["code"], str)

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
            assert after_error["result"]["structuredContent"]["code"] in {
                "DEV_NO_SESSION",
                "DEV_SESSION_STOPPED",
            }
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
