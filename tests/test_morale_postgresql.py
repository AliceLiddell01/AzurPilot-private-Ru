from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, insert, select, text
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageConflictError
from module.application.fleet_state import FleetScanService
from module.application.morale import (
    MoraleKnowledge,
    MoraleRecoveryProfile,
    MoraleService,
    RecordMoraleObservation,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.persistence import DatabaseSettings, LazyEngine, PostgresUnitOfWork
from module.persistence.schema import (
    formation_surface_fleet_morale_observation,
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
    *,
    ship: int | None,
    form: ShipForm = ShipForm.BASE,
) -> FormationFleetSlotObservation:
    if ship is None:
        return FormationFleetSlotObservation(side=side, position=position, occupied=False)
    return FormationFleetSlotObservation(
        side=side,
        position=position,
        occupied=True,
        identity_status=IdentityStatus.MATCHED,
        raw_name_ocr=f"Ship {ship}",
        displayed_name=f"Ship {ship}",
        canonical_identity=CanonicalShipIdentity(f"azur_lane_ship_group:{ship}"),
        canonical_name=f"Ship {ship}",
        ship_form=form,
    )


def _snapshot(fleet_index: int, *, form: ShipForm = ShipForm.BASE):
    coordinates = (
        (FormationFleetSide.MAIN, 1, 1),
        (FormationFleetSide.MAIN, 2, 2),
        (FormationFleetSide.MAIN, 3, None),
        (FormationFleetSide.VANGUARD, 1, 1),
        (FormationFleetSide.VANGUARD, 2, None),
        (FormationFleetSide.VANGUARD, 3, None),
    )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=tuple(
            _slot(
                side,
                position,
                ship=ship,
                form=form if side is FormationFleetSide.MAIN and position == 1 else ShipForm.BASE,
            )
            for side, position, ship in coordinates
        ),
        catalog_fingerprint="c" * 64,
    )


class _Controller:
    def __init__(self, forms: dict[int, ShipForm] | None = None):
        self.forms = forms or {}

    def scan_surface_fleet(self, fleet_index):
        return _snapshot(fleet_index, form=self.forms.get(fleet_index, ShipForm.BASE))


def _services(database, *, clock, id_factory=uuid4, controller=None):
    factory = lambda: PostgresUnitOfWork(database)
    scanner = FleetScanService(
        factory,
        controller or _Controller(),
        clock=clock,
    )
    morale = MoraleService(factory, clock=clock, id_factory=id_factory)
    return scanner, morale


def _command(
    *,
    fleet_index=1,
    side=FormationFleetSide.MAIN,
    position=1,
    form=ShipForm.BASE,
    baseline=Decimal(50),
    key="morale:integration",
    observed_at=datetime(2026, 8, 27, tzinfo=UTC),
):
    ship = 1 if position == 1 else 2
    return RecordMoraleObservation(
        fleet_index=fleet_index,
        side=side,
        position=position,
        canonical_identity=CanonicalShipIdentity(f"azur_lane_ship_group:{ship}"),
        ship_form=form,
        baseline=baseline,
        recovery=MoraleRecoveryProfile.outside_dorm_base(),
        source="integration:exact",
        idempotency_key=key,
        observed_at=observed_at,
    )


