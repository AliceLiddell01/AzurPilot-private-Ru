from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
_PREVIOUS_HEAD = "0004_fleet_manual_scan_command"
_REQUIRED_ENV = (
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_PORT",
    "CI_POSTGRES_ADMIN_USER",
    "CI_POSTGRES_ADMIN_PASSWORD",
    "CI_POSTGRES_OWNER_USER",
    "CI_POSTGRES_MIGRATOR_USER",
    "CI_POSTGRES_MIGRATOR_PASSWORD",
)
pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in _REQUIRED_ENV)
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL CI DB",
)


def _admin_connection(database: str = "postgres"):
    return psycopg.connect(
        host=os.environ["AZURPILOT_POSTGRES_HOST"],
        port=os.environ["AZURPILOT_POSTGRES_PORT"],
        dbname=database,
        user=os.environ["CI_POSTGRES_ADMIN_USER"],
        password=os.environ["CI_POSTGRES_ADMIN_PASSWORD"],
        autocommit=True,
    )


@pytest.fixture
def migration_database():
    database = f"azurpilot_form_{uuid4().hex[:12]}"
    with _admin_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database),
                sql.Identifier(os.environ["CI_POSTGRES_OWNER_USER"]),
            )
        )
    try:
        yield database
    finally:
        with _admin_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def _alembic_environment(database: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AZURPILOT_POSTGRES_DATABASE": database,
            "AZURPILOT_POSTGRES_USER": os.environ["CI_POSTGRES_MIGRATOR_USER"],
            "AZURPILOT_POSTGRES_PASSWORD": os.environ[
                "CI_POSTGRES_MIGRATOR_PASSWORD"
            ],
            "AZURPILOT_POSTGRES_SSLMODE": "disable",
            "PGOPTIONS": f"-c role={os.environ['CI_POSTGRES_OWNER_USER']}",
        }
    )
    return environment


def _upgrade(database: str, revision: str, *, check: bool = True):
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=_alembic_environment(database),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=check,
    )


def _seed_matched_slots(
    database: str,
    rows: tuple[tuple[str, str, str], ...],
) -> None:
    now = datetime.now(UTC)
    instance_id = uuid4()
    run_id = uuid4()
    snapshot_id = uuid4()
    with _admin_connection(database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO azurpilot.app_instance (id, name, active, created_at) "
            "VALUES (%s, %s, true, %s)",
            (instance_id, f"migration-{uuid4().hex}", now),
        )
        cursor.execute(
            "INSERT INTO azurpilot.formation_surface_fleet_scan_run "
            "(id, instance_id, source, started_at, finished_at, status, error_code) "
            "VALUES (%s, %s, 'migration-regression', %s, %s, 'succeeded', NULL)",
            (run_id, instance_id, now, now),
        )
        cursor.execute(
            "INSERT INTO azurpilot.formation_surface_fleet_scan_request "
            "(run_id, fleet_index) VALUES (%s, 1)",
            (run_id,),
        )
        cursor.execute(
            "INSERT INTO azurpilot.formation_surface_fleet_snapshot "
            "(id, run_id, instance_id, idempotency_key, payload_digest, fleet_index, "
            "observed_at, complete, catalog_fingerprint) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, true, %s)",
            (
                snapshot_id,
                run_id,
                instance_id,
                f"migration-{uuid4().hex}",
                "a" * 64,
                now,
                "b" * 64,
            ),
        )
        for position, (raw_name, displayed_name, canonical_name) in enumerate(
            rows, start=1
        ):
            cursor.execute(
                "INSERT INTO azurpilot.formation_surface_fleet_slot "
                "(snapshot_id, side, position, occupied, identity_status, raw_name_ocr, "
                "displayed_name, canonical_identity_key, canonical_name) "
                "VALUES (%s, 'main', %s, true, 'matched', %s, %s, %s, %s)",
                (
                    snapshot_id,
                    position,
                    raw_name,
                    displayed_name,
                    f"azur_lane_ship_group:{900000 + position}",
                    canonical_name,
                ),
            )
        connection.commit()


def _stored_forms(database: str) -> tuple[str, ...]:
    with _admin_connection(database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT ship_form FROM azurpilot.formation_surface_fleet_slot "
            "ORDER BY position"
        )
        return tuple(row[0] for row in cursor.fetchall())


def test_0005_backfills_exact_and_retrofit_forms(migration_database):
    _upgrade(migration_database, _PREVIOUS_HEAD)
    _seed_matched_slots(
        migration_database,
        (
            ("Generic Base Ship", "Generic Base Ship", "Generic Base Ship"),
            (
                "Generic Retrofit Ship (Retrofit)",
                "Generic Retrofit Ship (Retrofit)",
                "Generic Retrofit Ship",
            ),
            (
                "Generic Partial Ship (Retro1",
                "Generic Partial Ship (Retro1",
                "Generic Partial Ship",
            ),
        ),
    )

    _upgrade(migration_database, "head")

    assert _stored_forms(migration_database) == ("base", "retrofit", "retrofit")


def test_0005_replays_legacy_fuzzy_truncated_and_explicit_retrofit_paths(
    migration_database,
):
    _upgrade(migration_database, _PREVIOUS_HEAD)
    _seed_matched_slots(
        migration_database,
        (
            ("Gener1c Base Ship", "Gener1c Base Ship", "Generic Base Ship"),
            ("Generic Trunca...", "Generic Trunca...", "Generic Truncated Ship"),
            (
                "Generic Retrofit Ship (R...",
                "Generic Retrofit Ship (R...",
                "Generic Retrofit Ship",
            ),
        ),
    )

    _upgrade(migration_database, "head")

    assert _stored_forms(migration_database) == ("base", "base", "retrofit")


def test_0005_fails_closed_for_structurally_invalid_historical_matched_row(
    migration_database,
):
    _upgrade(migration_database, _PREVIOUS_HEAD)
    _seed_matched_slots(
        migration_database,
        (("Generic Base Ship", "Generic Base Ship", "Generic Base Ship"),),
    )
    with _admin_connection(migration_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE azurpilot.formation_surface_fleet_slot "
            "SET canonical_identity_key = 'unexpected:900001'"
        )

    result = _upgrade(migration_database, "head", check=False)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "структурно некорректных исторических MATCHED-слотов" in output
    assert "форма для некорректной записи не назначается" in output
    with _admin_connection(migration_database) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == _PREVIOUS_HEAD
        cursor.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'azurpilot' "
            "AND table_name = 'formation_surface_fleet_slot' "
            "AND column_name = 'ship_form'"
            ")"
        )
        assert cursor.fetchone()[0] is False
