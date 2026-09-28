from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager

import httpx2
import pytest

from azurpilot.integrations import mcp_client
from azurpilot.integrations.mcp_client import (
    HttpEndpointError,
    HttpTransportPolicy,
    McpCallPlan,
    call_http_tool,
)


def _patch_http_session_dependencies(monkeypatch: pytest.MonkeyPatch):
    options: list[dict[str, object]] = []

    class FakeHttpClient:
        def __init__(self, **kwargs: object) -> None:
            options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeClientSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    streamable_http = importlib.import_module("mcp.client.streamable_http")
    client_session = importlib.import_module("mcp.client.session")

    @asynccontextmanager
    async def fake_streamable_http_client(*_args: object, **_kwargs: object):
        yield object(), object()

    monkeypatch.setattr(httpx2, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(client_session, "ClientSession", FakeClientSession)
    return options


@pytest.mark.parametrize(
    ("endpoint", "policy", "trust_env"),
    (
        (
            "http://127.0.0.1:8775/mcp",
            HttpTransportPolicy.ISOLATED_LOOPBACK,
            False,
        ),
        (
            "https://context7.example.test/mcp",
            HttpTransportPolicy.REMOTE_HTTPS,
            True,
        ),
    ),
)
def test_http_client_uses_route_transport_policy_without_redirects(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    policy: HttpTransportPolicy,
    trust_env: bool,
) -> None:
    options = _patch_http_session_dependencies(monkeypatch)

    async def exercise() -> None:
        async with mcp_client._http_session(
            endpoint=endpoint,
            headers={"Authorization": "Bearer test"},
            timeout_seconds=1,
            transport_policy=policy,
        ):
            pass

    asyncio.run(exercise())

    assert options == [
        {
            "headers": {"Authorization": "Bearer test"},
            "timeout": 1,
            "follow_redirects": False,
            "trust_env": trust_env,
        }
    ]


def test_bearer_http_route_cannot_target_non_loopback_or_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _patch_http_session_dependencies(monkeypatch)
    plan = McpCallPlan(
        required_tools=frozenset({"read"}),
        probe_tool="read",
        arguments={},
    )

    async def exercise() -> None:
        await call_http_tool(
            endpoint="http://external.example.test/mcp",
            headers={"Authorization": "Bearer test"},
            tool_name="read",
            arguments={},
            timeout_seconds=1,
            plan=plan,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        )

    with pytest.raises(HttpEndpointError):
        asyncio.run(exercise())

    assert options == []


def test_transport_policies_reject_endpoint_type_mismatch_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = _patch_http_session_dependencies(monkeypatch)

    async def exercise() -> None:
        async with mcp_client._http_session(
            endpoint="https://external.example.test/mcp",
            headers={"Authorization": "Bearer local-test"},
            timeout_seconds=1,
            transport_policy=HttpTransportPolicy.ISOLATED_LOOPBACK,
        ):
            pass

    with pytest.raises(HttpEndpointError):
        asyncio.run(exercise())

    assert options == []

