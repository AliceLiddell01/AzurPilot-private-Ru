import json
from pathlib import Path

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
    postgres_block = compose.split("  postgres:\n", 1)[1].split(
        "  postgres-bootstrap:\n", 1
    )[0]
    bootstrap_block = compose.split("  postgres-bootstrap:\n", 1)[1].split(
        "  alloy:\n", 1
    )[0]

    assert "name: azurpilot-infrastructure" in compose
    assert "postgres:18@sha256:" in compose
    assert "target: /var/lib/postgresql" in compose
    assert "PGDATA: /var/lib/postgresql/18/docker" in compose
    assert '"127.0.0.1:${AZURPILOT_POSTGRES_PORT:-5432}:5432"' in compose
    assert "external: true" in compose
    assert "no-new-privileges:true" in compose
    assert "name: azurpilot-postgres-data" in compose
    assert "name: azurpilot-observability_alloy-data" in compose
    assert "name: azurpilot-observability_grafana-data" in compose
    assert "restart: unless-stopped" in compose
    assert "gosu postgres pg_isready -U postgres -d $${POSTGRES_DB}" in compose
    assert "postgres_bootstrap_password" in postgres_block
    assert "postgres_app_password" not in postgres_block
    assert "postgres_migrator_password" not in postgres_block
    assert "postgres_app_password" in bootstrap_block
    assert "postgres_migrator_password" in bootstrap_block
    assert "profiles:\n      - bootstrap" in bootstrap_block
    assert "CREATE ROLE azurpilot_app" in bootstrap_script
    assert "CREATE ROLE azurpilot_migrator" in bootstrap_script
    assert "local all postgres peer" in init_script
    assert "local all all scram-sha-256" in init_script
    assert "SET log_statement = 'none';" in bootstrap_script
    assert "chmod --reference=\"$hba_file\"" in init_script
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
    pgadmin_block = compose.split("  pgadmin:\n", 1)[1].split("  alloy:\n", 1)[0]
    server = servers["Servers"]["1"]

    assert "dpage/pgadmin4:9.17@sha256:" in pgadmin_block
    assert '"127.0.0.1:${AZURPILOT_PGADMIN_PORT:-5050}:8080"' in pgadmin_block
    assert "PGADMIN_DEFAULT_PASSWORD_FILE: /run/secrets/pgadmin_admin_password" in pgadmin_block
    assert "PGADMIN_DISABLE_POSTFIX: \"1\"" in pgadmin_block
    assert "PGADMIN_LISTEN_PORT: \"8080\"" in pgadmin_block
    assert "PGADMIN_CONFIG_ENHANCED_COOKIE_PROTECTION: \"True\"" in pgadmin_block
    assert "PGPASS_FILE: /run/secrets/pgadmin_pgpass" in pgadmin_block
    assert "no-new-privileges:true" in pgadmin_block
    assert "postgres_bootstrap_password" not in pgadmin_block
    assert "postgres_migrator_password" not in pgadmin_block
    assert "name: azurpilot-pgadmin-data" in compose
    assert "pgadmin_admin_password" in pgadmin_block
    assert "pgadmin_pgpass" in pgadmin_block
    assert server == {
        "Name": "AzurPilot PostgreSQL",
        "Group": "AzurPilot",
        "Host": "postgres",
        "Port": 5432,
        "MaintenanceDB": "azurpilot",
        "Username": "azurpilot_migrator",
        "SSLMode": "prefer",
        "ConnectionParameters": {"sslmode": "prefer", "connect_timeout": 10},
        "Shared": False,
        "Comment": "Локальный Docker PostgreSQL с ролью azurpilot_migrator.",
    }
