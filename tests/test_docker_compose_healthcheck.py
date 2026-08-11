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
