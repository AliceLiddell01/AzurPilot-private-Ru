"""Типизированные transport-neutral контракты диагностики PostgreSQL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

DATABASE_DIAGNOSTICS_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")


class DatabaseCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


def _identifier(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} имеет недопустимый формат")
    return value


def _text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
        raise ValueError(f"{field} имеет недопустимый формат")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseCheckDescriptor:
    check_id: str
    description: str
    target_scoped: bool = True
    read_only: bool = True

    def __post_init__(self) -> None:
        _identifier(self.check_id, field="check_id")
        _text(self.description, field="description")
        if type(self.target_scoped) is not bool:
            raise TypeError("target_scoped должен быть bool")
        if self.read_only is not True:
            raise ValueError("Диагностика базы данных должна быть read-only")

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "description": self.description,
            "target_scoped": self.target_scoped,
            "read_only": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class DatabaseCheckResult:
    check_id: str
    status: DatabaseCheckStatus
    code: str
    message: str
    observed: str | int | bool | None = None

    def __post_init__(self) -> None:
        _identifier(self.check_id, field="check_id")
        if not isinstance(self.status, DatabaseCheckStatus):
            raise TypeError("status должен быть DatabaseCheckStatus")
        _identifier(self.code, field="code")
        _text(self.message, field="message")
        if self.observed is not None:
            if type(self.observed) not in {str, int, bool}:
                raise TypeError("observed должен быть scalar")
            if isinstance(self.observed, str):
                _text(self.observed, field="observed")

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class DatabaseStatusSnapshot:
    target_profile: str
    marker_ready: bool
    connectivity: bool
    app_role_ready: bool
    expected_schema_head: str
    current_schema_head: str | None
    schema_marker_version: int | None
    target_resolved: bool
    required_tables_ready: bool
    domain_consistency: bool | None
    transaction_ready: bool
    config_match: bool
    checks: tuple[DatabaseCheckResult, ...]
    schema_version: int = DATABASE_DIAGNOSTICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.target_profile, field="target_profile")
        _identifier(self.expected_schema_head, field="expected_schema_head")
        if self.current_schema_head is not None:
            _identifier(self.current_schema_head, field="current_schema_head")
        if self.schema_marker_version is not None and type(self.schema_marker_version) is not int:
            raise TypeError("schema_marker_version должен быть int или None")
        for name in (
            "marker_ready",
            "connectivity",
            "app_role_ready",
            "target_resolved",
            "required_tables_ready",
            "transaction_ready",
            "config_match",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} должен быть bool")
        if self.domain_consistency is not None and type(self.domain_consistency) is not bool:
            raise TypeError("domain_consistency должен быть bool или None")
        if type(self.schema_version) is not int or self.schema_version != DATABASE_DIAGNOSTICS_SCHEMA_VERSION:
            raise ValueError("Неподдерживаемая версия диагностики базы данных")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(item, DatabaseCheckResult) for item in self.checks
        ):
            raise TypeError("checks должен быть tuple DatabaseCheckResult")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_profile": self.target_profile,
            "marker_ready": self.marker_ready,
            "connectivity": self.connectivity,
            "app_role_ready": self.app_role_ready,
            "expected_schema_head": self.expected_schema_head,
            "current_schema_head": self.current_schema_head,
            "schema_marker_version": self.schema_marker_version,
            "target_resolved": self.target_resolved,
            "required_tables_ready": self.required_tables_ready,
            "domain_consistency": self.domain_consistency,
            "transaction_ready": self.transaction_ready,
            "config_match": self.config_match,
            "checks": [item.as_dict() for item in self.checks],
        }


class DatabaseDiagnosticsReader(Protocol):
    def list_checks(self) -> tuple[DatabaseCheckDescriptor, ...]: ...

    def run_check(self, check_id: str, target_profile: str) -> DatabaseCheckResult: ...

    def get_status(self, target_profile: str) -> DatabaseStatusSnapshot: ...


__all__ = [
    "DATABASE_DIAGNOSTICS_SCHEMA_VERSION",
    "DatabaseCheckDescriptor",
    "DatabaseCheckResult",
    "DatabaseCheckStatus",
    "DatabaseDiagnosticsReader",
    "DatabaseStatusSnapshot",
]
