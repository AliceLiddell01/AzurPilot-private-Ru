from pathlib import Path

import pytest

from dev_tools.observability_mcp import (
    EXPECTED_PROFILE_TOOL_SET,
    EXPECTED_PROFILE_TOOLS,
    ObservabilityMcpError,
    _runtime_tool_names_from_payload,
    load_and_validate_profile,
    validate_profile,
)

ROOT = Path(__file__).resolve().parents[1]


def test_grafana_mcp_profile_is_the_single_pinned_read_only_source():
    profile_path = ROOT / ".docker/azurpilot-observability-profile.json"
    profile = load_and_validate_profile(profile_path)
    server = profile["servers"][0]
    snapshot = server["snapshot"]["server"]

    assert not (ROOT / ".docker/grafana-mcp-server.yaml").exists()
    assert server["image"] == (
        "mcp/grafana@sha256:"
        "9362bcf6aa0e44e61f645b905cec03fb346a946a34a4dafecd7f3e28d3724014"
    )
    assert server["tools"] == list(EXPECTED_PROFILE_TOOLS)
    assert snapshot["secrets"] == [
        {"name": "grafana.api_key", "env": "GRAFANA_SERVICE_ACCOUNT_TOKEN"}
    ]
    assert snapshot["env"] == [
        {"name": "GRAFANA_URL", "value": "{{grafana.url}}"}
    ]
    assert "volumes" not in server
    assert "ports" not in server


def test_grafana_mcp_profile_has_a_bounded_read_allowlist():
    profile = load_and_validate_profile(
        ROOT / ".docker/azurpilot-observability-profile.json"
    )
    server = profile["servers"][0]
    snapshot = server["snapshot"]["server"]
    tools = server["tools"]
    tool_set = set(tools)

    assert profile["id"] == "azurpilot-observability"
    assert len(profile["servers"]) == 1
    assert server["config"] == {"url": "http://host.docker.internal:3000"}
    assert server["secrets"] == "default"
    assert server["image"] == snapshot["image"]
    assert snapshot["command"] == [
        "--transport=stdio",
        "--disable-write",
        "--max-loki-log-limit=50",
    ]
    assert len(tools) == 25
    assert len(set(tools)) == 25
    assert set(tools) == EXPECTED_PROFILE_TOOL_SET
    assert {
        "create_annotation",
        "create_datasource",
        "create_folder",
        "create_incident",
        "create_snapshot",
        "delete_snapshot",
        "grafana_api_request",
        "install_plugin",
        "update_annotation",
        "update_dashboard",
        "update_datasource",
    }.isdisjoint(tool_set)
    assert profile["secrets"]["default"]["provider"] == "docker-desktop-store"


@pytest.mark.parametrize(
    ("runtime_tools", "error_code"),
    [
        (
            list(EXPECTED_PROFILE_TOOLS) + ["mcp-add"],
            "MCP_RUNTIME_DYNAMIC_TOOL_PRESENT",
        ),
        (
            list(EXPECTED_PROFILE_TOOLS) + ["code-mode"],
            "MCP_RUNTIME_DYNAMIC_TOOL_PRESENT",
        ),
        (
            list(EXPECTED_PROFILE_TOOLS[:-1]) + ["mcp-exec"],
            "MCP_RUNTIME_DYNAMIC_TOOL_PRESENT",
        ),
    ],
)
def test_runtime_catalog_rejects_dynamic_or_unbounded_tools(
    runtime_tools, error_code
):
    with pytest.raises(ObservabilityMcpError, match=error_code):
        _runtime_tool_names_from_payload(
            [{"name": name} for name in runtime_tools]
        )


def test_runtime_catalog_accepts_exact_static_allowlist():
    names = _runtime_tool_names_from_payload(
        [{"name": name} for name in EXPECTED_PROFILE_TOOLS]
    )

    assert names == tuple(sorted(EXPECTED_PROFILE_TOOLS))


def test_profile_validation_rejects_duplicate_tools():
    profile = load_and_validate_profile(
        ROOT / ".docker/azurpilot-observability-profile.json"
    )
    profile["servers"][0]["tools"][-1] = profile["servers"][0]["tools"][0]

    with pytest.raises(ObservabilityMcpError, match="MCP_PROFILE_TOOL_DUPLICATE"):
        validate_profile(profile)
