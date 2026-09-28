from __future__ import annotations

import json

import pytest

import module.mcp_shared.local_http_supervisor as supervisor_module
from module.mcp_shared.local_http_constants import LOCAL_HTTP_ENDPOINTS
from module.mcp_shared.local_http_supervisor import (
    LOCAL_HTTP_SERVICES,
    WINDOWS_MCP_BRIDGE_SERVICE,
    LocalHttpSupervisor,
    LocalHttpSupervisorError,
)
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_BIND_HOST,
    BRIDGE_EXPECTED_IDENTITY_HEADER,
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_MAX_IDENTITY_HEADER_BYTES,
    BRIDGE_PORT,
    BRIDGE_ROUTES,
    BRIDGE_ROUTES_BY_IDENTITY_PATH,
    BRIDGE_ROUTES_BY_PATH,
    BridgeIdentityError,
    BridgeSourceIdentity,
    identity_mismatch_fields,
    parse_expected_identity,
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


def test_bridge_routes_are_canonical_fixed_loopback_endpoints() -> None:
    assert (BRIDGE_BIND_HOST, BRIDGE_PORT) == ("127.0.0.1", 8780)
    assert set(BRIDGE_ROUTES) == {"dev", "game"}
    assert set(BRIDGE_ROUTES_BY_PATH) == {"/dev/mcp", "/game/mcp"}
    assert set(BRIDGE_ROUTES_BY_IDENTITY_PATH) == {
        "/identity/dev",
        "/identity/game",
    }

    expected = {
        "dev": (
            "/dev/mcp",
            "/identity/dev",
            "azurpilot-dev",
            LOCAL_HTTP_ENDPOINTS["azurpilot-dev"],
        ),
        "game": (
            "/game/mcp",
            "/identity/game",
            "azurpilot-game",
            LOCAL_HTTP_ENDPOINTS["azurpilot-game"],
        ),
    }
    for family, route in BRIDGE_ROUTES.items():
        path, identity_path, server_name, upstream_url = expected[family]
        assert (route.path, route.identity_path, route.server_name) == (
            path,
            identity_path,
            server_name,
        )
        assert route.upstream_url == upstream_url
        assert route.upstream_host in {"127.0.0.1:8775", "127.0.0.1:8776"}
        assert route.upstream_origin == f"http://{route.upstream_host}"
        assert route.readiness_url == f"{route.upstream_origin}/ready"
        assert route.bridge_url == f"http://127.0.0.1:8780{path}"
        assert BRIDGE_ROUTES_BY_PATH[path] is route
        assert BRIDGE_ROUTES_BY_IDENTITY_PATH[identity_path] is route


def test_bridge_supervisor_is_separate_and_does_not_receive_caller_token() -> None:
    assert tuple(service.name for service in LOCAL_HTTP_SERVICES) == (
        "azurpilot-dev",
        "azurpilot-game",
    )
    assert WINDOWS_MCP_BRIDGE_SERVICE.name == "windows-mcp-bridge"
    assert WINDOWS_MCP_BRIDGE_SERVICE.port == BRIDGE_PORT
    assert WINDOWS_MCP_BRIDGE_SERVICE.credential_kind == "bridge_caller"
    assert WINDOWS_MCP_BRIDGE_SERVICE.expose_token_to_child is False
    assert WINDOWS_MCP_BRIDGE_SERVICE.token_env_var != "AZURPILOT_DEV_LOCAL_MCP_TOKEN"
    assert WINDOWS_MCP_BRIDGE_SERVICE.token_env_var != "AZURPILOT_GAME_LOCAL_MCP_TOKEN"


def test_bridge_service_cannot_be_started_by_supervisor_outside_windows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module, "_supports_windows_mcp_bridge", lambda: False)

    with pytest.raises(LocalHttpSupervisorError, match="только в Windows"):
        LocalHttpSupervisor(tmp_path, services=(WINDOWS_MCP_BRIDGE_SERVICE,))


def test_expected_identity_round_trips_through_canonical_header() -> None:
    identity = _identity()

    serialized = serialize_identity(identity)

    assert len(serialized.encode("ascii")) <= BRIDGE_MAX_IDENTITY_HEADER_BYTES
    assert parse_expected_identity(serialized) == identity
    assert parse_expected_identity(serialized.encode("ascii")) == identity
    assert BRIDGE_EXPECTED_IDENTITY_HEADER == "x-azurpilot-expected-source-identity"


@pytest.mark.parametrize(
    "raw_identity",
    [
        None,
        "",
        b"\xff",
        "{malformed",
        (
            '{"identity_protocol":"azurpilot-mcp-source-identity/v1",'
            '"server_name":"azurpilot-dev","server_name":"azurpilot-dev"}'
        ),
        serialize_identity(_identity()).replace(
            '"server_name":"azurpilot-dev"',
            '"server_name":"azurpilot-dev","unexpected":true',
        ),
        " " * (BRIDGE_MAX_IDENTITY_HEADER_BYTES + 1),
    ],
)
def test_expected_identity_parser_rejects_missing_malformed_or_unbounded_input(
    raw_identity: str | bytes | None,
) -> None:
    with pytest.raises(BridgeIdentityError):
        parse_expected_identity(raw_identity)


def test_identity_mismatch_fields_returns_only_changed_fields_in_contract_order() -> (
    None
):
    expected = _identity()
    actual = _identity(
        server_name="azurpilot-game",
        server_version="2.0.0",
        source_revision="f" * 40,
        source_set_digest="1" * 64,
        contract_revision="2" * 64,
        tool_catalog_sha256="3" * 64,
        capability_catalog_sha256="4" * 64,
    )

    assert identity_mismatch_fields(expected, expected) == ()
    assert identity_mismatch_fields(expected, actual) == (
        "server_name",
        "server_version",
        "source_revision",
        "source_set_digest",
        "contract_revision",
        "tool_catalog_sha256",
        "capability_catalog_sha256",
    )

    one_field_changed = _identity(source_set_digest="1" * 64)
    assert identity_mismatch_fields(expected, one_field_changed) == (
        "source_set_digest",
    )


def test_identity_header_is_single_closed_json_object() -> None:
    identity = _identity()
    decoded = json.loads(serialize_identity(identity))
    assert set(decoded) == {
        "identity_protocol",
        "server_name",
        "server_version",
        "source_revision",
        "source_set_digest",
        "contract_revision",
        "tool_catalog_sha256",
        "capability_catalog_sha256",
    }
