from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageConflictError
from module.application.fleet_state import (
    FleetRefreshPolicy,
    FleetScanRunStatus,
    FleetScanService,
    FleetStateRequest,
    FleetStateService,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.persistence import DatabaseSettings, LazyEngine, PostgresUnitOfWork
from module.persistence.schema import (
    formation_surface_fleet_scan_request,
    formation_surface_fleet_scan_run,
    formation_surface_fleet_slot,
    formation_surface_fleet_snapshot,
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


def _slot(
    side: FormationFleetSide,
    position: int,
    status: IdentityStatus | None,
) -> FormationFleetSlotObservation:
    if status is None:
        return FormationFleetSlotObservation(side=side, position=position, occupied=False)
    matched = status is IdentityStatus.MATCHED
    return FormationFleetSlotObservation(
        side=side,
        position=position,
        occupied=True,
        identity_status=status,
        raw_name_ocr=f"raw-{side.value}-{position}",
        displayed_name=f"display-{side.value}-{position}",
        canonical_identity=(
            CanonicalShipIdentity(f"azur_lane_ship_group:{position}")
            if matched
            else None
        ),
        canonical_name=f"Ship {position}" if matched else None,
    )


def _snapshot(fleet_index: int, *, complete: bool = True) -> FormationFleetSnapshot:
    statuses = (
        IdentityStatus.MATCHED,
        IdentityStatus.MATCHED if complete else IdentityStatus.UNRESOLVED,
        None,
        IdentityStatus.MATCHED if complete else IdentityStatus.AMBIGUOUS,
        None,
        None,
    )
    coordinates = (
        (FormationFleetSide.MAIN, 1),
        (FormationFleetSide.MAIN, 2),
        (FormationFleetSide.MAIN, 3),
        (FormationFleetSide.VANGUARD, 1),
        (FormationFleetSide.VANGUARD, 2),
        (FormationFleetSide.VANGUARD, 3),
    )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=tuple(
            _slot(side, position, status)
            for (side, position), status in zip(coordinates, statuses, strict=True)
        ),
        catalog_fingerprint="b" * 64,
    )


class _Controller:
    def __init__(self):
        self.calls = []
        self.incomplete = set()

    def scan_surface_fleet(self, fleet_index):
        self.calls.append(fleet_index)
        return _snapshot(fleet_index, complete=fleet_index not in self.incomplete)


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        current = self.value
        self.value += timedelta(microseconds=1)
        return current


def _services(database, *, instance="profile", clock=None, controller=None):
    del instance
    controller = controller or _Controller()
    clock = clock or _Clock(datetime(2026, 8, 25, tzinfo=UTC))
    factory = lambda: PostgresUnitOfWork(database)
    scanner = FleetScanService(factory, controller, clock=clock)
    state = FleetStateService(factory, scanner, clock=clock)
    return controller, clock, scanner, state


def test_round_trip_full_incomplete_all_slots_and_identity_states(database):
    controller = _Controller()
    controller.incomplete.add(2)
    _, _, scanner, state = _services(database, controller=controller)

    result = scanner.scan(
        "profile-a",
        FleetSelection.several(1, 2),
        source="consumer:integration",
    )
    latest = state.state(
        "profile-a",
        FleetStateRequest(
            FleetSelection.several(1, 2),
            FleetRefreshPolicy.NEVER,
        ),
        source="consumer:integration",
    )

    assert result.status is FleetScanRunStatus.SUCCEEDED
    assert tuple(item.snapshot.complete for item in latest.observations) == (True, False)
    assert len(latest.observations[1].snapshot.slots) == 6
    assert latest.observations[1].snapshot.slots[1].identity_status is IdentityStatus.UNRESOLVED
    assert latest.observations[1].snapshot.slots[3].identity_status is IdentityStatus.AMBIGUOUS
    matched = latest.observations[1].snapshot.slots[0]
    assert matched.canonical_identity.key == "azur_lane_ship_group:1"
    assert matched.canonical_name == "Ship 1"
    assert latest.observations[1].snapshot.slots[2].occupied is False

    with database.get().connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(formation_surface_fleet_slot)
        ).scalar_one() == 12


