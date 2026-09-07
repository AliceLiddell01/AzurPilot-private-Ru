import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_grafana_mcp_server_definition_is_pinned_and_read_only():
    server = yaml.safe_load(
        (ROOT / ".docker/grafana-mcp-server.yaml").read_text(encoding="utf-8")
    )

    assert server["name"] == "grafana"
    assert server["type"] == "server"
    assert server["image"] == (
        "mcp/grafana@sha256:"
        "9362bcf6aa0e44e61f645b905cec03fb346a946a34a4dafecd7f3e28d3724014"
    )
    assert server["command"] == [
        "--transport=stdio",
        "--disable-write",
        "--max-loki-log-limit=50",
    ]
    assert server["secrets"] == [
        {"name": "grafana.api_key", "env": "GRAFANA_API_KEY"}
    ]
    assert server["env"] == [
        {"name": "GRAFANA_URL", "value": "{{grafana.url}}"}
    ]
    assert "volumes" not in server
    assert "ports" not in server


def test_grafana_mcp_profile_has_a_bounded_read_allowlist():
    profile = json.loads(
        (ROOT / ".docker/azurpilot-observability-profile.json").read_text(
            encoding="utf-8"
        )
    )
    server = profile["servers"][0]
    snapshot = server["snapshot"]["server"]
    tools = set(server["tools"])

    assert profile["id"] == "azurpilot-observability"
    assert server["config"] == {"url": "http://host.docker.internal:3000"}
    assert server["secrets"] == "default"
    assert server["image"] == snapshot["image"]
    assert snapshot["command"] == [
        "--transport=stdio",
        "--disable-write",
        "--max-loki-log-limit=50",
    ]
    assert len(tools) == 25
    assert {
        "check_datasources_health",
        "get_dashboard_panel_queries",
        "get_dashboard_property",
        "get_dashboard_summary",
        "get_datasource",
        "list_datasources",
        "list_loki_label_names",
        "list_loki_label_values",
        "list_prometheus_label_names",
        "list_prometheus_label_values",
        "list_prometheus_metric_metadata",
        "list_prometheus_metric_names",
        "query_loki_logs",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "generate_deeplink",
        "alerting_manage_rules",
        "tempo_docs-traceql",
        "tempo_get-attribute-names",
        "tempo_get-attribute-values",
        "tempo_get-trace",
        "tempo_traceql-metrics-instant",
        "tempo_traceql-metrics-range",
        "tempo_traceql-search",
    } == tools
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
    }.isdisjoint(tools)
    assert profile["secrets"]["default"]["provider"] == "docker-desktop-store"
