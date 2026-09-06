"""Санитизированная проверка security posture production PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SecurityPosture:
    listener: str
    password_encryption: str
    hba_is_active: bool
    rules: tuple[dict[str, object], ...]
    deployment: str = "wsl"
    host_binding_loopback: bool = True


class SecurityPostureError(RuntimeError):
    """Security posture не соответствует production-контракту."""


def _contains(values: object, expected: str) -> bool:
    return isinstance(values, list) and expected in values


def validate_posture(posture: SecurityPosture) -> None:
    """Проверить HBA без публикации его фактического содержимого."""

    if posture.deployment not in {"docker", "wsl"}:
        raise SecurityPostureError("DEPLOYMENT_UNSUPPORTED")
    listeners = {
        value.strip() for value in posture.listener.split(",") if value.strip()
    }
    if posture.deployment == "docker":
        if not posture.host_binding_loopback:
            raise SecurityPostureError("HOST_BINDING_NOT_LOOPBACK_ONLY")
        if not listeners or not listeners <= {"*", "0.0.0.0", "::"}:
            raise SecurityPostureError("LISTENER_NOT_CONTAINER_WILDCARD")
    elif not listeners or not listeners <= {"localhost", "127.0.0.1", "::1"}:
        raise SecurityPostureError("LISTENER_NOT_LOOPBACK_ONLY")
    if posture.password_encryption != "scram-sha-256":
        raise SecurityPostureError("PASSWORD_ENCRYPTION_NOT_SCRAM")
    if posture.hba_is_active is not True:
        raise SecurityPostureError("HBA_CONFIGURATION_NOT_RELOADED")

    local_postgres_peer_index: int | None = None
    local_all_scram_index: int | None = None
    host_v4_scram = False
    host_v6_scram = False
    for index, rule in enumerate(posture.rules):
        if rule.get("error") is not None:
            raise SecurityPostureError("HBA_PARSE_ERROR")
        method = rule.get("auth_method")
        if method == "trust":
            raise SecurityPostureError("HBA_TRUST_PRESENT")
        rule_type = rule.get("type")
        databases = rule.get("database")
        users = rule.get("user_name")
        address = rule.get("address")
        netmask = rule.get("netmask")
        if rule_type == "local":
            if (
                method == "peer"
                and _contains(databases, "all")
                and _contains(users, "postgres")
            ):
                local_postgres_peer_index = index
                continue
            if (
                method == "scram-sha-256"
                and _contains(databases, "all")
                and _contains(users, "all")
            ):
                local_all_scram_index = index
                continue
            raise SecurityPostureError("HBA_LOCAL_RULE_UNSAFE")
        if rule_type not in {"host", "hostssl", "hostnossl"}:
            raise SecurityPostureError("HBA_RULE_TYPE_UNSUPPORTED")
        loopback_v4 = address == "127.0.0.1" and netmask == "255.255.255.255"
        loopback_v6 = (
            address == "::1"
            and netmask == "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
        )
        docker_v4 = address == "0.0.0.0" and netmask == "0.0.0.0"
        docker_v6 = address == "::" and netmask in {
            "::",
            "0:0:0:0:0:0:0:0",
        }
        docker_all = posture.deployment == "docker" and address == "all"
        if posture.deployment == "docker":
            allowed_host = (
                docker_v4
                or docker_v6
                or docker_all
                or loopback_v4
                or loopback_v6
            )
        else:
            allowed_host = loopback_v4 or loopback_v6
        if not allowed_host:
            raise SecurityPostureError("HBA_NON_LOOPBACK_HOST")
        if method != "scram-sha-256":
            raise SecurityPostureError("HBA_HOST_METHOD_NOT_SCRAM")
        if _contains(databases, "all") and _contains(users, "all"):
            host_v4_scram = host_v4_scram or (
                (docker_v4 or docker_all or loopback_v4)
                if posture.deployment == "docker"
                else loopback_v4
            )
            host_v6_scram = host_v6_scram or (
                (docker_v6 or docker_all or loopback_v6)
                if posture.deployment == "docker"
                else loopback_v6
            )

    if local_postgres_peer_index is None:
        raise SecurityPostureError("HBA_ADMIN_PEER_MISSING")
    if local_all_scram_index is None:
        raise SecurityPostureError("HBA_LOCAL_SCRAM_MISSING")
    if local_postgres_peer_index > local_all_scram_index:
        raise SecurityPostureError("HBA_ADMIN_PEER_SHADOWED")
    if not host_v4_scram or not host_v6_scram:
        raise SecurityPostureError("HBA_LOOPBACK_SCRAM_MISSING")


_DEFAULT_COMPOSE_FILE = (
    Path(__file__).resolve().parents[1] / "infrastructure/observability/compose.yaml"
)


def _docker_compose_arguments(compose_file: Path, service: str, *arguments: str) -> list[str]:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if executable is None:
        raise SecurityPostureError("DOCKER_CLI_UNAVAILABLE")
    compose_file = compose_file.resolve(strict=True)
    repository_root = compose_file.parents[2]
    env_file = repository_root / ".env"
    if not env_file.is_file():
        raise SecurityPostureError("DOCKER_ENV_UNAVAILABLE")
    return [
        executable,
        "compose",
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
        *arguments,
    ]


def _docker_port_is_loopback(output: str) -> bool:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        host_port = line.rsplit(":", 1)
        if len(host_port) != 2:
            return False
        host = host_port[0].strip("[]")
        if host not in {"127.0.0.1", "::1"}:
            return False
    return True


def _read_posture(
    distro: str = "Archlinux",
    *,
    deployment: str = "wsl",
    compose_file: str | Path = _DEFAULT_COMPOSE_FILE,
    service: str = "postgres",
) -> SecurityPosture:
    query = """
