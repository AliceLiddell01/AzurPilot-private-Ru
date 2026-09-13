from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from module.application.fleet_state import FleetStateObservation
from module.application.instance_identity import runtime_instance_identity
from module.application.morale import MoraleKnowledge, MoraleLocation
from module.application.morale_reconciliation import MoraleReconciliationService
from module.application.storage_models import InstanceIdentity
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormFloorSnapshot,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)

NOW = datetime(2026, 8, 27, 10, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _instance_id():
    return runtime_instance_identity("alas")[1]


def _identity(value=1):
    return CanonicalShipIdentity(f"azur_lane_ship_group:{value}")


def _slot(side, position, ship=None, form=ShipForm.BASE):
    if ship is None:
        return FormationFleetSlotObservation(
            side=side, position=position, occupied=False
        )
    return FormationFleetSlotObservation(
        side=side,
        position=position,
        occupied=True,
        identity_status=IdentityStatus.MATCHED,
        raw_name_ocr=f"Ship {ship}",
        displayed_name=f"Ship {ship}",
        canonical_identity=_identity(ship),
        canonical_name=f"Ship {ship}",
        ship_form=form,
    )


def _formation(
    instance_id, fleet=1, ship=1, form=ShipForm.BASE, at=NOW - timedelta(minutes=1)
):
    slots = (
        _slot(FormationFleetSide.MAIN, 1, ship, form),
        _slot(FormationFleetSide.MAIN, 2),
        _slot(FormationFleetSide.MAIN, 3),
        _slot(FormationFleetSide.VANGUARD, 1),
        _slot(FormationFleetSide.VANGUARD, 2),
        _slot(FormationFleetSide.VANGUARD, 3),
    )
    return FleetStateObservation(
        id=uuid4(),
        run_id=uuid4(),
        instance_id=instance_id,
        idempotency_key=f"fleet:{uuid4()}",
        observed_at=at,
        snapshot=FormationFleetSnapshot(fleet, slots, FINGERPRINT),
    )


def _observation(
    floor=DormFloor.FLOOR_1, ship=1, form=None, status=IdentityStatus.MATCHED
):
    matched = status is IdentityStatus.MATCHED
    return DormMoraleObservation(
        floor=floor,
        ordinal=1,
        raw_name_ocr=f"Ship {ship}",
        displayed_name=f"Ship {ship}",
        identity_status=status,
        canonical_identity=_identity(ship) if matched else None,
        canonical_name=f"Ship {ship}" if matched else None,
        ship_form=form if matched else None,
        morale=Decimal(100),
        recovery_per_hour=Decimal(47),
    )


def _scan(*observations, complete=True):
    grouped = {DormFloor.FLOOR_1: [], DormFloor.FLOOR_2: []}
    for item in observations:
        grouped[item.floor].append(item)
    attempts = []
    for floor in (DormFloor.FLOOR_1, DormFloor.FLOOR_2):
        if complete or floor is DormFloor.FLOOR_1:
            attempts.append(
                DormFloorScanAttempt(
                    floor,
                    DormFloorScanStatus.SUCCEEDED,
                    NOW,
                    DormFloorSnapshot(floor, tuple(grouped[floor]), FINGERPRINT),
                    None,
                )
            )
        else:
            attempts.append(
                DormFloorScanAttempt(
                    floor, DormFloorScanStatus.FAILED, error_code="ui_state"
                )
            )
    return DormMoraleScanResult(
        uuid4(),
        NOW - timedelta(seconds=2),
        NOW,
        tuple(attempts),
        "test:dorm",
        f"scan:{uuid4()}",
    )


class _Instances:
    def __init__(self, alias, instance_id):
        digest, _ = runtime_instance_identity(alias)
        self.value = (digest, InstanceIdentity(instance_id, alias))

    def resolve(self, *, alias_kind, alias_digest):
        return (
            self.value[1]
            if alias_kind == "legacy_instance" and alias_digest == self.value[0]
            else None
        )

    def register(self, *args, **kwargs):
        raise AssertionError("already seeded")


class _Fleet:
    def __init__(self, values):
        self.values, self.calls = values, 0

    def latest(self, instance_id, selection):
        self.calls += 1
        return tuple(
            x
            for x in self.values
            if x.instance_id == instance_id and x.fleet_index in selection.fleet_indices
        )


class _Morale:
    def __init__(self):
        self.values = []

    def append(self, value):
        self.values.append(value)
        return value


class _Dorm:
    def __init__(self):
        self.values = []

    def append_scan(self, instance_id, scan):
        self.values.append((instance_id, scan))
        return scan

    def latest(self, instance_id):
        return next((s for i, s in reversed(self.values) if i == instance_id), None)


class _Uow:
    def __init__(self, instances, fleet, morale, dorm):
        self.instances, self.fleet_state, self.morale, self.dorm_morale = (
            instances,
            fleet,
            morale,
            dorm,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def commit(self):
        pass

    def rollback(self):
        pass


def _service(formations):
    instance_id = formations[0].instance_id
    instances, fleet, morale, dorm = (
        _Instances("alas", instance_id),
        _Fleet(formations),
        _Morale(),
        _Dorm(),
    )
    service = MoraleReconciliationService(
        lambda: _Uow(instances, fleet, morale, dorm),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    return service, fleet, morale


def test_unique_unknown_form_writes_exact_ui_recovery_and_floor():
    instance_id = _instance_id()
    service, fleet, morale = _service((_formation(instance_id),))
    result = service.reconcile("alas", FleetSelection.one(1), _scan(_observation()))
    value = morale.values[0]
    assert result.exact_observations == 1 and fleet.calls == 1
    assert value.baseline == Decimal(
        100
    ) and value.recovery.recovery_per_hour == Decimal(47)
    assert (
        value.location is MoraleLocation.DORM_FLOOR_1
        and value.knowledge is MoraleKnowledge.EXACT
    )


def test_known_form_mismatch_fails_closed_and_does_not_mark_outside():
    instance_id = _instance_id()
    service, _, morale = _service((_formation(instance_id),))
    result = service.reconcile(
        "alas", FleetSelection.one(1), _scan(_observation(form=ShipForm.RETROFIT))
    )
    assert result.exact_observations == result.outside_dorm_observations == 0
    assert result.unmatched_observations == 1 and morale.values == []


def test_duplicate_candidates_are_ambiguous_and_one_observation_is_not_reused():
    instance_id = _instance_id()
    service, _, morale = _service(
        (_formation(instance_id, 1), _formation(instance_id, 2))
    )
    result = service.reconcile(
        "alas", FleetSelection.several(1, 2), _scan(_observation())
    )
    assert result.ambiguous_observations == 1 and morale.values == []


def test_complete_absence_writes_unknown_outside_but_partial_does_not():
    instance_id = _instance_id()
    for complete, expected in ((True, 1), (False, 0)):
        service, _, morale = _service((_formation(instance_id),))
        result = service.reconcile(
            "alas", FleetSelection.one(1), _scan(complete=complete)
        )
        assert result.outside_dorm_observations == expected
        if complete:
            value = morale.values[0]
            assert value.baseline is None and value.knowledge is MoraleKnowledge.UNKNOWN
            assert value.location is MoraleLocation.OUTSIDE_DORM
            assert value.recovery.recovery_per_hour == Decimal(20)
        else:
            assert morale.values == []


def test_unresolved_scan_and_stale_fleet_fail_closed():
    instance_id = _instance_id()
    service, _, morale = _service((_formation(instance_id),))
    result = service.reconcile(
        "alas",
        FleetSelection.one(1),
        _scan(_observation(status=IdentityStatus.UNRESOLVED)),
    )
    assert result.unresolved_observations == 1 and morale.values == []
    service, _, morale = _service(
        (_formation(instance_id, at=NOW + timedelta(seconds=1)),)
    )
    result = service.reconcile("alas", FleetSelection.one(1), _scan(_observation()))
    assert result.stale_fleet_indices == (1,) and morale.values == []
