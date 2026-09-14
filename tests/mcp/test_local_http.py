from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx2 as httpx
import pytest

from module.dev_mcp.adapter import DevMcpAdapter
from module.dev_mcp.contract import DEV_MCP_REQUIRED_SCOPE
from module.dev_mcp.local_http import create_local_http_app as create_dev_app
from module.game_mcp.adapter import GameMcpAdapter
from module.game_mcp.local_http import create_local_http_app as create_game_app
from module.game_mcp.server import GAME_MCP_REQUIRED_SCOPE, GAME_MCP_SCOPES
from module.mcp_shared.auth import current_access_token, current_transport
from module.mcp_shared.local_http import LocalHttpConfig, LocalHttpConfigError


def _config(
    *,
    server_name: str,
    port: int,
    required_scope: str,
    accepted_scopes: tuple[str, ...] | None = None,
) -> LocalHttpConfig:
    return LocalHttpConfig(
        server_name=server_name,
        port=port,
        required_scope=required_scope,
        accepted_scopes=accepted_scopes,
        token_env_var="AZURPILOT_TEST_LOCAL_MCP_TOKEN",
        token="test-token",
    )


@asynccontextmanager
async def _client(app: Any, config: LocalHttpConfig):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=f"http://{config.public_host}",
        ) as client,
    ):
        yield client


def _headers(
    config: LocalHttpConfig,
    *,
    token: str | None = "test-token",
    origin: str | None = None,
    host: str | None = None,
) -> dict[str, str]:
    headers = {
        "Host": host or config.public_host,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2024-11-05",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return headers


def _initialize_payload(request_id: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "local-http-test", "version": "1"},
        },
    }


def _contract_call_payload(
    request_id: int = 2, tool_name: str = "game_get_contract"
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {}},
    }


def test_local_http_config_is_strict_loopback_and_token_bounded() -> None:
    with pytest.raises(LocalHttpConfigError, match="127.0.0.1"):
        _config(
            server_name="azurpilot-game",
            port=18776,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
        ).__class__(
            server_name="azurpilot-game",
            port=18776,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
            token_env_var="AZURPILOT_TEST_LOCAL_MCP_TOKEN",
            token="test-token",
            bind_host="0.0.0.0",
        )
    with pytest.raises(LocalHttpConfigError, match="token_env_var"):
        LocalHttpConfig(
            server_name="azurpilot-game",
            port=18776,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
            token_env_var="bad-name",
            token="test-token",
        )


def test_game_local_http_requires_auth_and_reports_local_authority() -> None:
    async def scenario() -> None:
        config = _config(
            server_name="azurpilot-game",
            port=18776,
            required_scope=GAME_MCP_REQUIRED_SCOPE,
            accepted_scopes=GAME_MCP_SCOPES,
        )
        app = create_game_app(GameMcpAdapter(lambda: object()), config=config)
        async with _client(app, config) as client:
            missing = await client.post(
                "/mcp",
                headers=_headers(config, token=None),
                json=_initialize_payload(),
            )
            assert missing.status_code == 401
            wrong_token = await client.post(
                "/mcp",
                headers=_headers(config, token="wrong-token"),
                json=_initialize_payload(),
            )
            assert wrong_token.status_code == 401

            wrong_host = await client.get(
                "/ready",
                headers=_headers(config, host="localhost:18776", token=None),
            )
            assert wrong_host.status_code == 421

            wrong_origin = await client.get(
                "/ready",
                headers=_headers(config, token=None, origin="http://localhost:18776"),
            )
            assert wrong_origin.status_code == 403

            ready = await client.get("/ready", headers=_headers(config, token=None))
            assert ready.status_code == 200
            assert ready.json()["code"] == "LOCAL_MCP_READY"

            initialized = await client.post(
                "/mcp",
                headers=_headers(config),
                json=_initialize_payload(),
            )
            assert initialized.status_code == 200
            tools = await client.post(
                "/mcp",
                headers=_headers(config),
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                },
            )
            assert tools.status_code == 200
            tool_names = {
                item["name"] for item in tools.json()["result"]["tools"]
            }
            assert {
                "game_get_contract",
                "game_list_profiles",
                "game_start_profile",
            } <= tool_names
            called = await client.post(
                "/mcp",
                headers=_headers(config),
                json=_contract_call_payload(request_id=3),
            )
            assert called.status_code == 200
            result = called.json()["result"]["structuredContent"]
            assert result["details"]["request_context"] == {
                "transport": "local_http",
                "authenticated": True,
                "local_authority": True,
                "granted_scopes": list(GAME_MCP_SCOPES),
                "read_allowed": True,
                "control_allowed": True,
            }
        assert current_access_token() is None
        assert current_transport() == "local_stdio"

    asyncio.run(scenario())


def test_dev_local_http_reports_local_authority() -> None:
    async def scenario() -> None:
        config = _config(
            server_name="azurpilot-dev",
            port=18775,
            required_scope=DEV_MCP_REQUIRED_SCOPE,
        )
        app = create_dev_app(DevMcpAdapter(), config=config)
        async with _client(app, config) as client:
            called = await client.post(
                "/mcp",
                headers=_headers(config),
                json=_contract_call_payload(tool_name="dev_get_contract"),
            )
            assert called.status_code == 200
            result = called.json()["result"]["structuredContent"]
            assert result["code"] == "DEV_MCP_CONTRACT_READY"
            assert result["details"]["request_context"] == {
                "transport": "local_http",
                "authenticated": True,
                "local_authority": True,
                "granted_scopes": [DEV_MCP_REQUIRED_SCOPE],
                "read_allowed": True,
                "control_allowed": True,
            }
        assert current_access_token() is None
        assert current_transport() == "local_stdio"

    asyncio.run(scenario())
