from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.auth.provider import AccessToken
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

from module.dev_mcp.adapter import DevMcpResponse
from module.dev_mcp.remote import (
    DEFAULT_ALLOWED_ORIGINS,
    DEV_MCP_PORT,
    ConcurrencyLimitMiddleware,
    OIDCTokenVerifier,
    RemoteConfig,
    RemoteConfigError,
    RequestTimeoutMiddleware,
    create_remote_app,
    doctor,
    run_remote_server,
)
from module.dev_mcp.server import (
    DEV_MCP_ARGS,
    DEV_MCP_COMMAND,
    DEV_MCP_REQUIRED_SCOPE,
    DEV_MCP_TOOL_NAMES,
    tool_definitions,
)

_BASE_URL = "https://mcp.example.test"
_HOST = "mcp.example.test"
_ISSUER = "https://login.example.test"
_AUDIENCE = "https://mcp.example.test/mcp"
_JWKS_URL = "https://login.example.test/.well-known/jwks.json"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {
            "ok": True,
            "code": "TEST_OK",
            "message": "ok",
            "state": "ready",
            "session_id": None,
            "details": {},
        }


class _StaticVerifier:
    def __init__(self, resource: str) -> None:
        self.resource = resource
        self.tokens: list[str] = []

    async def verify_token(self, token: str) -> AccessToken | None:
        self.tokens.append(token)
        if token != "valid-token":
            return None
        return AccessToken(
            token="",
            client_id="user-1",
            scopes=[DEV_MCP_REQUIRED_SCOPE],
            expires_at=int(time.time()) + 300,
            resource=self.resource,
        )


class _ScreenshotAdapter:
    def __init__(self, image: bytes) -> None:
        self.image = image

    def call(self, name: str, arguments: dict[str, Any]) -> DevMcpResponse:
        assert name == "dev_get_screenshot"
        assert arguments == {}
        return DevMcpResponse(
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
                        "byte_size": len(self.image),
                        "sha256": hashlib.sha256(self.image).hexdigest(),
                    }
                },
            },
            self.image,
            "image/png",
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
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=_BASE_URL,
    ) as client:
        yield client


def _headers(
    *,
    auth: bool = True,
    origin: str | None = None,
    protocol_version: str = "2024-11-05",
    method: str | None = None,
    name: str | None = None,
) -> dict[str, str]:
    headers = {
        "Host": _HOST,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
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


def _modern_params(*, protocol_version: str = "2026-07-28", name: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        PROTOCOL_VERSION_META_KEY: protocol_version,
        CLIENT_INFO_META_KEY: {"name": "azurpilot-test", "version": "1"},
        CLIENT_CAPABILITIES_META_KEY: {},
    }
    params: dict[str, Any] = {"_meta": meta}
    if name is not None:
        params["name"] = name
    return params


def _initialize_payload(request_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "remote-test", "version": "1"},
        },
    }


def _png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x7f\xff"))
        + chunk(b"IEND", b"")
    )


def test_remote_config_is_https_loopback_and_oauth_fail_closed() -> None:
    with pytest.raises(RemoteConfigError, match="127.0.0.1"):
        _config(bind_host="0.0.0.0")
    with pytest.raises(RemoteConfigError, match="публичный HTTPS-порт"):
        _config(public_url="https://mcp.example.test:8443/mcp")
    with pytest.raises(RemoteConfigError, match="подстановочных символов"):
        _config(allowed_origins=("*",))
    with pytest.raises(RemoteConfigError, match="ASCII"):
        _config(public_url="https://пример.example/mcp")


def test_remote_config_requires_all_oauth_environment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AZURPILOT_DEV_MCP_PUBLIC_URL",
        "AZURPILOT_DEV_MCP_OAUTH_ISSUER",
        "AZURPILOT_DEV_MCP_OAUTH_AUDIENCE",
        "AZURPILOT_DEV_MCP_OAUTH_JWKS_URL",
        "AZURPILOT_DEV_MCP_OAUTH_SUBJECT",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RemoteConfigError, match="AZURPILOT_DEV_MCP_PUBLIC_URL"):
        RemoteConfig.from_env()


