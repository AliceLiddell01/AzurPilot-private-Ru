import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_healthcheck_matches_docker_webui_port():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "config/deploy.template-docker.yaml").read_text(encoding="utf-8")

    assert "WebuiPort: 25548" in deploy
    assert "socket.create_connection(('127.0.0.1', 25548), 3)" in compose
    assert "restart: unless-stopped" in compose


def test_compose_uses_host_timezone_instead_of_cn_default():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "Asia/Shanghai" not in compose
    assert "'/etc/localtime:/etc/localtime:ro'" in compose


def test_postgres_18_is_part_of_observability_compose_with_named_volume():
    compose = (ROOT / "infrastructure/observability/compose.yaml").read_text(
        encoding="utf-8"
    )
    init_script = (
        ROOT / "infrastructure/observability/postgres/init/01-bootstrap.sh"
    ).read_text(encoding="utf-8")
    bootstrap_script = (
        ROOT / "infrastructure/observability/postgres/bootstrap/01-bootstrap.sh"
    ).read_text(encoding="utf-8")
    compose_data = yaml.safe_load(compose)
    services = compose_data["services"]
    postgres_block = services["postgres"]
    bootstrap_block = services["postgres-bootstrap"]

    assert compose_data["name"] == "azurpilot-infrastructure"
    assert postgres_block["image"].startswith("postgres:18@sha256:")
    assert any(
        volume["target"] == "/var/lib/postgresql" for volume in postgres_block["volumes"]
    )
    assert postgres_block["environment"]["PGDATA"] == "/var/lib/postgresql/18/docker"
    assert postgres_block["ports"] == [
        "127.0.0.1:${AZURPILOT_POSTGRES_PORT:-5432}:5432"
    ]
    assert postgres_block["security_opt"] == ["no-new-privileges:true"]
    assert postgres_block["restart"] == "unless-stopped"
    assert postgres_block["healthcheck"]["test"] == [
        "CMD-SHELL",
        "gosu postgres pg_isready -U postgres -d $${POSTGRES_DB}",
    ]
    assert "postgres_bootstrap_password" in postgres_block["secrets"]
    assert "postgres_app_password" not in postgres_block["secrets"]
    assert "postgres_migrator_password" not in postgres_block["secrets"]
    assert "postgres_app_password" in bootstrap_block["secrets"]
    assert "postgres_migrator_password" in bootstrap_block["secrets"]
    assert bootstrap_block["profiles"] == ["bootstrap"]
    assert compose_data["volumes"]["postgres-data"]["name"] == "azurpilot-postgres-data"
    assert compose_data["volumes"]["alloy-data"]["name"] == "azurpilot-observability_alloy-data"
    assert compose_data["volumes"]["grafana-data"]["name"] == "azurpilot-observability_grafana-data"
    assert "CREATE ROLE azurpilot_app" in bootstrap_script
    assert "CREATE ROLE azurpilot_migrator" in bootstrap_script
    assert "local all postgres peer" in init_script
    assert "local all all scram-sha-256" in init_script
    assert "SET log_statement = 'none';" in bootstrap_script
    assert "chmod --reference=\"$hba_file\"" in init_script
    assert "trap cleanup EXIT" in init_script
    assert "umask 077" in bootstrap_script
    assert "unset bootstrap_password app_password migrator_password app_sql migrator_sql" in bootstrap_script
    assert "to_regclass('public.alembic_version') IS NOT NULL" in (
        ROOT / "infrastructure/observability/postgres/grant-app.sql"
    ).read_text(encoding="utf-8")
    assert "\\set ON_ERROR_STOP on" in (
        ROOT / "infrastructure/observability/postgres/grant-app.sql"
    ).read_text(encoding="utf-8")


