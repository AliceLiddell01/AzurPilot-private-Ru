from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from module.application.database_diagnostics import (
    DATABASE_DIAGNOSTICS_SCHEMA_VERSION,
    DatabaseCheckResult,
    DatabaseCheckStatus,
    DatabaseStatusSnapshot,
)
from module.persistence.database_diagnostics import PostgresDatabaseDiagnostics
from module.persistence.schema import EXPECTED_ALEMBIC_HEAD

_TARGET_PROFILE = "fixture-target"


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalar_one(self) -> object:
        if len(self._values) != 1:
            raise AssertionError(f"ожидалось одно значение, получено: {self._values!r}")
        return self._values[0]

    def scalar_one_or_none(self) -> object | None:
        if len(self._values) > 1:
            raise AssertionError(f"ожидалось не более одного значения, получено: {self._values!r}")
        return self._values[0] if self._values else None

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[object]:
        return list(self._values)

    def __iter__(self):
        return iter(self._values)


class _HealthConnection:
    def __init__(
        self,
        *,
        schema_head: str = EXPECTED_ALEMBIC_HEAD,
        orphan: bool = False,
    ) -> None:
        self.schema_head = schema_head
        self.orphan = orphan

    def execute(self, statement: object, _parameters: object = None) -> _ScalarResult:
        sql = str(statement).casefold()
        if "show server_version_num" in sql:
            return _ScalarResult(["180000"])
        if "version_num from alembic_version" in sql:
            return _ScalarResult([self.schema_head])
        if "current_user" in sql:
            return _ScalarResult(["azurpilot_app"])
        if "limit 1" in sql:
            return _ScalarResult([1] if self.orphan else [])
        if "select 1" in sql:
            return _ScalarResult([1])
        raise AssertionError(f"Неожиданный SQL в фикстуре: {sql}")


class _EngineContext:
    def __init__(self, connection: _HealthConnection) -> None:
        self.connection = connection

    def connect(self) -> _EngineContext:
        return self

    def __enter__(self) -> _HealthConnection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        return None


class _DiagnosticEngine:
    def __init__(self, connection: _HealthConnection) -> None:
        self.context = _EngineContext(connection)

    def get(self) -> _EngineContext:
        return self.context


class _ErrorEngine:
    def get(self) -> object:
        raise RuntimeError("password=secret не должен возвращаться")


def test_database_diagnostics_exposes_fixed_read_only_catalog_without_engine() -> None:
    diagnostics = PostgresDatabaseDiagnostics(
        None,
        marker_ready=False,
        marker_head=None,
        schema_marker_version=None,
        config_match=False,
    )

    checks = diagnostics.list_checks()
    assert [item.check_id for item in checks] == [
        "backend_marker",
        "connectivity",
        "app_role",
        "schema_head",
        "schema_marker",
        "target_resolution",
        "required_tables",
        "domain_consistency",
        "transaction",
        "config_match",
    ]
    assert all(item.read_only is True for item in checks)

    connectivity = diagnostics.run_check("connectivity", _TARGET_PROFILE)
    assert connectivity.status is DatabaseCheckStatus.UNAVAILABLE
    assert connectivity.code == "DEV_DATABASE_CONNECTION_UNAVAILABLE"
    assert diagnostics.run_check("backend_marker", _TARGET_PROFILE).status is DatabaseCheckStatus.FAIL

    status = diagnostics.get_status(_TARGET_PROFILE)
    assert status.schema_version == DATABASE_DIAGNOSTICS_SCHEMA_VERSION
    assert status.expected_schema_head == EXPECTED_ALEMBIC_HEAD
    assert status.connectivity is False
    assert status.domain_consistency is None
    assert all(item.observed is None or isinstance(item.observed, (str, int, bool)) for item in status.checks)

    with pytest.raises(ValueError):
        diagnostics.run_check("arbitrary_sql", _TARGET_PROFILE)