def test_remote_bootstrap_is_side_effect_free_and_stateless() -> None:
    adapter = _RecordingAdapter()
    verifier = _StaticVerifier(_AUDIENCE)
    app = create_remote_app(adapter, config=_config(), token_verifier=verifier)

    assert adapter.calls == []
    assert app.state.session_manager.stateless is True
    assert [route.path for route in app.routes] == [
        "/mcp",
        "/.well-known/oauth-protected-resource/mcp",
    ]


def test_remote_http_protocol_read_sequence_and_tool_auth_metadata() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        verifier = _StaticVerifier(_AUDIENCE)
        app = create_remote_app(adapter, config=_config(), token_verifier=verifier)
        async with _client(app) as client:
            initialized = await client.post("/mcp", headers=_headers(), json=_initialize_payload())
            assert initialized.status_code == 200
            assert initialized.json()["result"]["serverInfo"]["name"] == "azurpilot-dev"

            notification = await client.post(
                "/mcp",
                headers=_headers(),
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            assert notification.status_code == 202

            listed = await client.post(
                "/mcp",
                headers=_headers(),
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            tools = listed.json()["result"]["tools"]
            assert len(tools) == len(DEV_MCP_TOOL_NAMES)
            assert tools == [tool.model_dump(by_alias=True, exclude_none=True) for tool in tool_definitions()]
            assert all(
                tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": [DEV_MCP_REQUIRED_SCOPE]}]
                for tool in tools
            )

            requests = (
                ("dev_get_contract", {}),
                ("dev_preflight", {}),
                ("dev_list_smoke_capabilities", {}),
                ("dev_list_game_observation_capabilities", {}),
                ("dev_get_game_observation", {"capability_id": "resources", "parameters": {}}),
                ("dev_list_database_checks", {}),
                ("dev_run_database_check", {"check_id": "connectivity"}),
                ("dev_list_database_repairs", {}),
            )
            for request_id, (tool_name, arguments) in enumerate(requests, start=3):
                result = await client.post(
                    "/mcp",
                    headers=_headers(),
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    },
                )
                assert result.status_code == 200
                assert result.json()["result"]["structuredContent"]["ok"] is True

            assert adapter.calls == list(requests)

    asyncio.run(scenario())


def test_remote_http_modern_discovery_and_tool_call() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        app = create_remote_app(
            adapter,
            config=_config(),
            token_verifier=_StaticVerifier(_AUDIENCE),
        )
        async with _client(app) as client:
            discover_headers = _headers(protocol_version="2026-07-28", method="server/discover")
            discovered = await client.post(
                "/mcp",
                headers=discover_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": _modern_params(),
                },
            )
            assert discovered.status_code == 200
            assert discovered.json()["result"]["supportedVersions"]
            assert "2026-07-28" in discovered.json()["result"]["supportedVersions"]

            list_headers = _headers(protocol_version="2026-07-28", method="tools/list")
            listed = await client.post(
                "/mcp",
                headers=list_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": _modern_params()},
            )
            assert listed.status_code == 200
            assert len(listed.json()["result"]["tools"]) == len(DEV_MCP_TOOL_NAMES)

            call_headers = _headers(protocol_version="2026-07-28", method="tools/call", name="dev_get_contract")
            called = await client.post(
                "/mcp",
                headers=call_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {**_modern_params(name="dev_get_contract"), "arguments": {}},
                },
            )
            assert called.status_code == 200
            assert called.json()["result"]["structuredContent"]["code"] == "TEST_OK"
            assert [name for name, _ in adapter.calls] == ["dev_get_contract"]

    asyncio.run(scenario())