def test_round_trip_projection_and_slot_provenance(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    scanner, morale = _services(database, clock=lambda: now)
    formation = scanner.scan(
        "profile", FleetSelection.one(1), source="integration:formation"
    ).observations[0]

    observation = morale.record("profile", _command())
    state = morale.fleet("profile", 1, at=now)

    assert observation.formation_snapshot_id == formation.id
    assert state.formation_observation_id == formation.id
    assert state.slots[0].current == Decimal(50)
    assert state.slots[0].knowledge is MoraleKnowledge.EXACT
    assert state.slots[3].knowledge is MoraleKnowledge.UNKNOWN
    with database.get().connect() as connection:
        row = connection.execute(
            select(formation_surface_fleet_morale_observation)
        ).mappings().one()
    assert row["formation_snapshot_id"] == formation.id
    assert row["canonical_identity_key"] == "azur_lane_ship_group:1"
    assert row["ship_form"] == ShipForm.BASE.value


def test_maximum_bounded_recovery_rate_round_trips_without_numeric_overflow(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    scanner, morale = _services(database, clock=lambda: now)
    scanner.scan("profile", FleetSelection.one(1), source="integration:formation")
    command = replace(
        _command(key="morale:max-rate"),
        recovery=MoraleRecoveryProfile(Decimal(1500), Decimal(150), "test:max-rate"),
    )

    morale.record("profile", command)
    state = morale.fleet("profile", 1, at=now)

    assert state.slots[0].recovery.recovery_per_hour == Decimal(1500)


def test_repository_idempotency_and_changed_payload_conflict(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    scanner, morale = _services(database, clock=lambda: now)
    scanner.scan("profile", FleetSelection.one(1), source="integration:formation")
    observation = morale.record("profile", _command())

    with PostgresUnitOfWork(database) as uow:
        assert uow.morale.append(replace(observation, id=uuid4())) == observation
        uow.commit()
    with pytest.raises(StorageConflictError), PostgresUnitOfWork(database) as uow:
        uow.morale.append(replace(observation, baseline=Decimal(49)))


def test_latest_is_deterministic_and_isolated_by_slot_fleet_and_instance(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    ids = iter(
        (
            UUID("00000000-0000-0000-0000-000000000010"),
            UUID("00000000-0000-0000-0000-000000000020"),
            UUID("00000000-0000-0000-0000-000000000030"),
            UUID("00000000-0000-0000-0000-000000000040"),
        )
    )
    scanner, morale = _services(database, clock=lambda: now, id_factory=lambda: next(ids))
    scanner.scan("profile-a", FleetSelection.several(1, 2), source="formation:a")
    scanner.scan("profile-b", FleetSelection.one(1), source="formation:b")
    morale.record("profile-a", _command(baseline=Decimal(10), key="a:old"))
    newest = morale.record("profile-a", _command(baseline=Decimal(20), key="a:new"))
    morale.record(
        "profile-a",
        _command(
            side=FormationFleetSide.VANGUARD,
            baseline=Decimal(30),
            key="a:other-slot",
        ),
    )
    morale.record(
        "profile-a",
        _command(fleet_index=2, baseline=Decimal(40), key="a:other-fleet"),
    )
    _, morale_b = _services(database, clock=lambda: now)
    morale_b.record("profile-b", _command(baseline=Decimal(90), key="b"))

    a = morale.state("profile-a", FleetSelection.several(1, 2), at=now)
    b = morale_b.fleet("profile-b", 1, at=now)

    assert a.fleets[0].slots[0].morale_observation_id == newest.id
    assert a.fleets[0].slots[0].current == Decimal(20)
    assert a.fleets[0].slots[3].current == Decimal(30)
    assert a.fleets[1].slots[0].current == Decimal(40)
    assert b.slots[0].current == Decimal(90)


def test_database_constraints_reject_wrong_snapshot_identity_and_values(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    scanner, morale = _services(database, clock=lambda: now)
    scanner.scan("profile", FleetSelection.one(1), source="integration:formation")
    observation = morale.record("profile", _command())
    table = formation_surface_fleet_morale_observation
    with database.get().connect() as connection:
        stored = connection.execute(select(table)).mappings().one()

    invalid = dict(stored)
    invalid.update(
        id=uuid4(),
        idempotency_key="invalid:identity",
        canonical_identity_key="azur_lane_ship_group:999999",
    )
    invalid.pop("payload_digest")
    invalid["payload_digest"] = "d" * 64
    with pytest.raises(SQLAlchemyError), database.get().begin() as connection:
        connection.execute(insert(table).values(**invalid))

    invalid = dict(stored)
    invalid.update(
        id=uuid4(),
        idempotency_key="invalid:baseline",
        baseline=Decimal(151),
        payload_digest="e" * 64,
    )
    with pytest.raises(SQLAlchemyError), database.get().begin() as connection:
        connection.execute(insert(table).values(**invalid))
    assert observation.baseline == Decimal(50)


def test_full_fleet_read_is_set_based_and_latest_index_is_available(database):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    scanner, morale = _services(database, clock=lambda: now)
    scanner.scan("profile", FleetSelection.all(), source="integration:formation")
    for fleet_index in range(1, 7):
        morale.record(
            "profile",
            _command(fleet_index=fleet_index, key=f"morale:{fleet_index}"),
        )

    statements = []
    engine = database.get()

    def before_cursor_execute(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        state = morale.state("profile", FleetSelection.all(), at=now)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert len(state.fleets) == 6
    assert len(statements) == 4
    with engine.begin() as connection:
        instance_id = connection.execute(
            select(formation_surface_fleet_morale_observation.c.instance_id).limit(1)
        ).scalar_one()
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            connection.execute(
                text(
                    "EXPLAIN SELECT * "
                    "FROM azurpilot.formation_surface_fleet_morale_observation "
                    "WHERE instance_id = :instance_id AND fleet_index = 1 "
                    "AND side = 'main' AND position = 1 "
                    "ORDER BY observed_at DESC, id DESC LIMIT 1"
                ),
                {"instance_id": instance_id},
            ).scalars()
        )
    assert "ix_formation_fleet_morale_latest" in plan
