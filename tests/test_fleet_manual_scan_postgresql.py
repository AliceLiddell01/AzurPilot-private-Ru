from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageInvalidDataError
from module.application.fleet_manual_scan import (
    FleetManualScanCommandService,
    FleetManualScanStatus,
)
from module.application.fleet_state import FleetScanService
from module.application.instance_identity import resolve_runtime_instance
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.persistence import DatabaseSettings, LazyEngine, PostgresUnitOfWork
from module.persistence.schema import (
    formation_surface_fleet_scan_command,
    formation_surface_fleet_scan_command_fleet,
    metadata,
)

REQUIRED_ENV = (
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_DATABASE",
    "AZURPILOT_POSTGRES_USER",
)
pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in REQUIRED_ENV)
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL DB",
)


@pytest.fixture
def database():
    lazy = LazyEngine(DatabaseSettings.from_environment())
    with lazy.get().begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
    yield lazy
    lazy.dispose()


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 25, tzinfo=UTC)

    def __call__(self):
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


def _snapshot(fleet_index: int) -> FormationFleetSnapshot:
    coordinates = (
        (FormationFleetSide.MAIN, 1),
        (FormationFleetSide.MAIN, 2),
        (FormationFleetSide.MAIN, 3),
        (FormationFleetSide.VANGUARD, 1),
        (FormationFleetSide.VANGUARD, 2),
        (FormationFleetSide.VANGUARD, 3),
    )
    slots = []
    for side, position in coordinates:
        if position == 1:
            slots.append(
                FormationFleetSlotObservation(
                    side=side,
                    position=position,
                    occupied=True,
                    identity_status=IdentityStatus.MATCHED,
                    raw_name_ocr="Enterprise",
                    displayed_name="Enterprise",
                    canonical_identity=CanonicalShipIdentity(
                        f"azur_lane_ship_group:{fleet_index}-{side.value}"
                    ),
                    canonical_name="Enterprise",
                )
            )
        else:
            slots.append(
                FormationFleetSlotObservation(
                    side=side,
                    position=position,
                    occupied=False,
                )
            )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=tuple(slots),
        catalog_fingerprint="d" * 64,
    )


class _Controller:
    def __init__(self, fail_at=None) -> None:
        self.fail_at = fail_at

    def scan_surface_fleet(self, fleet_index):
        if fleet_index == self.fail_at:
            raise RuntimeError("unsafe Formation state")
        return _snapshot(fleet_index)


def _command_service(database, clock=None):
    return FleetManualScanCommandService(
        lambda: PostgresUnitOfWork(database),
        clock=clock or _Clock(),
    )


def _scan(database, selection, *, fail_at=None):
    scanner = FleetScanService(
        lambda: PostgresUnitOfWork(database),
        _Controller(fail_at),
        clock=_Clock(),
    )
    return scanner.scan("profile-a", selection, source="manual:webui")


def test_submit_selection_round_trip_instance_isolation_and_idempotency(database):
    service = _command_service(database)
    first = service.submit("profile-a", FleetSelection.several(6, 2, 6))
    duplicate = service.submit("profile-a", FleetSelection.one(1))
    other = service.submit("profile-b", FleetSelection.one(4))

    assert first.created is True
    assert first.command.selection.fleet_indices == (2, 6)
    assert duplicate.created is False
    assert duplicate.command.id == first.command.id
    assert duplicate.command.selection.fleet_indices == (2, 6)
    assert other.created is True
    assert other.command.instance_id != first.command.instance_id
    assert service.latest("profile-a").id == first.command.id
    assert service.latest("profile-b").id == other.command.id

    with database.get().connect() as connection:
        rows = connection.execute(
            select(formation_surface_fleet_scan_command_fleet).where(
                formation_surface_fleet_scan_command_fleet.c.command_id
                == first.command.id
            )
        ).all()
    assert tuple(row.fleet_index for row in rows) == (2, 6)


def test_atomic_claim_two_claimers_and_reconstruction(database):
    service = _command_service(database)
    submitted = service.submit("profile-a", FleetSelection.several(1, 2))

    def claim():
        return _command_service(database).claim_next("profile-a")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(executor.map(lambda _index: claim(), range(2)))

    claimed = tuple(item for item in claims if item is not None)
    assert len(claimed) == 1
    assert claimed[0].id == submitted.command.id
    assert claimed[0].status is FleetManualScanStatus.RUNNING
    assert _command_service(database).pending_exists("profile-a") is False


def test_terminal_transitions_result_fk_and_deterministic_latest(database):
    service = _command_service(database)
    succeeded = service.submit("profile-a", FleetSelection.one(1)).command
    assert service.claim_next("profile-a").id == succeeded.id
    success_batch = _scan(database, FleetSelection.one(1))
    success = service.finish(
        "profile-a",
        succeeded.id,
        status=FleetManualScanStatus.SUCCEEDED,
        result_run_id=success_batch.run_id,
        error_code=None,
    )
    assert success.status is FleetManualScanStatus.SUCCEEDED

    partial = service.submit("profile-a", FleetSelection.several(1, 2)).command
    service.claim_next("profile-a")
    partial_batch = _scan(database, FleetSelection.several(1, 2), fail_at=2)
    partial_done = service.finish(
        "profile-a",
        partial.id,
        status=FleetManualScanStatus.PARTIAL,
        result_run_id=partial_batch.run_id,
        error_code=partial_batch.failure_code,
    )
    assert partial_done.status is FleetManualScanStatus.PARTIAL

    failed = service.submit("profile-a", FleetSelection.one(2)).command
    service.claim_next("profile-a")
    failed_batch = _scan(database, FleetSelection.one(2), fail_at=2)
    failed_done = service.finish(
        "profile-a",
        failed.id,
        status=FleetManualScanStatus.FAILED,
        result_run_id=failed_batch.run_id,
        error_code=failed_batch.failure_code,
    )
    assert failed_done.status is FleetManualScanStatus.FAILED
    assert service.latest("profile-a").id == failed.id

    another = service.submit("profile-a", FleetSelection.one(3)).command
    service.claim_next("profile-a")
    with pytest.raises(StorageInvalidDataError):
        service.finish(
            "profile-a",
            another.id,
            status=FleetManualScanStatus.SUCCEEDED,
            result_run_id=uuid4(),
            error_code=None,
        )


def test_interrupted_running_command_is_failed_and_new_submit_is_allowed(database):
    service = _command_service(database)
    command = service.submit("profile-a", FleetSelection.one(5)).command
    service.claim_next("profile-a")

    assert service.recover_interrupted("profile-a") == 1
    recovered = service.latest("profile-a")
    assert recovered.id == command.id
    assert recovered.status is FleetManualScanStatus.FAILED
    assert recovered.error_code == "worker_interrupted"
    assert service.submit("profile-a", FleetSelection.one(6)).created is True


def test_invalid_lifecycle_constraint_and_rollback_safety(database):
    service = _command_service(database)
    command = service.submit("profile-a", FleetSelection.one(1)).command
    with pytest.raises(SQLAlchemyError), database.get().begin() as connection:
        connection.execute(
            update(formation_surface_fleet_scan_command)
            .where(formation_surface_fleet_scan_command.c.id == command.id)
            .values(status="succeeded")
        )

    with PostgresUnitOfWork(database) as uow:
        instance_id = resolve_runtime_instance(uow, "profile-rollback")
        uow.fleet_scan_commands.create_pending(
            instance_id,
            uuid4(),
            FleetSelection.one(2),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )
        # Намеренно не выполняем commit: __exit__ должен откатить identity и команду.

    assert _command_service(database).latest("profile-rollback") is None
