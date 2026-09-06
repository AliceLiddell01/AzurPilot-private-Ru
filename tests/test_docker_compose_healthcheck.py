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
