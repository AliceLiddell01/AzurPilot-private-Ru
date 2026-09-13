from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_tools import postgresql_security
from dev_tools.postgresql_security import (
    SecurityPosture,
    SecurityPostureError,
    _docker_compose_arguments,
    _docker_port_is_loopback,
    _read_posture,
    validate_posture,
)


def _posture() -> SecurityPosture:
    return SecurityPosture(
        listener="localhost",
        password_encryption="scram-sha-256",
        hba_is_active=True,
        deployment="wsl",
        rules=(
            {
                "type": "local",
                "database": ["all"],
                "user_name": ["postgres"],
                "address": None,
                "netmask": None,
                "auth_method": "peer",
                "error": None,
            },
            {
                "type": "local",
                "database": ["all"],
                "user_name": ["all"],
                "address": None,
                "netmask": None,
                "auth_method": "scram-sha-256",
                "error": None,
            },
            {
                "type": "host",
                "database": ["all"],
                "user_name": ["all"],
                "address": "127.0.0.1",
                "netmask": "255.255.255.255",
                "auth_method": "scram-sha-256",
                "error": None,
            },
            {
                "type": "host",
                "database": ["all"],
                "user_name": ["all"],
                "address": "::1",
                "netmask": "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
                "auth_method": "scram-sha-256",
                "error": None,
            },
        ),
    )


def test_security_posture_accepts_loopback_scram_contract():
    validate_posture(_posture())


def test_security_posture_accepts_docker_wildcard_listener_with_loopback_publish():
    docker_rules = tuple(
        rule
        | (
            {"address": "0.0.0.0", "netmask": "0.0.0.0"}
            if rule.get("address") == "127.0.0.1"
            else {"address": "::", "netmask": "::"}
        )
        for rule in _posture().rules
        if rule.get("type") != "local"
    )
    validate_posture(
        replace(
            _posture(),
            listener="*",
            deployment="docker",
            rules=_posture().rules[:2] + docker_rules,
        )
    )


def test_security_posture_rejects_docker_non_loopback_publish():
    with pytest.raises(SecurityPostureError, match="HOST_BINDING_NOT_LOOPBACK_ONLY"):
        validate_posture(
            replace(
                _posture(),
                listener="*",
                deployment="docker",
                host_binding_loopback=False,
            )
        )


@pytest.mark.parametrize(
    ("posture", "reason"),
    (
        (replace(_posture(), listener="*"), "LISTENER_NOT_LOOPBACK_ONLY"),
        (
            replace(_posture(), password_encryption="md5"),
            "PASSWORD_ENCRYPTION_NOT_SCRAM",
        ),
        (
            replace(_posture(), hba_is_active=False),
            "HBA_CONFIGURATION_NOT_RELOADED",
        ),
        (
            replace(
                _posture(),
                rules=(_posture().rules[0] | {"auth_method": "trust"},)
                + _posture().rules[1:],
            ),
            "HBA_TRUST_PRESENT",
        ),
        (
            replace(
                _posture(),
                rules=_posture().rules
                + (
                    {
                        "type": "host",
                        "database": ["all"],
                        "user_name": ["all"],
                        "address": "0.0.0.0/0",
                        "netmask": "0.0.0.0",
                        "auth_method": "scram-sha-256",
                        "error": None,
                    },
                ),
            ),
            "HBA_NON_LOOPBACK_HOST",
        ),
        (
            replace(
                _posture(),
                rules=_posture().rules[:2]
                + (_posture().rules[2] | {"auth_method": "md5"},)
                + _posture().rules[3:],
            ),
            "HBA_HOST_METHOD_NOT_SCRAM",
        ),
        (
            replace(
                _posture(),
                rules=(_posture().rules[1], _posture().rules[0])
                + _posture().rules[2:],
            ),
            "HBA_ADMIN_PEER_SHADOWED",
        ),
        (
            replace(
                _posture(),
                rules=(_posture().rules[0] | {"error": "syntax error"},)
                + _posture().rules[1:],
            ),
            "HBA_PARSE_ERROR",
        ),
        (
            replace(
                _posture(),
                rules=(_posture().rules[0] | {"auth_method": "scram-sha-256"},)
                + _posture().rules[1:],
            ),
            "HBA_LOCAL_RULE_UNSAFE",
        ),
        (
            replace(
                _posture(),
                rules=_posture().rules[:2]
                + (_posture().rules[2] | {"type": "hostgssenc"},)
                + _posture().rules[3:],
            ),
            "HBA_RULE_TYPE_UNSUPPORTED",
        ),
        (
            replace(_posture(), rules=_posture().rules[1:]),
            "HBA_ADMIN_PEER_MISSING",
        ),
        (
            replace(
                _posture(),
                rules=(_posture().rules[0],) + _posture().rules[2:],
            ),
            "HBA_LOCAL_SCRAM_MISSING",
        ),
        (
            replace(_posture(), rules=_posture().rules[:3]),
            "HBA_LOOPBACK_SCRAM_MISSING",
        ),
    ),
)
def test_security_posture_rejects_unsafe_rules(posture: SecurityPosture, reason: str):
    with pytest.raises(SecurityPostureError, match=reason):
        validate_posture(posture)


