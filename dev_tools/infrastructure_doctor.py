"""Безопасная диагностика Docker Caddy ingress без управления MCP backend."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CANONICAL_PROJECT = "azurpilot-infrastructure"
CADDY_SERVICE = "caddy"
CADDY_PROFILE = "remote-ingress"
COMPOSE_RELATIVE_PATH = Path("infrastructure/observability/compose.yaml")
CADDYFILE_RELATIVE_PATH = Path("infrastructure/caddy/Caddyfile")
ENV_RELATIVE_PATH = Path(".env")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CADDY_HOST_KEY = "AZURPILOT_CADDY_HOST"
_EXPECTED_PUBLISHED_PORTS = frozenset({"80/tcp", "443/tcp", "443/udp"})
_DEV_SCOPE = "azurpilot:dev"
_GAME_SCOPE = "azurpilot:game.read"


def _docker_executable() -> str:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if executable is None:
        raise FileNotFoundError("docker")
    return executable


def _run(
    arguments: list[str], *, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [_docker_executable(), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        **options,
    )


def _compose_arguments(repository_root: Path, *arguments: str) -> list[str]:
    root = repository_root.resolve(strict=True)
    env_file = root / ENV_RELATIVE_PATH
    compose_file = root / COMPOSE_RELATIVE_PATH
    if not env_file.is_file() or not compose_file.is_file():
        raise FileNotFoundError("canonical compose")
    return [
        "compose",
        "--project-name",
        CANONICAL_PROJECT,
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
        *arguments,
    ]


def _parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _configured_caddy_host(env_file: Path) -> str | None:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeError:
        return None

    values: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key) or key != _CADDY_HOST_KEY:
            continue
        values.append(_parse_env_value(raw_value))
    if len(values) != 1 or not values[0] or any(char.isspace() for char in values[0]):
        return None
    try:
        parsed = urlsplit(f"https://{values[0]}")
        port = parsed.port
    except ValueError:
        return None
    if not (
        parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
        and port is None
        and "*" not in values[0]
    ):
        return None
    return values[0]


def _caddy_host_is_configured(env_file: Path) -> bool:
    return _configured_caddy_host(env_file) is not None


def _json_lines(raw: str) -> list[dict[str, Any]] | None:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        records.append(value)
    return records


def _payload(ok: bool, code: str, **details: object) -> dict[str, object]:
    return {
        "ok": ok,
        "code": code,
        "project": CANONICAL_PROJECT,
        "service": CADDY_SERVICE,
        **details,
    }


def _published_ports(container_id: str) -> tuple[bool, dict[str, object]]:
    try:
        result = _run(
            ["inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id]
        )
    except OSError, subprocess.SubprocessError:
        return False, {}
    if result.returncode != 0:
        return False, {}
    try:
        ports = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, {}
    if not isinstance(ports, dict):
        return False, {}
    observed = {str(key) for key in ports}
    if observed != _EXPECTED_PUBLISHED_PORTS:
        return False, {"published_port_keys": sorted(observed)}
    for key, bindings in ports.items():
        if not isinstance(bindings, list) or not bindings:
            return False, {"published_port_keys": sorted(observed)}
        expected_host_port = str(key).split("/", 1)[0]
        for binding in bindings:
            if (
                not isinstance(binding, dict)
                or str(binding.get("HostPort")) != expected_host_port
            ):
                return False, {"published_port_keys": sorted(observed)}
    return True, {"published_port_keys": sorted(observed)}


def _https_request(host: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPSConnection(
        host,
        443,
        timeout=10,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        body = response.read(64 * 1024)
        headers = {key.casefold(): value for key, value in response.getheaders()}
        return response.status, headers, body
    finally:
        connection.close()


class _PublicProbeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _probe_public_endpoint(host: str, *, scope: str) -> dict[str, int]:
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    try:
        metadata_status, _, metadata_body = _https_request(host, metadata_path)
    except OSError, http.client.HTTPException:
        raise _PublicProbeError("CADDY_DNS_TLS_UNAVAILABLE") from None
    if metadata_status != 200:
        raise _PublicProbeError("CADDY_PUBLIC_ENDPOINT_INVALID")
    try:
        metadata = json.loads(metadata_body)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise _PublicProbeError("CADDY_PUBLIC_ENDPOINT_INVALID") from None
    scopes = metadata.get("scopes_supported") if isinstance(metadata, dict) else None
    authorization_servers = (
        metadata.get("authorization_servers") if isinstance(metadata, dict) else None
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("resource") != f"https://{host}/mcp"
        or not isinstance(authorization_servers, list)
        or not authorization_servers
        or any(
            not isinstance(server, str) or not server
            for server in authorization_servers
        )
        or not isinstance(scopes, list)
        or scope not in scopes
    ):
        raise _PublicProbeError("CADDY_PUBLIC_ENDPOINT_INVALID")

    try:
        mcp_status, headers, _ = _https_request(host, "/mcp")
    except OSError, http.client.HTTPException:
        raise _PublicProbeError("CADDY_BACKEND_UNAVAILABLE") from None
    challenge = headers.get("www-authenticate", "")
    if mcp_status in {502, 503, 504}:
        raise _PublicProbeError("CADDY_BACKEND_UNAVAILABLE")
    if (
        mcp_status != 401
        or "bearer" not in challenge.casefold()
        or "resource_metadata=" not in challenge.casefold()
    ):
        raise _PublicProbeError("CADDY_MCP_CONTRACT_INVALID")
    return {"metadata_status": metadata_status, "mcp_status": mcp_status}


def probe(repository_root: Path = Path(".")) -> dict[str, object]:
    """Проверить DNS/TLS и read-only public MCP postconditions."""

    try:
        root = repository_root.resolve(strict=True)
        host = _configured_caddy_host(root / ENV_RELATIVE_PATH)
        if host is None or not (root / CADDYFILE_RELATIVE_PATH).is_file():
            return _payload(False, "CADDY_CONFIG_INVALID")
    except OSError:
        return _payload(False, "CADDY_CONFIG_UNAVAILABLE")

    try:
        endpoints = {
            "dev": _probe_public_endpoint(host, scope=_DEV_SCOPE),
            "game": _probe_public_endpoint(f"game.{host}", scope=_GAME_SCOPE),
        }
    except _PublicProbeError as exc:
        return _payload(False, exc.code)
    return _payload(True, "CADDY_PUBLIC_MCP_READY", endpoints=endpoints)


def doctor(repository_root: Path = Path(".")) -> dict[str, object]:
    """Проверить Docker Caddy state; backend и OAuth проверяются отдельно."""

    try:
        root = repository_root.resolve(strict=True)
        env_file = root / ENV_RELATIVE_PATH
        caddyfile = root / CADDYFILE_RELATIVE_PATH
        if not env_file.is_file() or not caddyfile.is_file():
            return _payload(False, "CADDY_CONFIG_UNAVAILABLE")
        if not _caddy_host_is_configured(env_file):
            return _payload(False, "CADDY_CONFIG_INVALID")
        _docker_executable()
    except FileNotFoundError, OSError:
        return _payload(False, "DOCKER_UNAVAILABLE")

    try:
        info = _run(["info"], timeout=30)
    except OSError, subprocess.SubprocessError:
        return _payload(False, "DOCKER_UNAVAILABLE")
    if info.returncode != 0:
        return _payload(False, "DOCKER_UNAVAILABLE")

    try:
        config_arguments = _compose_arguments(
            root, "--profile", CADDY_PROFILE, "config", "--quiet"
        )
        ps_arguments = _compose_arguments(
            root,
            "--profile",
            CADDY_PROFILE,
            "ps",
            "--all",
            "--format",
            "json",
        )
    except FileNotFoundError, OSError:
        return _payload(False, "CADDY_CONFIG_UNAVAILABLE")

    try:
        config = _run(config_arguments, timeout=60)
    except OSError, subprocess.SubprocessError:
        return _payload(False, "CADDY_CONFIG_UNAVAILABLE")
    if config.returncode != 0:
        return _payload(False, "CADDY_CONFIG_INVALID")
    try:
        status = _run(ps_arguments, timeout=60)
    except OSError, subprocess.SubprocessError:
        return _payload(False, "CADDY_STATUS_UNAVAILABLE")
    if status.returncode != 0:
        return _payload(False, "CADDY_STATUS_UNAVAILABLE")
    records = _json_lines(status.stdout)
    if records is None:
        return _payload(False, "CADDY_STATUS_INVALID")
    caddy_records = [
        record for record in records if record.get("Service") == CADDY_SERVICE
    ]
    if not caddy_records:
        return _payload(False, "CADDY_CONTAINER_ABSENT")
    record = caddy_records[0]
    state = str(record.get("State", "")).casefold()
    health = str(record.get("Health", "")).casefold()
    details: dict[str, object] = {"state": state, "health": health}
    if state not in {"running", "up"}:
        return _payload(False, "CADDY_CONTAINER_STOPPED", **details)
    if health == "starting":
        return _payload(False, "CADDY_HEALTHCHECK_STARTING", **details)
    if health == "unhealthy":
        return _payload(False, "CADDY_HEALTHCHECK_FAILED", **details)
    container_id = str(record.get("ID", ""))
    if not container_id:
        return _payload(False, "CADDY_STATUS_INVALID", **details)
    ports_ok, port_details = _published_ports(container_id)
    details.update(port_details)
    if not ports_ok:
        return _payload(False, "CADDY_PORTS_INVALID", **details)
    return _payload(True, "CADDY_READY", **details)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Диагностика Docker Caddy ingress AzurPilot."
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Проверить Docker Caddy state.")
    subparsers.add_parser("probe", help="Проверить public TLS и read-only MCP.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    payload = (
        doctor(arguments.repository_root)
        if arguments.command == "doctor"
        else probe(arguments.repository_root)
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