def test_caddy_is_pinned_profiled_and_keeps_mcp_backends_host_side():
    compose = (ROOT / "infrastructure/observability/compose.yaml").read_text(
        encoding="utf-8"
    )
    caddyfile = (ROOT / "infrastructure/caddy/Caddyfile").read_text(encoding="utf-8")
    compose_data = yaml.safe_load(compose)
    caddy = compose_data["services"]["caddy"]

    assert caddy["image"] == (
        "caddy:2.11.4-alpine@sha256:"
        "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    )
    assert caddy["profiles"] == ["remote-ingress"]
    assert caddy["restart"] == "unless-stopped"
    assert caddy["security_opt"] == ["no-new-privileges:true"]
    assert caddy["ports"] == ["80:80/tcp", "443:443/tcp", "443:443/udp"]
    assert caddy["environment"] == {
        "AZURPILOT_CADDY_HOST": "${AZURPILOT_CADDY_HOST:-}",
        "AZURPILOT_GAME_MCP_PUBLIC_HOST": "${AZURPILOT_GAME_MCP_PUBLIC_HOST:-}",
    }
    assert {
        (volume["type"], volume["source"], volume["target"], volume.get("read_only"))
        for volume in caddy["volumes"]
    } == {
        ("bind", "../caddy", "/etc/caddy", True),
        ("volume", "caddy-data", "/data", None),
        ("volume", "caddy-config", "/config", None),
    }
    assert caddy["healthcheck"]["test"] == [
        "CMD",
        "caddy",
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    ]
    assert "host.docker.internal:8765" in caddyfile
    assert "host.docker.internal:8766" in caddyfile
    assert "reverse_proxy 127.0.0.1:8765" not in caddyfile
    assert "reverse_proxy 127.0.0.1:8766" not in caddyfile
    assert compose_data["volumes"]["caddy-data"] == {"name": "azurpilot-caddy-data"}
    assert compose_data["volumes"]["caddy-config"] == {"name": "azurpilot-caddy-config"}


def test_pgadmin_is_loopback_only_and_preconfigured_for_postgres():
    compose = (ROOT / "infrastructure/observability/compose.yaml").read_text(
        encoding="utf-8"
    )
    servers = json.loads(
        (ROOT / "infrastructure/observability/pgadmin/servers.json").read_text(
            encoding="utf-8"
        )
    )
    pgadmin_block = yaml.safe_load(compose)["services"]["pgadmin"]
    server = servers["Servers"]["1"]

    assert pgadmin_block["image"].startswith("dpage/pgadmin4:9.17@sha256:")
    assert pgadmin_block["ports"] == [
        "127.0.0.1:${AZURPILOT_OBSERVABILITY_PGADMIN_PORT:-5050}:8080"
    ]
    assert pgadmin_block["environment"] == {
        "PGADMIN_DEFAULT_EMAIL": "${AZURPILOT_OBSERVABILITY_PGADMIN_ADMIN_EMAIL:-admin@azurpilot.dev}",
        "PGADMIN_DEFAULT_PASSWORD_FILE": "/run/secrets/pgadmin_admin_password",
        "PGADMIN_DISABLE_POSTFIX": "1",
        "PGADMIN_LISTEN_ADDRESS": "0.0.0.0",
        "PGADMIN_LISTEN_PORT": "8080",
        "PGADMIN_CONFIG_ENHANCED_COOKIE_PROTECTION": "True",
        "PGPASS_FILE": "/run/secrets/pgadmin_pgpass",
    }
    assert pgadmin_block["security_opt"] == ["no-new-privileges:true"]
    assert "postgres_bootstrap_password" not in pgadmin_block["secrets"]
    assert "postgres_migrator_password" not in pgadmin_block["secrets"]
    assert pgadmin_block["secrets"] == ["pgadmin_admin_password", "pgadmin_pgpass"]
    assert yaml.safe_load(compose)["volumes"]["pgadmin-data"]["name"] == "azurpilot-pgadmin-data"
    assert server == {
        "Name": "AzurPilot PostgreSQL",
        "Group": "AzurPilot",
        "Host": "postgres",
        "Port": 5432,
        "MaintenanceDB": "postgres",
        "Username": "azurpilot_migrator",
        "SSLMode": "prefer",
        "ConnectionParameters": {"sslmode": "prefer", "connect_timeout": 10},
        "Shared": False,
        "Comment": "Локальный Docker PostgreSQL; application database выбирается отдельно.",
    }


def test_grafana_datasources_provision_loki_tempo_incident_correlation():
    datasource_path = (
        ROOT
        / "infrastructure/observability/grafana/provisioning/datasources/datasources.yaml"
    )
    data = yaml.safe_load(datasource_path.read_text(encoding="utf-8"))
    datasources = {item["uid"]: item for item in data["datasources"]}

    assert set(datasources) == {"prometheus", "loki", "tempo"}
    loki = datasources["loki"]
    derived_field = loki["jsonData"]["derivedFields"][0]
    assert derived_field == {
        "datasourceUid": "tempo",
        "matcherRegex": "trace_id",
        "matcherType": "label",
        "name": "trace_id",
        "url": "$${__value.raw}",
        "urlDisplayLabel": "Открыть trace",
    }

    tempo = datasources["tempo"]
    assert tempo["jsonData"]["tracesToLogsV2"] == {
        "datasourceUid": "loki",
        "filterBySpanID": False,
        "filterByTraceID": True,
        "spanEndTimeShift": "1m",
        "spanStartTimeShift": "-1m",
        "tags": [{"key": "service.name", "value": "service_name"}],
    }

    loki_config = (
        ROOT / "infrastructure/observability/loki/config.yaml"
    ).read_text(encoding="utf-8")
    assert "trace_id" not in loki_config
    assert "span_id" not in loki_config


def test_grafana_operator_dashboards_and_alerts_are_provisioned_as_code():
    compose_path = ROOT / "infrastructure/observability/compose.yaml"
    compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    grafana_volumes = compose_data["services"]["grafana"]["volumes"]
    mounted_targets = {
        (volume["source"], volume["target"], volume.get("read_only"))
        for volume in grafana_volumes
        if volume["type"] == "bind"
    }
    assert (
        "./grafana/dashboards",
        "/var/lib/grafana/dashboards",
        True,
    ) in mounted_targets
    assert (
        "./grafana/provisioning/dashboards",
        "/etc/grafana/provisioning/dashboards",
        True,
    ) in mounted_targets
    assert (
        "./grafana/provisioning/alerting",
        "/etc/grafana/provisioning/alerting",
        True,
    ) in mounted_targets

    provider_path = (
        ROOT
        / "infrastructure/observability/grafana/provisioning/dashboards/providers.yaml"
    )
    provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    assert provider == {
        "apiVersion": 1,
        "providers": [
            {
                "name": "AzurPilot",
                "orgId": 1,
                "folder": "AzurPilot",
                "folderUid": "azurpilot",
                "type": "file",
                "disableDeletion": True,
                "updateIntervalSeconds": 30,
                "allowUiUpdates": False,
                "options": {
                    "path": "/var/lib/grafana/dashboards",
                    "foldersFromFilesStructure": False,
                },
            }
        ],
    }

    dashboard_root = ROOT / "infrastructure/observability/grafana/dashboards"
    dashboards = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(dashboard_root.glob("*.json"))
    }
    assert set(dashboards) == {"azurpilot-overview", "azurpilot-errors"}
    assert dashboards["azurpilot-overview"]["uid"] == "azurpilot-overview"
    assert dashboards["azurpilot-overview"]["title"] == "AzurPilot Overview"
    assert any(
        panel["title"] == "Успешные запуски"
        for panel in dashboards["azurpilot-overview"]["panels"]
    )
    assert dashboards["azurpilot-errors"]["uid"] == "azurpilot-errors"
    assert dashboards["azurpilot-errors"]["title"] == "AzurPilot Errors / Incidents"

    allowed_datasources = {"prometheus", "loki", "tempo"}
    for dashboard in dashboards.values():
        panel_ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(panel_ids) == len(set(panel_ids))
        for panel in dashboard["panels"]:
            datasource = panel.get("datasource")
            if datasource is not None:
                assert datasource["uid"] in allowed_datasources
            for target in panel.get("targets", []):
                target_datasource = target.get("datasource")
                if target_datasource is not None:
                    assert target_datasource["uid"] in allowed_datasources
        dashboard_text = json.dumps(dashboard, ensure_ascii=False)
        assert "task_run_id" not in dashboard_text
        assert "trace_id" not in dashboard_text

    overview_text = json.dumps(dashboards["azurpilot-overview"], ensure_ascii=False)
    assert "azurpilot_task_run_total" in overview_text
    assert "azurpilot_task_duration_seconds_bucket" in overview_text
    assert "detected_level = \\\"error\\\"" in overview_text
    assert "with (most_recent=true)" in overview_text

    alerting_path = (
        ROOT
        / "infrastructure/observability/grafana/provisioning/alerting/alert-rules.yaml"
    )
    alerting = yaml.safe_load(alerting_path.read_text(encoding="utf-8"))
    assert alerting["apiVersion"] == 1
    rules = alerting["groups"][0]["rules"]
    assert {rule["uid"] for rule in rules} == {
        "azurpilot-task-failures",
        "azurpilot-task-duration-p95",
    }
    alerting_text = alerting_path.read_text(encoding="utf-8")
    assert "azurpilot_task=" not in alerting_text
    assert "azurpilot_profile=" not in alerting_text
    for rule in rules:
        assert rule["condition"] == "C"
        assert rule["isPaused"] is False
        assert rule["noDataState"] == "OK"
        assert {item["datasourceUid"] for item in rule["data"]} == {
            "prometheus",
            "__expr__",
        }
