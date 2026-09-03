from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from module.application.fleet_state import FleetStateObservation
from module.application.instance_identity import runtime_instance_identity
from module.application.morale import (
    MoraleKnowledge,
    MoraleLocation,
    project_morale,
)
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
    floor=DormFloor.FLOOR_1,
    ship=1,
    form=None,
    status=IdentityStatus.MATCHED,
    ordinal=1,
):
    matched = status is IdentityStatus.MATCHED
    return DormMoraleObservation(
        floor=floor,
        ordinal=ordinal,
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

    def latest(self, instance_id, selection):
        latest = {}
        for value in self.values:
            if (
                value.instance_id == instance_id
                and value.fleet_index in selection.fleet_indices
            ):
                latest[(value.fleet_index, value.side, value.position)] = value
        return tuple(
            sorted(
                latest.values(),
                key=lambda value: (
                    value.fleet_index,
                    value.side.value,
                    value.position,
                ),
            )
        )


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
        clock=lambda: NOW + timedelta(minutes=20),
    )
    return service, fleet, morale


def test_unique_unknown_form_writes_exact_ui_recovery_and_floor():
    instance_id = _instance_id()
    service, fleet, morale = _service((_formation(instance_id),))
    result = service.reconcile("alas", FleetSelection.one(1), _scan(_observation()))
    value = morale.values[0]
    assert result.exact_observations == 1 and fleet.calls == 1
    assert result.lookup_targets == ()
    assert value.baseline == Decimal(100)
    assert value.recovery.recovery_per_hour == Decimal(47)
    assert value.location is MoraleLocation.DORM_FLOOR_1
    assert value.knowledge is MoraleKnowledge.EXACT


def test_known_form_mismatch_routes_target_to_lookup_without_fake_outside():
    instance_id = _instance_id()
    service, _, _morale = _service((_formation(instance_id),))
    result = service.reconcile(
        "alas", FleetSelection.one(1), _scan(_observation(form=ShipForm.RETROFIT))
    )
    assert result.exact_observations == result.outside_dorm_observations == 0
    assert result.unmatched_observations == 1
    assert len(result.lookup_targets) == 1
    assert morale.values == []


def test_duplicate_candidates_are_ambiguous_and_each_target_needs_lookup():
    instance_id = _instance_id()
    service, _, morale = _service(
        (_formation(instance_id, 1), _formation(instance_id, 2))
    )
    result = service.reconcile(
        "alas", FleetSelection.several(1, 2), _scan(_observation())
    )
    assert result.ambiguous_observations == 1
    assert len(result.lookup_targets) == 2
    assert morale.values == []


def test_complete_absence_never_synthesizes_initial_119():
    instance_id = _instance_id()
    service, _, _morale = _service((_formation(instance_id),))
    result = service.reconcile("alas", FleetSelection.one(1), _scan())
    assert result.complete_scan is True
    assert result.outside_dorm_observations == 0
    assert len(result.lookup_targets) == 1
    assert morale.values == []


def test_partial_scan_also_routes_missing_target_to_lookup_without_outside_claim():
    instance_id = _instance_id()
    service, _, morale = _service((_formation(instance_id),))
    result = service.reconcile(
        "alas", FleetSelection.one(1), _scan(complete=False)
    )
    assert result.complete_scan is False
    assert len(result.lookup_targets) == 1
    assert result.outside_dorm_observations == 0
    assert morale.values == []


def test_unrelated_unresolved_dorm_card_does_not_block_exact_target():
    instance_id = _instance_id()
    service, _, morale = _service((_formation(instance_id),))
    unrelated = _observation(
        ship=99,
        status=IdentityStatus.UNRESOLVED,
        ordinal=2,
    )
    result = service.reconcile(
        "alas",
        FleetSelection.one(1),
        _scan(_observation(), unrelated),
    )
    assert result.unresolved_observations == 1
    assert result.exact_observations == 1
    assert result.lookup_targets == ()
    assert len(morale.values) == 1


def test_unrelated_matched_dorm_ship_is_counted_but_does_not_block_target_lookup():
    instance_id = _instance_id()
    service, _, morale = _service((_formation(instance_id),))
    result = service.reconcile(
        "alas",
        FleetSelection.one(1),
        _scan(_observation(ship=99)),
    )
    assert result.unmatched_observations == 1
    assert len(result.lookup_targets) == 1
    assert morale.values == []


def test_targeted_search_records_exact_150_outside_and_time_does_not_clamp_down():
    instance_id = _instance_id()
    service, _, _morale = _service((_formation(instance_id),))
    scan = _scan()
    result = service.reconcile("alas", FleetSelection.one(1), scan)
    target = result.lookup_targets[0]

    value = service.record_targeted_outside(
        "alas",
        target,
        dorm_scan_id=result.dorm_scan_id,
        morale=Decimal(150),
        observed_at=NOW + timedelta(minutes=1),
    )

    assert value.baseline == Decimal(150)
    assert value.knowledge is MoraleKnowledge.EXACT
    assert value.location is MoraleLocation.OUTSIDE_DORM
    assert value.recovery.recovery_per_hour == Decimal(20)
    assert value.recovery.recovery_ceiling == Decimal(119)
    projected = project_morale(value, at=NOW + timedelta(minutes=13))
    assert projected.value == Decimal(150)


def test_targeted_search_below_119_recovers_only_to_outside_ceiling():
    instance_id = _instance_id()
    service, _, _ = _service((_formation(instance_id),))
    result = service.reconcile("alas", FleetSelection.one(1), _scan())
    value = service.record_targeted_outside(
        "alas",
        result.lookup_targets[0],
        dorm_scan_id=result.dorm_scan_id,
        morale=Decimal(118),
        observed_at=NOW + timedelta(minutes=1),
    )
    projected = project_morale(value, at=NOW + timedelta(hours=1, minutes=1))
    assert projected.value == Decimal(119)


def test_stale_fleet_never_enters_targeted_lookup_queue():
    instance_id = _instance_id()
    service, _, morale = _service(
        (_formation(instance_id, at=NOW + timedelta(seconds=1)),)
    )
    result = service.reconcile("alas", FleetSelection.one(1), _scan(_observation()))
    assert result.stale_fleet_indices == (1,)
    assert result.target_count == 1
    assert result.lookup_targets == ()
    assert morale.values == []