SELECT json_build_object(
  'listener', current_setting('listen_addresses'),
  'password_encryption', current_setting('password_encryption'),
  'hba_is_active', pg_conf_load_time() >=
    (pg_stat_file(current_setting('hba_file'))).modification,
  'rules', COALESCE((
    SELECT json_agg(json_build_object(
      'type', type,
      'database', database,
      'user_name', user_name,
      'address', address,
      'netmask', netmask,
      'auth_method', auth_method,
      'error', error
    ) ORDER BY line_number)
    FROM pg_hba_file_rules
  ), '[]'::json)
)
""".strip()
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    host_binding_loopback = True
    if deployment == "docker":
        compose_path = Path(compose_file)
        arguments = _docker_compose_arguments(
            compose_path,
            service,
            "exec",
            "-T",
            "--user",
            "postgres",
            service,
            "psql",
            "--username",
            "postgres",
            "--dbname",
            "postgres",
            "--tuples-only",
            "--no-align",
            "--quiet",
            "--command",
            query,
        )
        port_result = subprocess.run(
            _docker_compose_arguments(
                compose_path,
                service,
                "port",
                service,
                "5432",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            **options,
        )
        if port_result.returncode != 0 or not _docker_port_is_loopback(
            port_result.stdout
        ):
            raise SecurityPostureError("HOST_BINDING_NOT_LOOPBACK_ONLY")
    elif deployment == "wsl":
        arguments = [
            "wsl.exe",
            "--distribution",
            distro,
            "--exec",
            "sudo",
            "--non-interactive",
            "--user",
            "postgres",
            "psql",
            "--dbname",
            "postgres",
            "--tuples-only",
            "--no-align",
            "--quiet",
            "--command",
            query,
        ]
    else:
        raise SecurityPostureError("DEPLOYMENT_UNSUPPORTED")
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        **options,
    )
    if result.returncode != 0:
        raise SecurityPostureError("POSTURE_QUERY_FAILED")
    try:
        payload = json.loads(result.stdout.strip())
        rules = payload["rules"]
        if not isinstance(rules, list) or not all(
            isinstance(rule, dict) for rule in rules
        ):
            raise TypeError("rules должен быть списком JSON-объектов")
        return SecurityPosture(
            listener=str(payload["listener"]),
            password_encryption=str(payload["password_encryption"]),
            hba_is_active=payload["hba_is_active"],
            rules=tuple(rules),
            deployment=deployment,
            host_binding_loopback=host_binding_loopback,
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecurityPostureError("POSTURE_RESPONSE_INVALID") from exc


def audit(
    distro: str = "Archlinux",
    *,
    deployment: str = "docker",
    compose_file: str | Path = _DEFAULT_COMPOSE_FILE,
    service: str = "postgres",
) -> None:
    validate_posture(
        _read_posture(
            distro,
            deployment=deployment,
            compose_file=compose_file,
            service=service,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить listener, SCRAM и HBA production PostgreSQL."
    )
    parser.add_argument("--distro", default="Archlinux")
    parser.add_argument("--deployment", choices=("docker", "wsl"), default="docker")
    parser.add_argument("--compose-file", default=str(_DEFAULT_COMPOSE_FILE))
    parser.add_argument("--service", default="postgres")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        audit(
            arguments.distro,
            deployment=arguments.deployment,
            compose_file=arguments.compose_file,
            service=arguments.service,
        )
    except (OSError, subprocess.SubprocessError, SecurityPostureError) as exc:
        reason = str(exc) if isinstance(exc, SecurityPostureError) else "POSTURE_AUDIT_FAILED"
        print(f"Проверка безопасности PostgreSQL не пройдена: {reason}", file=sys.stderr)
        return 1
    print("Listener, SCRAM и HBA production PostgreSQL соответствуют контракту.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