def test_identical_composition_remains_two_observations_and_history_is_bounded(database):
    _, _, scanner, state = _services(database)
    scanner.scan("profile-a", FleetSelection.one(1), source="consumer:first")
    scanner.scan("profile-a", FleetSelection.one(1), source="consumer:second")

    history = state.history("profile-a", 1, limit=10)

    assert len(history) == 2
    assert history[0].observed_at > history[1].observed_at
    assert history[0].snapshot == history[1].snapshot
    assert len(state.history("profile-a", 1, limit=1)) == 1


def test_same_operation_is_idempotent_but_changed_payload_conflicts(database):
    _, _, scanner, state = _services(database)
    result = scanner.scan("profile", FleetSelection.one(1), source="consumer:first")
    observation = result.observations[0]

    with PostgresUnitOfWork(database) as uow:
        assert uow.fleet_state.append_observation(observation) is False
        uow.commit()

    changed = replace(observation, snapshot=_snapshot(1, complete=False))
    with pytest.raises(StorageConflictError), PostgresUnitOfWork(database) as uow:
        uow.fleet_state.append_observation(changed)

    assert len(state.history("profile", 1, limit=10)) == 1


def test_latest_isolated_by_instance_and_uses_stable_id_tiebreaker(database):
    same_time = datetime(2026, 8, 25, 5, tzinfo=UTC)

    class ConstantClock:
        def __call__(self):
            return same_time

    ids = iter(
        (
            UUID("00000000-0000-0000-0000-000000000010"),
            UUID("00000000-0000-0000-0000-000000000011"),
            UUID("00000000-0000-0000-0000-000000000020"),
            UUID("00000000-0000-0000-0000-000000000021"),
            UUID("00000000-0000-0000-0000-000000000030"),
            UUID("00000000-0000-0000-0000-000000000031"),
        )
    )
    controller = _Controller()
    factory = lambda: PostgresUnitOfWork(database)
    scanner = FleetScanService(
        factory,
        controller,
        clock=ConstantClock(),
        id_factory=lambda: next(ids),
    )
    state = FleetStateService(factory, scanner, clock=ConstantClock())
    scanner.scan("profile-a", FleetSelection.one(1), source="consumer:a1")
    scanner.scan("profile-a", FleetSelection.one(1), source="consumer:a2")
    scanner.scan("profile-b", FleetSelection.one(1), source="consumer:b1")

    latest_a = state.state(
        "profile-a",
        FleetStateRequest(
            FleetSelection.one(1),
            FleetRefreshPolicy.NEVER,
        ),
        source="consumer:read",
    )
    latest_b = state.state(
        "profile-b",
        FleetStateRequest(
            FleetSelection.one(1),
            FleetRefreshPolicy.NEVER,
        ),
        source="consumer:read",
    )

    assert latest_a.observations[0].id == UUID(
        "00000000-0000-0000-0000-000000000021"
    )
    assert latest_b.observations[0].id == UUID(
        "00000000-0000-0000-0000-000000000031"
    )


def test_failed_short_transaction_does_not_rollback_prior_observation(database):
    _, _, scanner, state = _services(database)
    first = scanner.scan("profile", FleetSelection.one(1), source="consumer:first")
    observation = first.observations[0]

    with pytest.raises(SQLAlchemyError), database.get().begin() as connection:
        connection.execute(
            insert(formation_surface_fleet_slot).values(
                snapshot_id=observation.id,
                side="invalid",
                position=1,
                occupied=False,
            )
        )

    assert len(state.history("profile", 1, limit=10)) == 1


def test_schema_constraints_cover_requested_fleet_fk_and_unique_snapshot(database):
    unknown_run = uuid4()
    with pytest.raises(SQLAlchemyError), database.get().begin() as connection:
        connection.execute(
            insert(formation_surface_fleet_scan_request).values(
                run_id=unknown_run,
                fleet_index=1,
            )
        )

    with database.get().connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(formation_surface_fleet_scan_run)
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(formation_surface_fleet_snapshot)
        ).scalar_one() == 0
