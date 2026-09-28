from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from starlette.requests import Request

import module.mcp_shared.windows_mcp_bridge as bridge_module
from module.mcp_shared.windows_mcp_bridge import (
    BridgeTimeoutMiddleware,
    BridgeUpstreamObservation,
    create_windows_mcp_bridge_app,
)
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_BACKEND_IDENTITY_HEADER,
    BRIDGE_EXPECTED_IDENTITY_HEADER,
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_ROUTES,
    BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS,
    BridgeRoute,
    BridgeSourceIdentity,
    serialize_identity,
)


def _identity(**updates: object) -> BridgeSourceIdentity:
    values: dict[str, object] = {
        "identity_protocol": BRIDGE_IDENTITY_PROTOCOL,
        "server_name": "azurpilot-dev",
        "server_version": "1.2.3",
        "source_revision": "a" * 40,
        "source_set_digest": "b" * 64,
        "contract_revision": "c" * 64,
        "tool_catalog_sha256": "d" * 64,
        "capability_catalog_sha256": "e" * 64,
    }
    values.update(updates)
    return BridgeSourceIdentity.model_validate(values)


@dataclass
class _OutboundRequest:
    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None
    timeout: object


class _FakeUpstreamResponse:
    status_code = 200

    def __init__(self) -> None:
        self.headers = {"content-type": "application/json"}
        self.close_calls = 0

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        yield b'{"ok":true}'

    async def aread(self) -> bytes:
        return b'{"ok":true}'

    async def aclose(self) -> None:
        self.close_calls += 1


class _FakeUpstreamClient:
    def __init__(self) -> None:
        self.requests: list[_OutboundRequest] = []
        self.responses: list[_FakeUpstreamResponse] = []
        self.cookie_header: str | None = None
        self.set_cookie_responses = False

    def build_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        timeout: object = None,
    ) -> _OutboundRequest:
        request_headers = dict(headers)
        if self.cookie_header is not None:
            request_headers["cookie"] = self.cookie_header
        return _OutboundRequest(method, url, request_headers, content, timeout)

    async def send(
        self,
        request: _OutboundRequest,
        *,
        stream: bool,
        follow_redirects: bool,
    ) -> _FakeUpstreamResponse:
        assert stream is True
        assert follow_redirects is False
        self.requests.append(request)
        response = _FakeUpstreamResponse()
        self.responses.append(response)
        if self.set_cookie_responses:
            self.cookie_header = "session=from-upstream"
        return response


@asynccontextmanager
async def _bridge_client(app: Any):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8780",
    ) as client:
        yield client


def _make_app(
    *,
    upstream_identity: Callable[[BridgeRoute], BridgeSourceIdentity] | None = None,
    upstream_observation: BridgeUpstreamObservation | None = None,
) -> tuple[Any, _FakeUpstreamClient, list[BridgeRoute], list[str]]:
    fake_upstream = _FakeUpstreamClient()
    observed: list[BridgeRoute] = []
    internal_token_reads: list[str] = []

    def observe(route: BridgeRoute) -> BridgeUpstreamObservation:
        observed.append(route)
        if upstream_observation is not None:
            return upstream_observation
        identity = (
            upstream_identity(route)
            if upstream_identity is not None
            else _identity(server_name=route.server_name)
        )
        return BridgeUpstreamObservation("ready", "BRIDGE_UPSTREAM_READY", identity)

    def read_internal_token(_root: str | Path, server_name: str) -> str:
        internal_token_reads.append(server_name)
        return f"internal-{server_name}-secret"

    app = create_windows_mcp_bridge_app(
        Path("C:/bridge-test-project"),
        http_client=fake_upstream,  # type: ignore[arg-type]
        upstream_observer=observe,
        caller_token_reader=lambda _root: "bridge-caller-secret",
        internal_token_reader=read_internal_token,
    )
    return app, fake_upstream, observed, internal_token_reads


def test_bridge_entrypoint_refuses_non_windows_before_reading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_supports_windows_mcp_bridge", lambda: False)
    monkeypatch.setattr(
        bridge_module,
        "read_local_mcp_bridge_caller_token",
        lambda _root: pytest.fail("не следует читать учётные данные вне Windows"),
    )

    assert bridge_module.main([]) == 2


