from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dev_tools import infrastructure_doctor

_GAME_HOST = "play.mcp.example.test"


def _repository_fixture(tmp_path: Path) -> Path:
    (tmp_path / "infrastructure/caddy").mkdir(parents=True)
    (tmp_path / "infrastructure/observability").mkdir(parents=True)
    (tmp_path / ".env").write_text(
        "AZURPILOT_CADDY_HOST=mcp.example.test\n"
        f"AZURPILOT_GAME_MCP_PUBLIC_HOST={_GAME_HOST}\n",
        encoding="utf-8",
    )
    (tmp_path / "infrastructure/caddy/Caddyfile").write_text(
        "mcp.example.test {}\n", encoding="utf-8"
    )
    (tmp_path / "infrastructure/observability/compose.yaml").write_text(
        "name: azurpilot-infrastructure\n", encoding="utf-8"
    )
    return tmp_path


def _result(arguments: list[str], *, output: str = "", code: int = 0):
    return subprocess.CompletedProcess(arguments, code, output, "")


def test_doctor_distinguishes_absent_caddy_container(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)

    def fake_run(arguments: list[str], **_kwargs):
        if arguments == ["info"]:
            return _result(arguments)
        if "config" in arguments:
            return _result(arguments)
        if "ps" in arguments:
            return _result(arguments)
        raise AssertionError(arguments)

    monkeypatch.setattr(infrastructure_doctor, "_docker_executable", lambda: "docker")
    monkeypatch.setattr(infrastructure_doctor, "_run", fake_run)

    assert infrastructure_doctor.doctor(root) == {
        "ok": False,
        "code": "CADDY_CONTAINER_ABSENT",
        "project": "azurpilot-infrastructure",
        "service": "caddy",
    }


def test_doctor_reports_missing_repository_as_config_unavailable(
    tmp_path: Path,
) -> None:
    payload = infrastructure_doctor.doctor(tmp_path / "missing-repository")

    assert payload == {
        "ok": False,
        "code": "CADDY_CONFIG_UNAVAILABLE",
        "project": "azurpilot-infrastructure",
        "service": "caddy",
    }


def test_doctor_reports_ready_only_with_expected_published_ports(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)
    ports = {
        "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
        "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        "443/udp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        "2019/tcp": None,
    }
    ps_record = {
        "ID": "container-id",
        "Service": "caddy",
        "State": "running",
        "Health": "healthy",
    }

    def fake_run(arguments: list[str], **_kwargs):
        if arguments == ["info"]:
            return _result(arguments)
        if "config" in arguments:
            return _result(arguments)
        if "ps" in arguments:
            return _result(arguments, output=json.dumps(ps_record) + "\n")
        if arguments[:2] == ["inspect", "--format"]:
            return _result(arguments, output=json.dumps(ports))
        raise AssertionError(arguments)

    monkeypatch.setattr(infrastructure_doctor, "_docker_executable", lambda: "docker")
    monkeypatch.setattr(infrastructure_doctor, "_run", fake_run)

    payload = infrastructure_doctor.doctor(root)

    assert payload["ok"] is True
    assert payload["code"] == "CADDY_READY"
    assert payload["published_port_keys"] == ["443/tcp", "443/udp", "80/tcp"]


def test_doctor_rejects_published_caddy_admin_port(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)
    ports = {
        "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
        "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        "443/udp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        "2019/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2019"}],
    }
    ps_record = {
        "ID": "container-id",
        "Service": "caddy",
        "State": "running",
        "Health": "healthy",
    }

    def fake_run(arguments: list[str], **_kwargs):
        if arguments == ["info"] or "config" in arguments:
            return _result(arguments)
        if "ps" in arguments:
            return _result(arguments, output=json.dumps(ps_record) + "\n")
        if arguments[:2] == ["inspect", "--format"]:
            return _result(arguments, output=json.dumps(ports))
        raise AssertionError(arguments)

    monkeypatch.setattr(infrastructure_doctor, "_docker_executable", lambda: "docker")
    monkeypatch.setattr(infrastructure_doctor, "_run", fake_run)

    payload = infrastructure_doctor.doctor(root)

    assert payload["ok"] is False
    assert payload["code"] == "CADDY_PORTS_INVALID"
    assert payload["published_port_keys"] == [
        "2019/tcp",
        "443/tcp",
        "443/udp",
        "80/tcp",
    ]


