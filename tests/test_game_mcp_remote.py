from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.server.auth.provider import AccessToken
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

from module.game_mcp.adapter import (
    GAME_MCP_CONTROL_TOOL_NAMES,
    GameMcpAdapter,
    GameMcpResponse,
)
from module.game_mcp.remote import (
    DEFAULT_ALLOWED_ORIGINS,
    GAME_MCP_CONTROL_SCOPE,
    GAME_MCP_PORT,
    GAME_MCP_REQUIRED_SCOPE,
    GAME_MCP_SCOPES,
    RemoteConfig,
    RemoteConfigError,
    create_remote_app,
    run_remote_server,
)
from module.game_mcp.server import tool_definitions
from module.mcp_shared.auth import current_access_token

_BASE_URL = "https://game-mcp.example.test"
_HOST = "game-mcp.example.test"
_ISSUER = "https://login.example.test"
_AUDIENCE = "https://game-mcp.example.test/mcp"
_JWKS_URL = "https://login.example.test/.well-known/jwks.json"


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, object]:
        self.calls.append((name, arguments))
        return {
            "ok": True,
            "code": "GAME_TEST_OK",
            "message": "ok",
            "state": "ready",
            "details": {},
        }

    def close(self) -> None:
        self.closed = True


class _StaticVerifier:
    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "valid-token":
            return None
        return AccessToken(
            token="",
            client_id="user-1",
            scopes=self.scopes,
            expires_at=int(time.time()) + 300,
            resource=_AUDIENCE,
        )


def _config(**overrides: Any) -> RemoteConfig:
    values: dict[str, Any] = {
        "public_url": _AUDIENCE,
        "oauth_issuer": _ISSUER,
        "oauth_audience": _AUDIENCE,
        "oauth_jwks_url": _JWKS_URL,
        "oauth_subject": "user-1",
        "allowed_origins": DEFAULT_ALLOWED_ORIGINS,
    }
    values.update(overrides)
    return RemoteConfig(**values)


@asynccontextmanager
async def _client(app: Any):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=_BASE_URL,
        ) as client,
    ):
        yield client


def _headers(
    *,
    auth: bool = True,
    origin: str | None = None,
    method: str | None = None,
    name: str | None = None,
) -> dict[str, str]:
    headers = {
        "Host": _HOST,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2026-07-28",
    }
    if auth:
        headers["Authorization"] = "Bearer valid-token"
    if origin is not None:
        headers["Origin"] = origin
    if method is not None:
        headers["MCP-Method"] = method
    if name is not None:
        headers["MCP-Name"] = name
    return headers


def _modern_params(name: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "_meta": {
            PROTOCOL_VERSION_META_KEY: "2026-07-28",
            CLIENT_INFO_META_KEY: {"name": "game-test", "version": "1"},
            CLIENT_CAPABILITIES_META_KEY: {},
        }
    }
    if name is not None:
        params["name"] = name
    return params


def test_game_remote_config_is_independent_and_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AZURPILOT_GAME_MCP_PUBLIC_URL",
        "AZURPILOT_GAME_MCP_OAUTH_ISSUER",
        "AZURPILOT_GAME_MCP_OAUTH_AUDIENCE",
        "AZURPILOT_GAME_MCP_OAUTH_JWKS_URL",
        "AZURPILOT_GAME_MCP_OAUTH_SUBJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RemoteConfigError, match="AZURPILOT_GAME_MCP_PUBLIC_URL"):
        RemoteConfig.from_env()
    with pytest.raises(RemoteConfigError, match="127.0.0.1"):
        _config(bind_host="0.0.0.0")
    assert GAME_MCP_REQUIRED_SCOPE != "azurpilot:dev"


