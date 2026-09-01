from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.client.client import Client
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
from module.dev_runtime import DevEnvironment, DevSessionManager
from module.dev_runtime.game_bridge import GameObservationCapability
from module.dev_runtime.target import DevTarget

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


def test_tool_definitions_are_strict_and_target_neutral() -> None:
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
        "dev_list_game_observation_capabilities",
        "dev_get_game_observation",
        "dev_capture_smoke_game_checkpoint",
        "dev_get_smoke_game_observations",
        "dev_get_database_status",
        "dev_list_database_checks",
        "dev_run_database_check",
        "dev_list_database_repairs",
        "dev_preview_database_repair",
        "dev_get_runtime_status",
        "dev_start_game",
        "dev_stop_game",
        "dev_restart_game",
        "dev_start_emulator",
        "dev_stop_emulator",
        "dev_restart_emulator",
        "dev_restart_adb",
        "dev_get_control_operation",
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
    additive = {"dev_get_evidence", "dev_get_logs", "dev_get_screenshot", "dev_submit_smoke_evaluation", "dev_capture_smoke_game_checkpoint"}
    control_start = {"dev_start_game", "dev_start_emulator"}
    control_stop = {"dev_stop_game", "dev_stop_emulator"}
    control_restart = {"dev_restart_game", "dev_restart_emulator", "dev_restart_adb"}
    control_mutating = control_start | control_stop | control_restart
    argument_tools = {
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
        "dev_get_game_observation",
        "dev_capture_smoke_game_checkpoint",
        "dev_get_smoke_game_observations",
        "dev_get_database_status",
        "dev_run_database_check",
        "dev_preview_database_repair",
        "dev_get_control_operation",
    }
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        assert not _FORBIDDEN_INPUT_FIELDS.intersection(tool.input_schema.get("properties", {}))
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None
        assert tool.meta == {"securitySchemes": [{"type": "oauth2", "scopes": [DEV_MCP_REQUIRED_SCOPE]}]}
        assert tool.output_schema["additionalProperties"] is False
        assert tool.annotations.read_only_hint is (tool.name not in (mutating | additive | control_mutating))
        assert tool.annotations.destructive_hint is (tool.name in (mutating | control_stop | control_restart))
        expected_idempotent = tool.name not in (mutating | additive | control_restart)
        assert tool.annotations.idempotent_hint is expected_idempotent
        if tool.name not in argument_tools:
            assert tool.input_schema["properties"] == {}
            assert "required" not in tool.input_schema

    for name in additive:
        tool = next(tool for tool in tools if tool.name == name)
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is False

    for name in control_start:
        tool = next(tool for tool in tools if tool.name == name)
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
    for name in control_stop:
        tool = next(tool for tool in tools if tool.name == name)
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.idempotent_hint is True
    for name in control_restart:
        tool = next(tool for tool in tools if tool.name == name)
        assert tool.annotations.read_only_hint is False
        assert tool.annotations.destructive_hint is True
        assert tool.annotations.idempotent_hint is False

    task_schema = next(tool for tool in tools if tool.name == "dev_plan_session").input_schema
    assert task_schema["required"] == ["root_tasks"]
    assert task_schema["properties"]["root_tasks"]["minItems"] == 1
    evidence_schema = next(tool for tool in tools if tool.name == "dev_get_evidence").input_schema
    assert set(evidence_schema["properties"]) == {"session_id"}
    timeline_schema = next(tool for tool in tools if tool.name == "dev_get_timeline").input_schema
    assert timeline_schema["properties"]["limit"]["maximum"] == 200
    logs_schema = next(tool for tool in tools if tool.name == "dev_get_logs").input_schema
    assert logs_schema["properties"]["cursor"]["maxLength"] == 2048
    evaluation_schema = next(tool for tool in tools if tool.name == "dev_submit_smoke_evaluation").input_schema
    assert set(evaluation_schema["properties"]) == {"smoke_id", "assertion_id", "verdict", "rationale"}
    assert evaluation_schema["required"] == ["smoke_id", "assertion_id", "verdict", "rationale"]
    assert "external_agent" not in evaluation_schema["properties"]
    game_schema = next(tool for tool in tools if tool.name == "dev_get_game_observation").input_schema
    assert game_schema["properties"]["parameters"]["maxProperties"] == 16
    assert game_schema["properties"]["parameters"]["additionalProperties"] is False
    assert game_schema["properties"]["parameters"]["patternProperties"]
    checkpoint_schema = next(tool for tool in tools if tool.name == "dev_capture_smoke_game_checkpoint").input_schema
    assert checkpoint_schema["properties"]["checkpoint_id"]["not"] == {"enum": ["before", "final"]}


