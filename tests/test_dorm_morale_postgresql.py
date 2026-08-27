import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select

from module.application.errors import StorageConflictError
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormFloorSnapshot,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.persistence import DatabaseSettings, LazyEngine
from module.persistence.dorm_morale_repositories import PostgresDormMoraleRepository
from module.persistence.schema import app_instance, dorm_morale_scan_run, metadata

pytestmark = pytest.mark.skipif(
    any(
        not os.environ.get(name)
        for name in (
            "AZURPILOT_POSTGRES_HOST",
            "AZURPILOT_POSTGRES_DATABASE",
            "AZURPILOT_POSTGRES_USER",
        )
    )
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL DB",
)


def _scan(*, finished_at, key):
    observation = DormMoraleObservation(
        DormFloor.FLOOR_1,
        1,
        "Arizona",
        "Arizona",
        IdentityStatus.MATCHED,
        Decimal(150),
        Decimal(40),
        CanonicalShipIdentity("azur_lane_ship_group:10504"),
        "Arizona",
    )
    return DormMoraleScanResult(
        uuid4(),
        finished_at - timedelta(seconds=2),
        finished_at,
        (
            DormFloorScanAttempt(
                DormFloor.FLOOR_1,
                DormFloorScanStatus.SUCCEEDED,
                finished_at - timedelta(seconds=1),
                DormFloorSnapshot(DormFloor.FLOOR_1, (observation,), "a" * 64),
            ),
            DormFloorScanAttempt(
                DormFloor.FLOOR_2,
                DormFloorScanStatus.SUCCEEDED,
                finished_at,
                DormFloorSnapshot(DormFloor.FLOOR_2, (), "a" * 64),
            ),
        ),
        "test:postgresql",
        key,
    )


def _require_disposable_target(settings):
    if os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1":
        raise RuntimeError("Очистка PostgreSQL требует явного подтверждения одноразовой БД.")
    expected = {
        "host": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_HOST"),
        "port": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_PORT"),
        "database": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_DATABASE"),
        "user": os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE_USER"),
    }
    actual = {
        "host": settings.host,
        "port": str(settings.port),
        "database": settings.database,
        "user": settings.user,
    }
    if any(not value for value in expected.values()) or expected != actual:
        raise RuntimeError(
            "Очистка PostgreSQL разрешена только для точно подтверждённой "
            "одноразовой тестовой БД."
        )


def _prepare_database(lazy, settings, instances, now):
    _require_disposable_target(settings)
    with lazy.get().begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
        connection.execute(
            insert(app_instance),
            [
                {"id": instance_id, "name": name, "created_at": now}
                for instance_id, name in instances
            ],
        )


def _append_concurrently(lazy, instance_id, scans):
    barrier = Barrier(len(scans))

    def append(scan):
        with lazy.get().begin() as connection:
            repository = PostgresDormMoraleRepository(connection)
            barrier.wait(timeout=10)
            return repository.append_scan(instance_id, scan)

    with ThreadPoolExecutor(max_workers=len(scans)) as executor:
        futures = tuple(executor.submit(append, scan) for scan in scans)
        return tuple(future.result(timeout=30) for future in futures)


def test_prepare_database_rejects_mismatched_disposable_target(monkeypatch):
    settings = DatabaseSettings.from_environment()
    monkeypatch.setenv(
        "AZURPILOT_POSTGRES_DISPOSABLE_DATABASE",
        f"{settings.database}_unexpected",
    )
    with pytest.raises(RuntimeError, match="точно подтверждённой"):
        _prepare_database(
            object(),
            settings,
            (),
            datetime(2026, 8, 27, 10, tzinfo=UTC),
        )


def test_dorm_scan_round_trip_idempotency_latest_and_instance_isolation():
    settings = DatabaseSettings.from_environment()
    lazy = LazyEngine(settings)
    first_instance, second_instance = uuid4(), uuid4()
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    try:
        _prepare_database(
            lazy,
            settings,
            (
                (first_instance, "dorm-pg-1"),
                (second_instance, "dorm-pg-2"),
            ),
            now,
        )
        with lazy.get().begin() as connection:
            repository = PostgresDormMoraleRepository(connection)
            older = _scan(finished_at=now, key="scan:older")
            newer = _scan(finished_at=now + timedelta(seconds=1), key="scan:newer")
            assert repository.append_scan(first_instance, older) == older
            assert repository.append_scan(first_instance, older) == older

            semantic_retry = _scan(finished_at=now, key="scan:older")
            semantic_result = repository.append_scan(first_instance, semantic_retry)
            assert semantic_result == older
            assert semantic_result.idempotency_key == older.idempotency_key

            conflicting_retry = _scan(
                finished_at=now + timedelta(seconds=2),
                key="scan:older",
            )
            with pytest.raises(StorageConflictError):
                repository.append_scan(first_instance, conflicting_retry)

            repository.append_scan(first_instance, newer)
            other_instance_scan = _scan(finished_at=now, key="scan:older")
            assert (
                repository.append_scan(second_instance, other_instance_scan)
                == other_instance_scan
            )

            latest = repository.latest(first_instance)
            isolated = repository.latest(second_instance)
            assert latest.id == newer.id and latest.observations == newer.observations
            assert latest.idempotency_key == newer.idempotency_key
            assert isolated.id == other_instance_scan.id
            assert isolated.idempotency_key == other_instance_scan.idempotency_key
    finally:
        lazy.dispose()


def test_concurrent_semantic_retry_replays_single_scan():
    settings = DatabaseSettings.from_environment()
    lazy = LazyEngine(settings)
    instance_id = uuid4()
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    scans = (
        _scan(finished_at=now, key="scan:concurrent"),
        _scan(finished_at=now, key="scan:concurrent"),
    )
    try:
        _prepare_database(
            lazy,
            settings,
            ((instance_id, "dorm-pg-concurrent"),),
            now,
        )
        results = _append_concurrently(lazy, instance_id, scans)

        assert results[0] == results[1]
        assert results[0].id in {scan.id for scan in scans}
        assert results[0].idempotency_key == "scan:concurrent"

        with lazy.get().begin() as connection:
            stored_count = connection.execute(
                select(func.count())
                .select_from(dorm_morale_scan_run)
                .where(
                    dorm_morale_scan_run.c.instance_id == instance_id,
                    dorm_morale_scan_run.c.idempotency_key == "scan:concurrent",
                )
            ).scalar_one()
            latest = PostgresDormMoraleRepository(connection).latest(instance_id)

        assert stored_count == 1
        assert latest == results[0]
    finally:
        lazy.dispose()