def _caller_headers(
    *, expected_identity: BridgeSourceIdentity | None = None
) -> dict[str, str]:
    headers = {
        "Host": "127.0.0.1:8780",
        "Authorization": "Bearer bridge-caller-secret",
    }
    if expected_identity is not None:
        headers[BRIDGE_EXPECTED_IDENTITY_HEADER] = serialize_identity(expected_identity)
    return headers


@pytest.mark.parametrize(
    ("path", "allowed_methods"),
    (
        ("/dev/mcp", "GET, POST, DELETE"),
        ("/game/mcp", "GET, POST, DELETE"),
        ("/identity/dev", "GET"),
        ("/identity/game", "GET"),
        ("/health", "GET"),
        ("/ready", "GET"),
    ),
)
def test_bridge_rejects_implicit_head_method(
    path: str, allowed_methods: str
) -> None:
    async def scenario() -> None:
        app, upstream, observed, internal_token_reads = _make_app()
        async with _bridge_client(app) as client:
            response = await client.head(path, headers=_caller_headers())

        assert response.status_code == 405
        assert response.headers["allow"] == allowed_methods
        assert upstream.requests == []
        assert observed == []
        assert internal_token_reads == []

    asyncio.run(scenario())


def test_bridge_rejects_missing_and_invalid_caller_auth_before_upstream_call() -> None:
    async def scenario() -> None:
        app, upstream, observed, internal_token_reads = _make_app()
        async with _bridge_client(app) as client:
            missing = await client.post(
                "/dev/mcp",
                headers={"Host": "127.0.0.1:8780"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            invalid = await client.post(
                "/dev/mcp",
                headers={
                    "Host": "127.0.0.1:8780",
                    "Authorization": "Bearer wrong-caller-secret",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )

        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "BRIDGE_CALLER_AUTH_INVALID"
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "BRIDGE_CALLER_AUTH_INVALID"
        assert observed == []
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


def test_bridge_requires_one_well_formed_expected_identity_before_observation() -> None:
    async def scenario() -> None:
        app, upstream, observed, internal_token_reads = _make_app()
        async with _bridge_client(app) as client:
            missing = await client.post(
                "/dev/mcp",
                headers=_caller_headers(),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            malformed = await client.post(
                "/dev/mcp",
                headers={
                    **_caller_headers(),
                    BRIDGE_EXPECTED_IDENTITY_HEADER: "not-json",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )

        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "BRIDGE_EXPECTED_IDENTITY_REQUIRED"
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "BRIDGE_REQUEST_INVALID"
        assert observed == []
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


def test_bridge_rejects_ready_upstream_without_runtime_identity() -> None:
    async def scenario() -> None:
        observation = BridgeUpstreamObservation(
            "ready", "BRIDGE_UPSTREAM_READY", identity=None
        )
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_observation=observation
        )
        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers=_caller_headers(expected_identity=_identity()),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            identity_response = await client.get(
                "/identity/dev",
                headers=_caller_headers(),
            )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN"
        assert identity_response.status_code == 503
        assert identity_response.json()["code"] == "BRIDGE_UPSTREAM_IDENTITY_UNKNOWN"
        assert observed == [BRIDGE_ROUTES["dev"], BRIDGE_ROUTES["dev"]]
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        ("server_name", "azurpilot-game"),
        ("server_version", "1.2.4"),
        ("source_revision", "f" * 40),
        ("source_set_digest", "1" * 64),
        ("contract_revision", "2" * 64),
        ("tool_catalog_sha256", "3" * 64),
        ("capability_catalog_sha256", "4" * 64),
    ),
)
def test_bridge_rejects_each_identity_mismatch_without_forwarding(
    field: str, changed_value: str
) -> None:
    async def scenario() -> None:
        actual = _identity()
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_identity=lambda _route: actual
        )
        expected = _identity(**{field: changed_value})

        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers=_caller_headers(expected_identity=expected),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "BRIDGE_SOURCE_MISMATCH"
        assert response.json()["error"]["mismatched_fields"] == [field]
        assert observed == [BRIDGE_ROUTES["dev"]]
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


def test_bridge_rejects_identity_from_wrong_route_family_without_forwarding() -> None:
    async def scenario() -> None:
        game_identity = _identity(server_name="azurpilot-game")
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_identity=lambda _route: game_identity
        )

        async with _bridge_client(app) as client:
            response = await client.post(
                "/game/mcp",
                headers=_caller_headers(expected_identity=_identity()),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "BRIDGE_SOURCE_MISMATCH"
        assert response.json()["error"]["mismatched_fields"] == ["server_name"]
        assert observed == [BRIDGE_ROUTES["game"]]
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


def test_bridge_forwards_exact_identity_to_fixed_upstream_with_internal_auth() -> None:
    async def scenario() -> None:
        identity = _identity()
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_identity=lambda _route: identity
        )

        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers={
                    **_caller_headers(expected_identity=identity),
                    "Origin": "http://127.0.0.1:8780",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert observed == [BRIDGE_ROUTES["dev"]]
        assert internal_token_reads == ["azurpilot-dev"]
        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request.method == "POST"
        assert request.url == "http://127.0.0.1:8775/mcp"
        assert request.headers["Host"] == "127.0.0.1:8775"
        assert request.headers["Origin"] == "http://127.0.0.1:8775"
        assert (
            request.headers["Authorization"] == "Bearer internal-azurpilot-dev-secret"
        )
        assert request.headers["Authorization"] != "Bearer bridge-caller-secret"
        assert request.headers[BRIDGE_BACKEND_IDENTITY_HEADER] == serialize_identity(
            identity
        )
        assert "bridge-caller-secret" not in request.headers.values()
        assert request.content is not None
        assert b'"method":"tools/list"' in request.content

    asyncio.run(scenario())


def test_bridge_preserves_modern_mcp_transport_headers_without_forwarding_caller_auth() -> None:
    async def scenario() -> None:
        identity = _identity()
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_identity=lambda _route: identity
        )

        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers={
                    **_caller_headers(expected_identity=identity),
                    "Origin": "http://127.0.0.1:8780",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Mcp-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "dev_get_contract",
                    "Mcp-Param-profile": "default",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "dev_get_contract", "arguments": {}},
                },
            )

        assert response.status_code == 200
        assert observed == [BRIDGE_ROUTES["dev"]]
        assert internal_token_reads == ["azurpilot-dev"]
        assert len(upstream.requests) == 1
        headers = upstream.requests[0].headers
        assert headers["mcp-protocol-version"] == "2026-07-28"
        assert headers["mcp-method"] == "tools/call"
        assert headers["mcp-name"] == "dev_get_contract"
        assert headers["mcp-param-profile"] == "default"
        assert headers["Authorization"] == "Bearer internal-azurpilot-dev-secret"
        assert "bridge-caller-secret" not in headers.values()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "extra_headers",
    (
        (("Mcp-Method", "tools/call"), ("mcp-method", "tools/call")),
        (("Mcp-Param-profile", "default"), ("mcp-param-profile", "other")),
        (("Mcp-Name", "x" * 513),),
        (("Mcp-Param-profile", "x" * 513),),
    ),
)
def test_bridge_rejects_duplicate_or_oversized_mcp_transport_headers(
    extra_headers: tuple[tuple[str, str], ...],
) -> None:
    async def scenario() -> None:
        identity = _identity()
        app, upstream, observed, internal_token_reads = _make_app(
            upstream_identity=lambda _route: identity
        )
        headers = [
            *_caller_headers(expected_identity=identity).items(),
            ("Origin", "http://127.0.0.1:8780"),
            ("Content-Type", "application/json"),
            ("Mcp-Protocol-Version", "2026-07-28"),
            *extra_headers,
        ]

        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BRIDGE_REQUEST_INVALID"
        assert observed == []
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw_headers",
    (
        ((b"mcp-param-bad name", b"value"),),
        ((b"mcp-param-" + b"a" * 120, b"value"),),
        ((b"mcp-param-a", b"one"), (b"MCP-PARAM-A", b"two")),
        ((b"mcp-param-a", b"value\r\ninjected: yes"),),
        (tuple((b"mcp-param-k" + str(index).encode(), b"v") for index in range(33))),
        tuple(
            (b"mcp-param-k" + str(index).encode(), b"v" * 512)
            for index in range(8)
        ),
    ),
)
def test_bridge_header_policy_rejects_invalid_parameter_header_shape(
    raw_headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    request = Request({"type": "http", "headers": list(raw_headers)})

    with pytest.raises(bridge_module.BridgeRequestError):
        bridge_module._forward_headers(request, BRIDGE_ROUTES["dev"])


def test_bridge_does_not_forward_shared_upstream_cookie_jar_between_routes() -> None:
    async def scenario() -> None:
        app, upstream, _, _ = _make_app()
        upstream.set_cookie_responses = True

        async with _bridge_client(app) as client:
            for route in (BRIDGE_ROUTES["dev"], BRIDGE_ROUTES["game"]):
                response = await client.post(
                    route.path,
                    headers=_caller_headers(
                        expected_identity=_identity(server_name=route.server_name)
                    ),
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert response.status_code == 200

        assert len(upstream.requests) == 2
        assert all("cookie" not in request.headers for request in upstream.requests)

    asyncio.run(scenario())


def test_bridge_uses_bounded_idle_timeout_for_long_lived_get_stream() -> None:
    async def scenario() -> None:
        identity = _identity()
        app, upstream, _, _ = _make_app(upstream_identity=lambda _route: identity)

        async with _bridge_client(app) as client:
            response = await client.get(
                "/dev/mcp",
                headers={
                    **_caller_headers(expected_identity=identity),
                    "Accept": "text/event-stream",
                },
            )

        assert response.status_code == 200
        assert len(upstream.requests) == 1
        timeout = upstream.requests[0].timeout
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS

    asyncio.run(scenario())


def test_bridge_bounds_downstream_writes_for_long_lived_get_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(bridge_module, "BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS", 0.01)
        messages: list[dict[str, object]] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            del scope, receive
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {
                    "type": "http.response.body",
                    "body": b"event: message\\ndata: {}\\n\\n",
                    "more_body": True,
                }
            )

        async def receive() -> dict[str, object]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "http.response.body":
                await asyncio.sleep(1)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dev/mcp",
            "headers": [],
        }
        with pytest.raises(TimeoutError):
            await BridgeTimeoutMiddleware(app)(scope, receive, send)

        assert [message["type"] for message in messages] == [
            "http.response.start",
            "http.response.body",
        ]

    asyncio.run(scenario())