def test_database_diagnostics_contracts_allow_only_sanitized_scalars() -> None:
    result = DatabaseCheckResult(
        "connectivity",
        DatabaseCheckStatus.PASS,
        "DEV_DATABASE_CONNECTED",
        "Подключение подтверждено",
        True,
    )
    assert result.as_dict() == {
        "check_id": "connectivity",
        "status": "pass",
        "code": "DEV_DATABASE_CONNECTED",
        "message": "Подключение подтверждено",
        "observed": True,
    }

    with pytest.raises(TypeError):
        DatabaseCheckResult(
            "connectivity",
            DatabaseCheckStatus.PASS,
            "DEV_DATABASE_CONNECTED",
            "Подключение подтверждено",
            {"password": "must not be accepted"},  # type: ignore[arg-type]
        )

    snapshot = DatabaseStatusSnapshot(
        target_profile=_TARGET_PROFILE,
        marker_ready=True,
        connectivity=True,
        app_role_ready=True,
        expected_schema_head=EXPECTED_ALEMBIC_HEAD,
        current_schema_head=EXPECTED_ALEMBIC_HEAD,
        schema_marker_version=1,
        target_resolved=True,
        required_tables_ready=True,
        domain_consistency=True,
        transaction_ready=True,
        config_match=True,
        checks=(result,),
    )
    assert snapshot.as_dict()["checks"] == [result.as_dict()]


def test_database_diagnostics_maps_healthy_and_schema_drift_states() -> None:
    healthy_connection = _HealthConnection()
    healthy = PostgresDatabaseDiagnostics._connectivity(healthy_connection)  # type: ignore[arg-type]
    assert healthy.status is DatabaseCheckStatus.PASS
    assert healthy.code == "DEV_DATABASE_CONNECTED"

    drift_connection = _HealthConnection(schema_head="0007_dorm_morale_reconciliation")
    drift = PostgresDatabaseDiagnostics._schema_head(drift_connection)  # type: ignore[arg-type]
    assert drift.status is DatabaseCheckStatus.FAIL
    assert drift.code == "DEV_DATABASE_SCHEMA_HEAD_MISMATCH"
    assert drift.observed == "0007_dorm_morale_reconciliation"

    diagnostics = PostgresDatabaseDiagnostics(
        _DiagnosticEngine(healthy_connection),  # type: ignore[arg-type]
        marker_ready=True,
        marker_head=EXPECTED_ALEMBIC_HEAD,
        schema_marker_version=1,
        config_match=True,
    )
    assert diagnostics.run_check("app_role", _TARGET_PROFILE).status is DatabaseCheckStatus.PASS

    orphan = PostgresDatabaseDiagnostics._domain_consistency(  # type: ignore[arg-type]
        _HealthConnection(orphan=True),
    )
    assert orphan.status is DatabaseCheckStatus.FAIL
    assert orphan.code == "DEV_DATABASE_DOMAIN_INCONSISTENT"


def test_database_diagnostics_sanitizes_connection_failures() -> None:
    diagnostics = PostgresDatabaseDiagnostics(
        _ErrorEngine(),  # type: ignore[arg-type]
        marker_ready=True,
        marker_head=EXPECTED_ALEMBIC_HEAD,
        schema_marker_version=1,
        config_match=True,
    )

    result = diagnostics.run_check("connectivity", _TARGET_PROFILE)

    assert result.status is DatabaseCheckStatus.UNAVAILABLE
    assert result.code == "DEV_DATABASE_CHECK_UNAVAILABLE"
    assert "secret" not in result.message


def test_database_diagnostics_import_does_not_open_connection() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import module.persistence.database_diagnostics as diagnostics; "
                "import module.persistence.runtime as runtime; "
                "assert diagnostics.PostgresDatabaseDiagnostics is not None; "
                "assert runtime.runtime_engine() is None; "
                "print('import-ok')"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "import-ok"
    assert "Traceback" not in completed.stderr
