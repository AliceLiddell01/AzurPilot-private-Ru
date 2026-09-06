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

    assert "name: azurpilot-infrastructure" in compose
    assert "postgres:18@sha256:" in compose
    assert "target: /var/lib/postgresql" in compose
    assert "PGDATA: /var/lib/postgresql/18/docker" in compose
    assert '"127.0.0.1:${AZURPILOT_POSTGRES_PORT:-5432}:5432"' in compose
    assert "name: azurpilot-postgres-data" in compose
    assert "name: azurpilot-observability_alloy-data" in compose
    assert "name: azurpilot-observability_grafana-data" in compose
    assert "restart: unless-stopped" in compose
    assert "pg_isready -U postgres -d $${POSTGRES_DB}" in compose
    assert "postgres_bootstrap_password" in compose
    assert "CREATE ROLE azurpilot_app" in init_script
    assert "CREATE ROLE azurpilot_migrator" in init_script
    assert "local all postgres peer" in init_script
    assert "local all all scram-sha-256" in init_script