def test_bridge_closes_upstream_when_downstream_disconnects_before_response_body() -> None:
    async def scenario() -> None:
        identity = _identity()
        app, upstream, _, _ = _make_app(upstream_identity=lambda _route: identity)
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                raise OSError("имитация отключения клиента")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/dev/mcp",
            "raw_path": b"/dev/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1:8780"),
                (b"authorization", b"Bearer bridge-caller-secret"),
                (
                    BRIDGE_EXPECTED_IDENTITY_HEADER.lower().encode("ascii"),
                    serialize_identity(identity).encode("ascii"),
                ),
                (b"content-length", b"0"),
            ],
            "server": ("127.0.0.1", 8780),
            "client": ("127.0.0.1", 12345),
        }

        await app(scope, receive, send)

        assert len(upstream.responses) == 1
        assert upstream.responses[0].close_calls == 1

    asyncio.run(scenario())


def test_bridge_bounds_chunked_get_body_before_rejecting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        messages = [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ]

        async def receive():
            return messages.pop(0)

        monkeypatch.setattr(bridge_module, "BRIDGE_MAX_REQUEST_BODY_BYTES", 4)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "headers": [(b"transfer-encoding", b"chunked")],
            },
            receive,
        )

        with pytest.raises(bridge_module.BridgeRequestError) as error:
            await bridge_module._read_bounded_body(request)

        assert error.value.code == "BRIDGE_REQUEST_TOO_LARGE"
        assert messages == []

    asyncio.run(scenario())


def test_bridge_rejects_arbitrary_path_as_non_proxy_route() -> None:
    async def scenario() -> None:
        app, upstream, observed, internal_token_reads = _make_app()
        async with _bridge_client(app) as client:
            response = await client.post(
                "/proxy/http://example.test/mcp",
                headers=_caller_headers(expected_identity=_identity()),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 404
        assert observed == []
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("host", "origin", "expected_status", "expected_code"),
    (
        ("evil.example.test:8780", None, 421, "BRIDGE_INVALID_HOST"),
        ("127.0.0.1:8780", "http://evil.example.test", 403, "BRIDGE_INVALID_ORIGIN"),
    ),
)
def test_bridge_rejects_spoofed_host_or_origin_before_upstream_call(
    host: str,
    origin: str | None,
    expected_status: int,
    expected_code: str,
) -> None:
    async def scenario() -> None:
        app, upstream, observed, internal_token_reads = _make_app()
        headers = _caller_headers(expected_identity=_identity())
        headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin

        async with _bridge_client(app) as client:
            response = await client.post(
                "/dev/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == expected_status
        assert response.json()["error"]["code"] == expected_code
        assert observed == []
        assert internal_token_reads == []
        assert upstream.requests == []

    asyncio.run(scenario())
