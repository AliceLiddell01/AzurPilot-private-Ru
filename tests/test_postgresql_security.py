from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from dev_tools.postgresql_security import (
    SecurityPosture,
    SecurityPostureError,
    _read_posture,
    validate_posture,
)


def _posture() -> SecurityPosture:
    return SecurityPosture(
        listener="localhost",
        password_encryption="scram-sha-256",
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


@pytest.mark.parametrize(
    ("posture", "reason"),
    (
        (replace(_posture(), listener="*"), "LISTENER_NOT_LOOPBACK_ONLY"),
        (
            replace(_posture(), password_encryption="md5"),
            "PASSWORD_ENCRYPTION_NOT_SCRAM",
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
                rules=(_posture().rules[2] | {"auth_method": "md5"},)
                + _posture().rules[1:2]
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
        _read_posture("Archlinux")