def test_server_bootstrap_does_not_construct_runtime_manager() -> None:
    factory_calls: list[int] = []

    def factory() -> object:
        factory_calls.append(1)
        return object()

    create_server(DevMcpAdapter(factory))

    assert factory_calls == []


def test_game_capability_protocol_uses_injected_bridge_factory(tmp_path: Path) -> None:
    capability = GameObservationCapability(
        capability_id="synthetic",
        description="Синтетическая capability",
        source="tests.synthetic",
    )
    bridge = SimpleNamespace(descriptors=lambda: (capability,))
    manager = DevSessionManager(
        DevEnvironment(tmp_path, Path("python"), DevTarget("fixture-target")),
        target_locked=True,
        game_bridge_factory=lambda _environment: bridge,
    )

    result = DevMcpAdapter(lambda: manager).call(
        "dev_list_game_observation_capabilities",
        {},
    )

    assert result["ok"] is True
    assert result["code"] == "DEV_GAME_OBSERVATION_CAPABILITIES_READY"
    assert result["details"]["capabilities"] == [capability.as_dict()]


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

    assert response.is_error is False
    assert response.structured_content is not None
    assert response.structured_content["details"]["screenshot"]["mime"] == "image/png"
    assert response.content[0].type == "image"
    assert response.content[0].mime_type == "image/png"
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
            assert initialized.server_info.name == SERVER_NAME
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == set(DEV_MCP_TOOL_NAMES)
            result = await session.call_tool("dev_list_tasks", {})
            assert result.structured_content is not None
            assert isinstance(result.structured_content["ok"], bool)
            assert result.structured_content["code"] in {
                "DEV_TASK_STATE_MISSING",
                "DEV_TASK_CATALOG_READY",
                "DEV_TARGET_NOT_CONFIGURED",
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
            }
            contract = await session.call_tool("dev_get_contract", {})
            assert contract.structured_content is not None
            assert contract.structured_content["ok"] is True
            assert contract.structured_content["details"]["contract"] == EXPECTED_CONTRACT

            game_capabilities = await session.call_tool(
                "dev_list_game_observation_capabilities",
                {},
            )
            assert game_capabilities.structured_content is not None
            assert game_capabilities.structured_content["code"] in {
                "DEV_GAME_OBSERVATION_CAPABILITIES_READY",
                "DEV_GAME_OBSERVATION_UNAVAILABLE",
                "DEV_TARGET_NOT_CONFIGURED",
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
            }
            if game_capabilities.structured_content["code"] == "DEV_GAME_OBSERVATION_CAPABILITIES_READY":
                assert isinstance(
                    game_capabilities.structured_content["details"]["capabilities"],
                    list,
                )
            game_observation = await session.call_tool(
                "dev_get_game_observation",
                {"capability_id": "resources", "parameters": {}},
            )
            assert game_observation.structured_content is not None
            assert game_observation.structured_content["code"] in {
                "DEV_GAME_OBSERVATION_READY",
                "DEV_GAME_OBSERVATION_UNKNOWN",
                "DEV_GAME_OBSERVATION_UNAVAILABLE",
                "DEV_TARGET_NOT_CONFIGURED",
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
            }
            if game_observation.structured_content["code"] == "DEV_GAME_OBSERVATION_READY":
                assert isinstance(
                    game_observation.structured_content["details"]["observation"],
                    dict,
                )
            database_checks = await session.call_tool("dev_list_database_checks", {})
            assert database_checks.structured_content is not None
            assert database_checks.structured_content["code"] in {
                "DEV_DATABASE_CHECKS_READY",
                "DEV_DATABASE_DIAGNOSTICS_UNAVAILABLE",
                "DEV_TARGET_NOT_CONFIGURED",
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
            }

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


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
                "DEV_TARGET_NOT_CONFIGURED",
                "DEV_TARGET_DEFAULT_PROFILE_MISSING",
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


def test_modern_client_discovers_2026_server_and_calls_tool() -> None:
    async def scenario() -> None:
        async with Client(_server_parameters(), mode="auto") as client:
            assert client.protocol_version == "2026-07-28"
            assert client.server_info is not None
            assert client.server_info.name == SERVER_NAME
            assert client.session.discover_result is not None
            assert "2026-07-28" in client.session.discover_result.supported_versions
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == set(DEV_MCP_TOOL_NAMES)
            result = await client.call_tool("dev_get_contract", {})
            assert result.is_error is False
            assert result.structured_content is not None
            assert result.structured_content["code"] == "DEV_MCP_CONTRACT_READY"

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
