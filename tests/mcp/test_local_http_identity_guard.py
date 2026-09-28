from __future__ import annotations

import asyncio
from typing import Any

import httpx2 as httpx
from starlette.responses import JSONResponse

from module.mcp_shared.local_http import LocalBackendIdentityGuardMiddleware
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_BACKEND_IDENTITY_HEADER,
    BRIDGE_IDENTITY_PROTOCOL,
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


def _identity_metadata(identity: BridgeSourceIdentity) -> dict[str, object]:
    # В метаданных готовности серверной части нет поля identity_protocol;
    # проверка добавляет признак протокола при построении закрытой модели идентичности.
    values = identity.model_dump(mode="python")
    values.pop("identity_protocol")
    return values


def test_identity_guard_rejects_mismatch_before_calling_application() -> None:
    async def scenario() -> None:
        calls = 0

        async def app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal calls
            calls += 1
            await JSONResponse({"called": True})(scope, receive, send)

        actual = _identity()
        guard = LocalBackendIdentityGuardMiddleware(app, _identity_metadata(actual))
        expected = _identity(source_revision="f" * 40)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=guard),
            base_url="http://127.0.0.1:8775",
        ) as client:
            response = await client.post(
                "/mcp",
                headers={BRIDGE_BACKEND_IDENTITY_HEADER: serialize_identity(expected)},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 409
        assert response.json() == {"error": "source_identity_mismatch"}
        assert calls == 0

    asyncio.run(scenario())


def test_identity_guard_allows_matching_identity_to_reach_application() -> None:
    async def scenario() -> None:
        calls = 0

        async def app(scope: Any, receive: Any, send: Any) -> None:
            nonlocal calls
            calls += 1
            await JSONResponse({"called": True})(scope, receive, send)

        identity = _identity()
        guard = LocalBackendIdentityGuardMiddleware(app, _identity_metadata(identity))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=guard),
            base_url="http://127.0.0.1:8775",
        ) as client:
            response = await client.post(
                "/mcp",
                headers={BRIDGE_BACKEND_IDENTITY_HEADER: serialize_identity(identity)},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 200
        assert response.json() == {"called": True}
        assert calls == 1

    asyncio.run(scenario())
