from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

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
from module.persistence.schema import metadata

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


def _slot(side: FormationFleetSide, position: int, ship: int | None):
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
        ship_form=ShipForm.BASE,
    )


def _snapshot(ship: int) -> FormationFleetSnapshot:
    return FormationFleetSnapshot(
        fleet_index=1,
        slots=(
            _slot(FormationFleetSide.MAIN, 1, ship),
            _slot(FormationFleetSide.MAIN, 2, None),
            _slot(FormationFleetSide.MAIN, 3, None),
            _slot(FormationFleetSide.VANGUARD, 1, None),
            _slot(FormationFleetSide.VANGUARD, 2, None),
            _slot(FormationFleetSide.VANGUARD, 3, None),
        ),
        catalog_fingerprint="f" * 64,
    )


class _Controller:
    def __init__(self, ship: int = 1):
        self.ship = ship

    def scan_surface_fleet(self, fleet_index: int) -> FormationFleetSnapshot:
        assert fleet_index == 1
        return _snapshot(self.ship)


def _services(database, *, clock, controller):
    factory = lambda: PostgresUnitOfWork(database)
    return (
        FleetScanService(factory, controller, clock=clock),
        MoraleService(factory, clock=clock),
    )


def _command(*, baseline: Decimal = Decimal(50), key: str = "same-key"):
    return RecordMoraleObservation(
        fleet_index=1,
        side=FormationFleetSide.MAIN,
        position=1,
        canonical_identity=CanonicalShipIdentity("azur_lane_ship_group:1"),
        ship_form=ShipForm.BASE,
        baseline=baseline,
        recovery=MoraleRecoveryProfile.outside_dorm_base(),
        source="regression:exact",
        idempotency_key=key,
    )


def test_a_b_a_slot_sequence_breaks_old_morale_continuity(database):
    now = [datetime(2026, 8, 27, 0, 0, tzinfo=UTC)]
    controller = _Controller(1)
    scanner, morale = _services(database, clock=lambda: now[0], controller=controller)

    scanner.scan("profile", FleetSelection.one(1), source="regression:a1")
    morale.record("profile", _command())

    now[0] += timedelta(minutes=10)
    controller.ship = 2
    scanner.scan("profile", FleetSelection.one(1), source="regression:b")

    now[0] += timedelta(minutes=10)
    controller.ship = 1
    scanner.scan("profile", FleetSelection.one(1), source="regression:a2")

    slot = morale.fleet("profile", 1, at=now[0]).slots[0]
    assert slot.knowledge is MoraleKnowledge.UNKNOWN
    assert slot.current is None


def test_repeated_same_occupant_scan_preserves_morale_continuity(database):
    now = [datetime(2026, 8, 27, 0, 0, tzinfo=UTC)]
    controller = _Controller(1)
    scanner, morale = _services(database, clock=lambda: now[0], controller=controller)

    scanner.scan("profile", FleetSelection.one(1), source="regression:a1")
    morale.record("profile", _command())

    now[0] += timedelta(minutes=20)
    scanner.scan("profile", FleetSelection.one(1), source="regression:a2")

    slot = morale.fleet("profile", 1, at=now[0]).slots[0]
    assert slot.knowledge is MoraleKnowledge.PROJECTED
    assert slot.current == Decimal(56)


def test_same_caller_idempotency_key_is_isolated_by_app_instance(database):
    now = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
    controller = _Controller(1)
    scanner, morale = _services(database, clock=lambda: now, controller=controller)

    scanner.scan("profile-a", FleetSelection.one(1), source="regression:a")
    scanner.scan("profile-b", FleetSelection.one(1), source="regression:b")

    first = morale.record("profile-a", _command(baseline=Decimal(10), key="shared"))
    second = morale.record(
        "profile-b",
        replace(_command(baseline=Decimal(20), key="shared")),
    )

    assert first.instance_id != second.instance_id
    assert morale.fleet("profile-a", 1, at=now).slots[0].current == Decimal(10)
    assert morale.fleet("profile-b", 1, at=now).slots[0].current == Decimal(20)