def test_remote_stateless_shutdown_has_no_sdk_error_traceback(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        app = create_remote_app(
            _RecordingAdapter(),
            config=_config(),
            token_verifier=_StaticVerifier(_AUDIENCE),
        )
        async with _client(app) as client:
            response = await client.post("/mcp", headers=_headers(), json=_initialize_payload())
            assert response.status_code == 200

    caplog.set_level(logging.ERROR, logger="mcp.server.streamable_http")
    asyncio.run(scenario())
    assert not any(
        record.name == "mcp.server.streamable_http" and record.getMessage() == "Error in message router"
        for record in caplog.records
    )


def test_remote_tool_descriptors_match_pinned_stdio_client() -> None:
    async def scenario() -> None:
        app = create_remote_app(
            _RecordingAdapter(),
            config=_config(),
            token_verifier=_StaticVerifier(_AUDIENCE),
        )
        async with _client(app) as client:
            await client.post("/mcp", headers=_headers(), json=_initialize_payload())
            response = await client.post(
                "/mcp",
                headers=_headers(),
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200
            remote_tools = response.json()["result"]["tools"]

        parameters = StdioServerParameters(
            command=DEV_MCP_COMMAND,
            args=list(DEV_MCP_ARGS),
            cwd=str(_REPOSITORY_ROOT),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            local_tools = (await session.list_tools()).tools

        assert remote_tools == [tool.model_dump(by_alias=True, exclude_none=True) for tool in local_tools]

    asyncio.run(scenario())


def test_remote_http_screenshot_is_official_image_content() -> None:
    async def scenario() -> None:
        image = _png_1x1()
        app = create_remote_app(
            _ScreenshotAdapter(image),
            config=_config(),
            token_verifier=_StaticVerifier(_AUDIENCE),
        )
        async with _client(app) as client:
            response = await client.post(
                "/mcp",
                headers=_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "dev_get_screenshot", "arguments": {}},
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            image_content = result["content"][0]
            assert image_content["type"] == "image"
            assert image_content["mimeType"] == "image/png"
            assert base64.b64decode(image_content["data"]) == image
            assert result["structuredContent"]["details"]["screenshot"]["sha256"] == hashlib.sha256(image).hexdigest()
            assert "base64" not in result["content"][1]["text"]

    asyncio.run(scenario())


def test_remote_auth_host_origin_body_and_malformed_request_fail_closed() -> None:
    async def scenario() -> None:
        adapter = _RecordingAdapter()
        verifier = _StaticVerifier(_AUDIENCE)
        app = create_remote_app(adapter, config=_config(), token_verifier=verifier)
        async with _client(app) as client:
            missing = await client.post("/mcp", headers=_headers(auth=False), json=_initialize_payload())
            assert missing.status_code == 401
            assert missing.json() == {"error": "unauthorized"}
            assert (
                'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
                in missing.headers["www-authenticate"]
            )
            assert f'scope="{DEV_MCP_REQUIRED_SCOPE}"' in missing.headers["www-authenticate"]

            query_token = await client.post(
                "/mcp?access_token=valid-token",
                headers=_headers(auth=False),
                json=_initialize_payload(),
            )
            assert query_token.status_code == 401

            wrong_token = await client.post(
                "/mcp",
                headers={**_headers(), "Authorization": "Bearer wrong-token"},
                json=_initialize_payload(),
            )
            assert wrong_token.status_code == 401

            wrong_host = await client.post(
                "/mcp",
                headers={**_headers(), "Host": "attacker.example.test"},
                json=_initialize_payload(),
            )
            assert wrong_host.status_code == 421

            wrong_origin = await client.post(
                "/mcp",
                headers=_headers(origin="https://attacker.example.test"),
                json=_initialize_payload(),
            )
            assert wrong_origin.status_code == 403

            too_large = await client.post(
                "/mcp",
                headers=_headers(),
                content=b"x" * (1024 * 1024 + 1),
            )
            assert too_large.status_code == 413

            malformed = await client.post(
                "/mcp",
                headers=_headers(),
                content=b"{not-json",
            )
            assert malformed.status_code == 400
            assert "Traceback" not in malformed.text

            wrong_content_type = await client.post(
                "/mcp",
                headers={**_headers(), "Content-Type": "text/plain"},
                content=json.dumps(_initialize_payload()).encode("utf-8"),
            )
            assert wrong_content_type.status_code == 400

            healthy = await client.post("/mcp", headers=_headers(), json=_initialize_payload(9))
            assert healthy.status_code == 200

            # Host и Origin проверяются до токена, а токен — до разбора body.
            assert verifier.tokens == [
                "wrong-token",
                "valid-token",
                "valid-token",
                "valid-token",
                "valid-token",
            ]
            assert adapter.calls == []

    asyncio.run(scenario())


def test_remote_request_bounds_cover_concurrency_and_timeout() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked(scope: Any, receive: Any, send: Any) -> None:
            entered.set()
            await release.wait()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        limited = ConcurrencyLimitMiddleware(
            blocked,
            _config(max_concurrent_requests=1, concurrency_acquire_timeout_seconds=0.01),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=limited),
            base_url=_BASE_URL,
        ) as client:
            first_task = asyncio.create_task(client.get("/"))
            await asyncio.wait_for(entered.wait(), timeout=1)
            busy = await client.get("/")
            assert busy.status_code == 503
            release.set()
            first = await asyncio.wait_for(first_task, timeout=1)
            assert first.status_code == 200

        async def hanging(scope: Any, receive: Any, send: Any) -> None:
            await asyncio.Event().wait()

        timeout_app = RequestTimeoutMiddleware(hanging, _config(request_timeout_seconds=0.01))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=timeout_app),
            base_url=_BASE_URL,
        ) as client:
            timed_out = await client.get("/")
            assert timed_out.status_code == 504
            assert timed_out.json() == {"error": "request_timeout"}

        async def started_hanging(scope: Any, receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await asyncio.Event().wait()

        started_timeout_app = RequestTimeoutMiddleware(
            started_hanging,
            _config(request_timeout_seconds=0.01),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=started_timeout_app),
            base_url=_BASE_URL,
        ) as client:
            completed = await client.get("/")
            assert completed.status_code == 200
            assert completed.content == b""

        long_lived_started = asyncio.Event()
        long_lived_release = asyncio.Event()

        async def long_lived_get(scope: Any, receive: Any, send: Any) -> None:
            long_lived_started.set()
            await long_lived_release.wait()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        long_lived_app = RequestTimeoutMiddleware(
            long_lived_get,
            _config(request_timeout_seconds=0.01),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=long_lived_app),
            base_url=_BASE_URL,
        ) as client:
            request = asyncio.create_task(client.get("/mcp"))
            try:
                await asyncio.wait_for(long_lived_started.wait(), timeout=1)
                done, _ = await asyncio.wait({request}, timeout=0.05)
                assert not done
            finally:
                long_lived_release.set()
            response = await asyncio.wait_for(request, timeout=1)
            assert response.status_code == 200
            assert response.content == b"ok"

    asyncio.run(scenario())


