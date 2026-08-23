"""Санитизированная проверка security posture production PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecurityPosture:
    listener: str
    password_encryption: str
    rules: tuple[dict[str, object], ...]


class SecurityPostureError(RuntimeError):
    """Security posture не соответствует production-контракту."""


def _contains(values: object, expected: str) -> bool:
    return isinstance(values, list) and expected in values


def validate_posture(posture: SecurityPosture) -> None:
    """Проверить HBA без публикации его фактического содержимого."""

    listeners = {
        value.strip() for value in posture.listener.split(",") if value.strip()
    }
    if not listeners or not listeners <= {"localhost", "127.0.0.1", "::1"}:
        raise SecurityPostureError("LISTENER_NOT_LOOPBACK_ONLY")
    if posture.password_encryption != "scram-sha-256":
        raise SecurityPostureError("PASSWORD_ENCRYPTION_NOT_SCRAM")

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
        if not loopback_v4 and not loopback_v6:
            raise SecurityPostureError("HBA_NON_LOOPBACK_HOST")
        if method != "scram-sha-256":
            raise SecurityPostureError("HBA_HOST_METHOD_NOT_SCRAM")
        if method == "scram-sha-256" and _contains(databases, "all") and _contains(users, "all"):
            host_v4_scram = host_v4_scram or loopback_v4
            host_v6_scram = host_v6_scram or loopback_v6

    if local_postgres_peer_index is None:
        raise SecurityPostureError("HBA_ADMIN_PEER_MISSING")
    if local_all_scram_index is None:
        raise SecurityPostureError("HBA_LOCAL_SCRAM_MISSING")
    if local_postgres_peer_index > local_all_scram_index:
        raise SecurityPostureError("HBA_ADMIN_PEER_SHADOWED")
    if not host_v4_scram or not host_v6_scram:
        raise SecurityPostureError("HBA_LOOPBACK_SCRAM_MISSING")


def _read_posture(distro: str) -> SecurityPosture:
    query = """
SELECT json_build_object(
  'listener', current_setting('listen_addresses'),
  'password_encryption', current_setting('password_encryption'),
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
    result = subprocess.run(
        [
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
        ],
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
        return SecurityPosture(
            listener=str(payload["listener"]),
            password_encryption=str(payload["password_encryption"]),
            rules=tuple(payload["rules"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecurityPostureError("POSTURE_RESPONSE_INVALID") from exc


def audit(distro: str = "Archlinux") -> None:
    validate_posture(_read_posture(distro))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить listener, SCRAM и HBA production PostgreSQL."
    )
    parser.add_argument("--distro", default="Archlinux")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        audit(arguments.distro)
    except (OSError, subprocess.SubprocessError, SecurityPostureError) as exc:
        reason = str(exc) if isinstance(exc, SecurityPostureError) else "POSTURE_AUDIT_FAILED"
        print(f"Проверка безопасности PostgreSQL не пройдена: {reason}", file=sys.stderr)
        return 1
    print("Listener, SCRAM и HBA production PostgreSQL соответствуют контракту.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