def test_game_remote_is_stateless_modern_and_scope_separated() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        config = _config()
        app = create_remote_app(
            adapter,
            config=config,
            token_verifier=_StaticVerifier([GAME_MCP_REQUIRED_SCOPE]),
        )
        assert app.state.session_manager.stateless is True
        async with _client(app) as client:
            bad_host_headers = _headers()
            bad_host_headers["Host"] = "attacker.example.test"
            bad_host = await client.post(
                "/mcp",
                headers=bad_host_headers,
                json={"jsonrpc": "2.0"},
            )
            assert bad_host.status_code == 421

            bad_origin = await client.post(
                "/mcp",
                headers=_headers(origin="https://attacker.example.test"),
                json={"jsonrpc": "2.0"},
            )
            assert bad_origin.status_code == 403

            discovered = await client.post(
                "/mcp",
                headers=_headers(method="server/discover"),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            PROTOCOL_VERSION_META_KEY: "2026-07-28",
                            CLIENT_INFO_META_KEY: {"name": "game-test", "version": "1"},
                            CLIENT_CAPABILITIES_META_KEY: {},
                        }
                    },
                },
            )
            assert discovered.status_code == 200
            assert "2026-07-28" in discovered.json()["result"]["supportedVersions"]

            listed = await client.post(
                "/mcp",
                headers=_headers(method="tools/list"),
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": _modern_params(),
                },
            )
            assert listed.status_code == 200
            assert listed.json()["result"]["tools"] == [
                tool.model_dump(by_alias=True, exclude_none=True)
                for tool in tool_definitions()
            ]
            assert all(
                tool["_meta"]["securitySchemes"]
                == [
                    {
                        "type": "oauth2",
                        "scopes": [
                            GAME_MCP_CONTROL_SCOPE
                            if tool["name"] in GAME_MCP_CONTROL_TOOL_NAMES
                            else GAME_MCP_REQUIRED_SCOPE
                        ],
                    }
                ]
                for tool in listed.json()["result"]["tools"]
            )

            called = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_get_contract"),
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_contract"),
                        "arguments": {},
                    },
                },
            )
            assert called.status_code == 200
            assert (
                called.json()["result"]["structuredContent"]["code"] == "GAME_TEST_OK"
            )
            called_again = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_get_contract"),
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_contract"),
                        "arguments": {},
                    },
                },
            )
            assert called_again.status_code == 200
            assert (
                called_again.json()["result"]["structuredContent"]["code"]
                == "GAME_TEST_OK"
            )
        assert adapter.calls == [
            ("game_get_contract", {}),
            ("game_get_contract", {}),
        ]
        assert adapter.closed is True

    asyncio.run(scenario())


def test_game_remote_rejects_dev_scope_and_missing_auth() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        app = create_remote_app(
            adapter,
            config=_config(),
            token_verifier=_StaticVerifier(["azurpilot:dev"]),
        )
        async with _client(app) as client:
            missing = await client.post(
                "/mcp", headers=_headers(auth=False), json={"jsonrpc": "2.0"}
            )
            assert missing.status_code == 401
            assert (
                f'scope="{GAME_MCP_REQUIRED_SCOPE}"'
                in missing.headers["www-authenticate"]
            )

            wrong_token = await client.post(
                "/mcp",
                headers={**_headers(), "Authorization": "Bearer wrong-token"},
                json={"jsonrpc": "2.0"},
            )
            assert wrong_token.status_code == 401

            query_token = await client.post(
                "/mcp?access_token=valid-token",
                headers=_headers(auth=False),
                json={"jsonrpc": "2.0"},
            )
            assert query_token.status_code == 401

            dev_token = await client.post(
                "/mcp", headers=_headers(), json={"jsonrpc": "2.0"}
            )
            assert dev_token.status_code == 403
            assert dev_token.json() == {"error": "forbidden"}
        assert adapter.calls == []

    asyncio.run(scenario())


def test_game_remote_contract_reports_effective_context_and_cleans_principal() -> None:
    async def scenario() -> None:
        app = create_remote_app(
            GameMcpAdapter(lambda: object()),
            config=_config(),
            token_verifier=_StaticVerifier(
                [GAME_MCP_REQUIRED_SCOPE, GAME_MCP_CONTROL_SCOPE]
            ),
        )
        async with _client(app) as client:
            response = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_get_contract"),
                json={
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_contract"),
                        "arguments": {},
                    },
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]["structuredContent"]
            assert result["details"]["request_context"] == {
                "transport": "remote_http",
                "authenticated": True,
                "local_authority": False,
                "granted_scopes": [
                    GAME_MCP_REQUIRED_SCOPE,
                    GAME_MCP_CONTROL_SCOPE,
                ],
                "read_allowed": True,
                "control_allowed": True,
            }
            assert "valid-token" not in response.text
        assert current_access_token() is None

    asyncio.run(scenario())


def test_game_remote_request_context_does_not_leak_between_concurrent_scopes() -> None:
    class _ScopeVerifier:
        async def verify_token(self, token: str) -> AccessToken | None:
            scopes = {
                "read-token": [GAME_MCP_REQUIRED_SCOPE],
                "control-token": [GAME_MCP_REQUIRED_SCOPE, GAME_MCP_CONTROL_SCOPE],
            }.get(token)
            if scopes is None:
                return None
            return AccessToken(
                token="",
                client_id=token,
                scopes=scopes,
                expires_at=int(time.time()) + 300,
                resource=_AUDIENCE,
            )

    async def scenario() -> None:
        app = create_remote_app(
            GameMcpAdapter(lambda: object()),
            config=_config(),
            token_verifier=_ScopeVerifier(),
        )

        async def request(client: httpx.AsyncClient, token: str, request_id: int):
            headers = _headers(
                auth=False, method="tools/call", name="game_get_contract"
            )
            headers["Authorization"] = f"Bearer {token}"
            return await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_contract"),
                        "arguments": {},
                    },
                },
            )

        async with _client(app) as client:
            tokens = ["read-token", "control-token"] * 4
            responses = await asyncio.gather(
                *(request(client, token, index) for index, token in enumerate(tokens))
            )
            for token, response in zip(tokens, responses, strict=True):
                assert response.status_code == 200
                result = response.json()["result"]["structuredContent"]
                expected_scopes = [GAME_MCP_REQUIRED_SCOPE]
                if token == "control-token":
                    expected_scopes.append(GAME_MCP_CONTROL_SCOPE)
                assert (
                    result["details"]["request_context"]["granted_scopes"]
                    == expected_scopes
                )
        assert current_access_token() is None

    asyncio.run(scenario())