def test_remote_stream_limiter_does_not_consume_request_capacity() -> None:
    async def scenario() -> None:
        stream_started = asyncio.Event()
        stream_release = asyncio.Event()

        async def endpoint(scope: Any, _receive: Any, send: Any) -> None:
            if scope.get("method") == "GET" and scope.get("path") == "/mcp":
                stream_started.set()
                await stream_release.wait()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})

        limited = ConcurrencyLimitMiddleware(
            endpoint,
            _config(max_concurrent_requests=1, concurrency_acquire_timeout_seconds=0.01),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=limited),
            base_url=_BASE_URL,
        ) as client:
            stream_request = asyncio.create_task(client.get("/mcp"))
            await asyncio.wait_for(stream_started.wait(), timeout=1)
            post = await client.post("/mcp", content=b"{}")
            assert post.status_code == 200
            second_stream = await client.get("/mcp")
            assert second_stream.status_code == 503
            stream_release.set()
            stream_response = await asyncio.wait_for(stream_request, timeout=1)
            assert stream_response.status_code == 200

    asyncio.run(scenario())


def test_remote_metadata_is_public_but_has_exact_cors_and_no_wildcard() -> None:
    async def scenario() -> None:
        app = create_remote_app(_RecordingAdapter(), config=_config(), token_verifier=_StaticVerifier(_AUDIENCE))
        async with _client(app) as client:
            metadata = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": _HOST, "Origin": "https://chatgpt.com"},
            )
            assert metadata.status_code == 200
            assert metadata.json() == {
                "resource": _AUDIENCE,
                "authorization_servers": [_ISSUER],
                "scopes_supported": [DEV_MCP_REQUIRED_SCOPE],
                "bearer_methods_supported": ["header"],
            }
            assert metadata.headers["access-control-allow-origin"] == "https://chatgpt.com"
            assert "*" not in metadata.headers["access-control-allow-origin"]

            options = await client.options(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": _HOST, "Origin": "https://chatgpt.com"},
            )
            assert options.status_code == 204
            assert options.headers["access-control-allow-origin"] == "https://chatgpt.com"

            bad_origin = await client.get(
                "/.well-known/oauth-protected-resource/mcp",
                headers={"Host": _HOST, "Origin": "https://attacker.example.test"},
            )
            assert bad_origin.status_code == 403

    asyncio.run(scenario())