def test_security_posture_rejects_non_object_rule(monkeypatch):
    monkeypatch.setattr(
        "dev_tools.postgresql_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                '{"listener":"localhost","password_encryption":"scram-sha-256",'
                '"rules":["invalid"]}'
            ),
        ),
    )

    with pytest.raises(SecurityPostureError, match="POSTURE_RESPONSE_INVALID"):
        _read_posture("Archlinux", deployment="wsl")


@pytest.mark.parametrize(
    ("output", "expected"),
    (("127.0.0.1:5432\n", True), ("[::1]:5432\n", True), ("0.0.0.0:5432\n", False)),
)
def test_docker_port_loopback_accepts_bracketed_ipv6(
    output: str, expected: bool
):
    assert _docker_port_is_loopback(output) is expected


def _compose_file(repository_root: Path) -> Path:
    compose_file = repository_root / "infrastructure" / "observability" / "compose.yaml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("name: test\n", encoding="utf-8")
    return compose_file


def _env_file_argument(arguments: list[str]) -> str:
    return arguments[arguments.index("--env-file") + 1]


def test_docker_compose_uses_canonical_root_env_not_nested_env(tmp_path, monkeypatch):
    repository_root = tmp_path / "repository"
    compose_file = _compose_file(repository_root)
    root_env = repository_root / ".env"
    root_env.write_text("ROOT=value\n", encoding="utf-8")
    (compose_file.parent / ".env").write_text("NESTED=value\n", encoding="utf-8")
    monkeypatch.setattr(postgresql_security, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(postgresql_security.shutil, "which", lambda _name: "docker.exe")

    arguments = _docker_compose_arguments(compose_file, "config")

    assert _env_file_argument(arguments) == str(root_env.resolve())


def test_docker_compose_ignores_unrelated_ancestor_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANCESTOR=value\n", encoding="utf-8")
    repository_root = tmp_path / "repository"
    compose_file = _compose_file(repository_root)
    root_env = repository_root / ".env"
    root_env.write_text("ROOT=value\n", encoding="utf-8")
    monkeypatch.setattr(postgresql_security, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(postgresql_security.shutil, "which", lambda _name: "docker.exe")

    arguments = _docker_compose_arguments(compose_file, "config")

    assert _env_file_argument(arguments) == str(root_env.resolve())


def test_docker_compose_requires_canonical_root_env(tmp_path, monkeypatch):
    repository_root = tmp_path / "repository"
    compose_file = _compose_file(repository_root)
    monkeypatch.setattr(postgresql_security, "REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(postgresql_security.shutil, "which", lambda _name: "docker.exe")

    with pytest.raises(SecurityPostureError, match="DOCKER_ENV_UNAVAILABLE"):
        _docker_compose_arguments(compose_file, "config")


def test_docker_compose_custom_path_requires_explicit_repository_root(tmp_path, monkeypatch):
    compose_file = tmp_path / "custom" / "compose.yaml"
    compose_file.parent.mkdir()
    compose_file.write_text("name: custom\n", encoding="utf-8")
    repository_root = tmp_path / "custom-environment"
    repository_root.mkdir()
    root_env = repository_root / ".env"
    root_env.write_text("ROOT=value\n", encoding="utf-8")
    monkeypatch.setattr(postgresql_security.shutil, "which", lambda _name: "docker.exe")

    arguments = _docker_compose_arguments(
        compose_file,
        "config",
        repository_root=repository_root,
    )

    assert _env_file_argument(arguments) == str(root_env.resolve())


def test_docker_posture_rejects_unavailable_service(tmp_path, monkeypatch):
    compose_file = tmp_path / "infrastructure" / "observability" / "compose.yaml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("name: test\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    monkeypatch.setattr(
        "dev_tools.postgresql_security.shutil.which", lambda _name: "docker.exe"
    )
    monkeypatch.setattr(
        "dev_tools.postgresql_security.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )

    with pytest.raises(SecurityPostureError, match="DOCKER_SERVICE_UNAVAILABLE"):
        _read_posture(
            deployment="docker",
            compose_file=compose_file,
            repository_root=tmp_path,
            service="postgres",
        )


def test_docker_posture_reads_loopback_compose_binding(tmp_path, monkeypatch):
    compose_file = tmp_path / "infrastructure" / "observability" / "compose.yaml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("name: test\n", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=value\n", encoding="utf-8")
    docker_rules = tuple(
        rule
        | (
            {"address": "0.0.0.0", "netmask": "0.0.0.0"}
            if rule.get("address") == "127.0.0.1"
            else {"address": "::", "netmask": "::"}
        )
        for rule in _posture().rules[2:]
    )
    payload = {
        "listener": "*",
        "password_encryption": "scram-sha-256",
        "hba_is_active": True,
        "rules": list(_posture().rules[:2] + docker_rules),
    }
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="127.0.0.1:5432\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(
        "dev_tools.postgresql_security.shutil.which", lambda _name: "docker.exe"
    )
    monkeypatch.setattr(
        "dev_tools.postgresql_security.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )

    posture = _read_posture(
        deployment="docker",
        compose_file=compose_file,
        repository_root=tmp_path,
        service="postgres",
    )

    assert posture.deployment == "docker"
    assert posture.host_binding_loopback is True
