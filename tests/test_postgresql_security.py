from __future__ import annotations

from dataclasses import replace

import pytest

from dev_tools.postgresql_security import (
    SecurityPosture,
    SecurityPostureError,
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
    ),
)
def test_security_posture_rejects_unsafe_rules(posture: SecurityPosture, reason: str):
    with pytest.raises(SecurityPostureError, match=reason):
        validate_posture(posture)