def test_game_remote_enforces_per_tool_scope_before_adapter() -> None:
    async def scenario() -> None:
        read_adapter = _RecordingAdapter()
        read_app = create_remote_app(
            read_adapter,
            config=_config(),
            token_verifier=_StaticVerifier([GAME_MCP_REQUIRED_SCOPE]),
        )
        async with _client(read_app) as client:
            denied_control = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_start_profile"),
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_start_profile"),
                        "arguments": {"profile": "alpha"},
                    },
                },
            )
            assert denied_control.status_code == 200
            assert (
                denied_control.json()["result"]["structuredContent"]["code"]
                == "GAME_MCP_UNAUTHORIZED"
            )
        assert read_adapter.calls == []

        control_adapter = _RecordingAdapter()
        control_app = create_remote_app(
            control_adapter,
            config=_config(),
            token_verifier=_StaticVerifier([GAME_MCP_CONTROL_SCOPE]),
        )
        async with _client(control_app) as client:
            denied_read = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_get_contract"),
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_contract"),
                        "arguments": {},
                    },
                },
            )
            assert denied_read.status_code == 200
            assert (
                denied_read.json()["result"]["structuredContent"]["code"]
                == "GAME_MCP_UNAUTHORIZED"
            )
            allowed_control = await client.post(
                "/mcp",
                headers=_headers(method="tools/call", name="game_start_profile"),
                json={
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_start_profile"),
                        "arguments": {"profile": "alpha"},
                    },
                },
            )
            assert allowed_control.status_code == 200
            assert (
                allowed_control.json()["result"]["structuredContent"]["code"]
                == "GAME_TEST_OK"
            )
        assert control_adapter.calls == [("game_start_profile", {"profile": "alpha"})]

    asyncio.run(scenario())


def test_game_remote_metadata_contains_game_resource_and_scope() -> None:
    async def scenario() -> None:
        app = create_remote_app(
            _RecordingAdapter(),
            config=_config(),
            token_verifier=_StaticVerifier([GAME_MCP_REQUIRED_SCOPE]),
        )
        async with _client(app) as client:
            response = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": _HOST, "Origin": "https://chatgpt.com"},
            )
            assert response.status_code == 200
            assert response.json() == {
                "resource": _AUDIENCE,
                "authorization_servers": [_ISSUER],
                "scopes_supported": list(GAME_MCP_SCOPES),
                "bearer_methods_supported": ["header"],
            }
            assert (
                response.headers["access-control-allow-origin"] == "https://chatgpt.com"
            )

    asyncio.run(scenario())


def test_game_remote_server_uses_dedicated_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(_app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("module.game_mcp.remote.uvicorn.run", fake_run)
    run_remote_server(_RecordingAdapter(), config=_config())
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == GAME_MCP_PORT == 8766


def test_game_remote_screenshot_result_uses_native_image_content() -> None:
    image = b"not-used-by-transport-test"

    class _ScreenshotAdapter(_RecordingAdapter):
        def call(
            self, name: str, arguments: dict[str, Any]
        ) -> dict[str, object] | GameMcpResponse:
            self.calls.append((name, arguments))
            if name == "game_get_screenshot":
                return GameMcpResponse(
                    {
                        "ok": True,
                        "code": "GAME_SCREENSHOT_READY",
                        "message": "ok",
                        "state": "ready",
                        "details": {"screenshot": {"mime": "image/png"}},
                    },
                    image,
                    "image/png",
                )
            return super().call(name, arguments)

    async def scenario() -> None:
        adapter = _ScreenshotAdapter()
        app = create_remote_app(
            adapter,
            config=_config(),
            token_verifier=_StaticVerifier([GAME_MCP_REQUIRED_SCOPE]),
        )
        async with _client(app) as client:
            response = await client.post(
                "/mcp",
                headers=_headers(
                    method="tools/call", name="game_get_screenshot"
                ),
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        **_modern_params("game_get_screenshot"),
                        "arguments": {"profile": "ap"},
                    },
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["content"][0]["type"] == "image"
            assert result["content"][0]["mimeType"] == "image/png"
            assert (
                base64.b64decode(result["content"][0]["data"]) == image
            )
            structured = json.dumps(
                result["structuredContent"], ensure_ascii=False
            )
            assert base64.b64encode(image).decode("ascii") not in structured
        assert adapter.closed is True

    asyncio.run(scenario())