def test_caddy_template_uses_loopback_backend_without_public_admin_or_forbidden_ports() -> None:
    template = (_REPOSITORY_ROOT / "infrastructure" / "caddy" / "Caddyfile").read_text(encoding="utf-8")

    assert "admin 127.0.0.1:2019" in template
    assert "reverse_proxy host.docker.internal:8765" in template
    assert "reverse_proxy host.docker.internal:8766" in template
    assert 'Cache-Control "no-store"' in template
    assert "response_header_timeout 195s" in template
    assert "header_up Authorization" not in template
    assert "file_server" not in template
    assert "0.0.0.0" not in template
    for forbidden_port in ("25549", "5432"):
        assert forbidden_port not in template


def test_remote_doctor_does_not_require_host_caddy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    values = {
        "AZURPILOT_DEV_MCP_PUBLIC_URL": _AUDIENCE,
        "AZURPILOT_DEV_MCP_OAUTH_ISSUER": _ISSUER,
        "AZURPILOT_DEV_MCP_OAUTH_AUDIENCE": _AUDIENCE,
        "AZURPILOT_DEV_MCP_OAUTH_JWKS_URL": _JWKS_URL,
        "AZURPILOT_DEV_MCP_OAUTH_SUBJECT": "user-1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "")

    assert doctor() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "code": "REMOTE_CONFIG_READY",
        "bind_host_loopback": True,
        "public_https_path": True,
    }


def test_remote_backend_uses_fixed_port(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(_app: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("module.dev_mcp.remote.uvicorn.run", fake_run)
    run_remote_server(config=_config())

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == DEV_MCP_PORT == 8765


def test_oidc_verifier_checks_signature_issuer_audience_expiry_subject_resource_and_scope() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    class _JwkClient:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            return SimpleNamespace(key=public_pem)

    config = _config(public_url="https://resource.example.test/mcp")
    now = int(time.time())
    verifier = OIDCTokenVerifier(config, jwk_client=_JwkClient(), clock=lambda: now)

    def token(*, include_resource: bool = True, **claims: Any) -> str:
        values = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": "user-1",
            "exp": now + 1_000,
            "nbf": now - 1,
            "scope": DEV_MCP_REQUIRED_SCOPE,
        }
        if include_resource:
            values["resource"] = config.public_url
        values.update(claims)
        return jwt.encode(values, private_pem, algorithm="RS256")

    async def scenario() -> None:
        valid = await verifier.verify_token(token(include_resource=False))
        assert valid is not None
        assert valid.token == ""
        assert valid.client_id == "user-1"
        assert valid.scopes == [DEV_MCP_REQUIRED_SCOPE]
        assert valid.resource == config.public_url

        with_resource = await verifier.verify_token(token())
        assert with_resource is not None
        assert with_resource.resource == config.public_url

        for invalid in (
            token(aud="https://other.example.test"),
            token(aud=None),
            token(iss="https://other.example.test"),
            token(exp=now - 1),
            token(nbf=now + 100),
            token(sub="other-user"),
            token(sub="пользователь"),
            token(resource=None),
            token(resource=_AUDIENCE),
            token(resource="https://other.example.test/mcp"),
            token(resource="https://пример.example/mcp"),
            token(scope="other:scope"),
        ):
            assert await verifier.verify_token(invalid) is None

    asyncio.run(scenario())