def test_probe_validates_public_oauth_and_mcp_contract(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)
    metadata = {
        "resource": "https://mcp.example.test/mcp",
        "authorization_servers": ["https://auth.example.test"],
        "scopes_supported": ["azurpilot:dev"],
    }
    game_metadata = {
        "resource": f"https://{_GAME_HOST}/mcp",
        "authorization_servers": ["https://auth.example.test"],
        "scopes_supported": ["azurpilot:game.read", "azurpilot:game.control"],
    }

    def fake_request(host: str, path: str):
        if path == "/.well-known/oauth-protected-resource/mcp":
            body = game_metadata if host == _GAME_HOST else metadata
            return 200, {}, json.dumps(body).encode()
        metadata_host = _GAME_HOST if host == _GAME_HOST else host
        scope = "azurpilot:game.read" if host == _GAME_HOST else "azurpilot:dev"
        return (
            401,
            {
                "www-authenticate": (
                    f'Bearer resource_metadata="https://{metadata_host}/'
                    f'.well-known/oauth-protected-resource/mcp", scope="{scope}"'
                )
            },
            b"",
        )

    monkeypatch.setattr(infrastructure_doctor, "_https_request", fake_request)

    payload = infrastructure_doctor.probe(root)

    assert payload == {
        "ok": True,
        "code": "CADDY_PUBLIC_MCP_READY",
        "project": "azurpilot-infrastructure",
        "service": "caddy",
        "endpoints": {
            "dev": {"metadata_status": 200, "mcp_status": 401},
            "game": {"metadata_status": 200, "mcp_status": 401},
        },
    }


def test_probe_rejects_cross_wired_resource_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)

    def fake_request(host: str, path: str):
        if path.endswith("oauth-protected-resource/mcp"):
            resource = f"https://{host}/mcp"
            scopes = (
                ["azurpilot:game.read", "azurpilot:game.control"]
                if host == _GAME_HOST
                else ["azurpilot:dev"]
            )
            return 200, {}, json.dumps(
                {
                    "resource": resource,
                    "authorization_servers": ["https://auth.example.test"],
                    "scopes_supported": scopes,
                }
            ).encode()
        wrong_host = "mcp.example.test" if host == _GAME_HOST else _GAME_HOST
        return (
            401,
            {
                "www-authenticate": (
                    f'Bearer resource_metadata="https://{wrong_host}/'
                    '.well-known/oauth-protected-resource/mcp", scope="azurpilot:game.read"'
                )
            },
            b"",
        )

    monkeypatch.setattr(infrastructure_doctor, "_https_request", fake_request)

    payload = infrastructure_doctor.probe(root)

    assert payload["ok"] is False
    assert payload["code"] == "CADDY_MCP_CONTRACT_INVALID"


def test_probe_requires_all_scopes_from_game_contract(
    tmp_path: Path, monkeypatch
) -> None:
    root = _repository_fixture(tmp_path)

    def fake_request(host: str, path: str):
        if path.endswith("oauth-protected-resource/mcp"):
            return 200, {}, json.dumps(
                {
                    "resource": f"https://{host}/mcp",
                    "authorization_servers": ["https://auth.example.test"],
                    "scopes_supported": [
                        "azurpilot:game.read"
                        if host == _GAME_HOST
                        else "azurpilot:dev"
                    ],
                }
            ).encode()
        scope = "azurpilot:game.read" if host == _GAME_HOST else "azurpilot:dev"
        return (
            401,
            {
                "www-authenticate": (
                    f'Bearer resource_metadata="https://{host}/'
                    f'.well-known/oauth-protected-resource/mcp", scope="{scope}"'
                )
            },
            b"",
        )

    monkeypatch.setattr(infrastructure_doctor, "_https_request", fake_request)

    payload = infrastructure_doctor.probe(root)

    assert payload["ok"] is False
    assert payload["code"] == "CADDY_PUBLIC_ENDPOINT_INVALID"
