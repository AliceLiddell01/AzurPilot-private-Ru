import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert

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
from module.persistence.schema import app_instance, metadata

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


def test_dorm_scan_round_trip_idempotency_latest_and_instance_isolation():
    lazy = LazyEngine(DatabaseSettings.from_environment())
    first_instance, second_instance = uuid4(), uuid4()
    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    try:
        with lazy.get().begin() as connection:
            for table in reversed(metadata.sorted_tables):
                connection.execute(delete(table))
            connection.execute(
                insert(app_instance),
                [
                    {"id": first_instance, "name": "dorm-pg-1", "created_at": now},
                    {"id": second_instance, "name": "dorm-pg-2", "created_at": now},
                ],
            )
            repository = PostgresDormMoraleRepository(connection)
            older = _scan(finished_at=now, key="scan:older")
            newer = _scan(finished_at=now + timedelta(seconds=1), key="scan:newer")
            assert repository.append_scan(first_instance, older) == older
            assert repository.append_scan(first_instance, older) == older
            semantic_retry = _scan(finished_at=now, key="scan:older")
            assert repository.append_scan(first_instance, semantic_retry).id == older.id
            repository.append_scan(first_instance, newer)
            other_instance_scan = _scan(finished_at=now, key="scan:older")
            repository.append_scan(second_instance, other_instance_scan)
            latest = repository.latest(first_instance)
            isolated = repository.latest(second_instance)
            assert latest.id == newer.id and latest.observations == newer.observations
            assert isolated.id == other_instance_scan.id
    finally:
        lazy.dispose()
