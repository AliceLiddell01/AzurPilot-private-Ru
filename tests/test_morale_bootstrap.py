from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from module.application.morale import (
    MoraleFleetState,
    MoraleKnowledge,
    MoraleLocation,
    MoraleRecoveryProfile,
    MoraleSelectionState,
    MoraleSlotState,
)
from module.application.morale_bootstrap import (
    CampaignMoraleBootstrapError,
    CampaignMoraleBootstrapper,
)
from module.application.morale_reconciliation import (
    MoraleReconciliationResult,
    TargetedMoraleLookupTarget,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dorm.morale_lookup import (
    TargetedMoraleLocationHint,
    TargetedMoraleLookupObservation,
)
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormFloorSnapshot,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.formation.model import FleetSelection, FormationFleetSide

NOW = datetime(2026, 8, 29, 10, tzinfo=UTC)
FINGERPRINT = "a" * 64


def _identity(value):
    return CanonicalShipIdentity(f"azur_lane_ship_group:{value}")


def _empty_slot(fleet, side, position):
    return MoraleSlotState(
        fleet_index=fleet,
        side=side,
        position=position,
        occupied=False,
        identity_status=None,
        canonical_identity=None,
        canonical_name=None,
        ship_form=None,
        knowledge=MoraleKnowledge.UNKNOWN,
    )


def _target_slot(fleet=6, *, exact=False, dorm_scan_id=None):
    kwargs = {}
    if exact:
        kwargs = {
            "knowledge": MoraleKnowledge.EXACT,
            "baseline": Decimal(150),
            "current": Decimal(150),
            "recovery": MoraleRecoveryProfile.outside_dorm_base(),
            "observed_at": NOW,
            "source": "targeted_search:exact",
            "morale_observation_id": uuid4(),
            "location": MoraleLocation.OUTSIDE_DORM,
            "dorm_scan_id": dorm_scan_id or uuid4(),
        }
    else:
        kwargs = {"knowledge": MoraleKnowledge.UNKNOWN}
    return MoraleSlotState(
        fleet_index=fleet,
        side=FormationFleetSide.MAIN,
        position=1,
        occupied=True,
        identity_status=IdentityStatus.MATCHED,
        canonical_identity=_identity(1),
        canonical_name="Argus",
        ship_form=ShipForm.BASE,
        **kwargs,
    )


def _state(slot):
    fleet = slot.fleet_index
    slots = (
        slot,
        _empty_slot(fleet, FormationFleetSide.MAIN, 2),
        _empty_slot(fleet, FormationFleetSide.MAIN, 3),
        _empty_slot(fleet, FormationFleetSide.VANGUARD, 1),
        _empty_slot(fleet, FormationFleetSide.VANGUARD, 2),
        _empty_slot(fleet, FormationFleetSide.VANGUARD, 3),
    )
    return MoraleSelectionState(
        FleetSelection.one(fleet),
        (MoraleFleetState(fleet, uuid4(), NOW, slots),),
        NOW,
    )


def _dorm_observation(
    ship,
    ordinal,
    *,
    status=IdentityStatus.MATCHED,
    morale=Decimal(150),
):
    matched = status is IdentityStatus.MATCHED
    return DormMoraleObservation(
        floor=DormFloor.FLOOR_1,
        ordinal=ordinal,
        raw_name_ocr=f"Ship {ship}",
        displayed_name=f"Ship {ship}",
        identity_status=status,
        morale=morale,
        recovery_per_hour=Decimal(40),
        canonical_identity=_identity(ship) if matched else None,
        canonical_name=(
            "Argus" if ship == 1 and matched else (f"Ship {ship}" if matched else None)
        ),
        ship_form=ShipForm.BASE if matched else None,
    )


def _scan(*observations, complete=True):
    attempts = (
        DormFloorScanAttempt(
            floor=DormFloor.FLOOR_1,
            status=DormFloorScanStatus.SUCCEEDED,
            observed_at=NOW,
            snapshot=DormFloorSnapshot(
                DormFloor.FLOOR_1,
                tuple(observations),
                FINGERPRINT,
            ),
        ),
        (
            DormFloorScanAttempt(
                floor=DormFloor.FLOOR_2,
                status=DormFloorScanStatus.SUCCEEDED,
                observed_at=NOW,
                snapshot=DormFloorSnapshot(DormFloor.FLOOR_2, (), FINGERPRINT),
            )
            if complete
            else DormFloorScanAttempt(
                floor=DormFloor.FLOOR_2,
                status=DormFloorScanStatus.FAILED,
                error_code="synthetic_floor_2_failure",
            )
        ),
    )
    return DormMoraleScanResult(
        id=uuid4(),
        started_at=NOW,
        finished_at=NOW,
        attempts=attempts,
        source="campaign:test",
        idempotency_key=f"scan:{uuid4()}",
    )


class _Config:
    def __init__(self):
        self.config_name = "alas"
        self.task = SimpleNamespace(command="Main")
        self.Fleet_Fleet1 = 6
        self.Fleet_Fleet2 = 2
        self.Fleet_FleetOrder = "fleet1_all_fleet2_standby"
        self.delays = []
        self.calls = []

    def task_delay(self, **kwargs):
        self.delays.append(kwargs)

    def task_call(self, task, force_call=True):
        self.calls.append((task, force_call))
        return True


class _MoraleService:
    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.calls = 0

    def state(self, instance, selection):
        assert instance == "alas"
        assert selection.fleet_indices == (6,)
        self.calls += 1
        return self.before if self.calls == 1 else self.after


class _Reconciliation:
    def __init__(self, lookup_targets=()):
        self.lookup_targets = tuple(lookup_targets)
        self.scans = []
        self.recorded = []

    def reconcile(self, instance, selection, scan):
        self.scans.append(scan)
        return MoraleReconciliationResult(
            dorm_scan_id=scan.id,
            complete_scan=scan.complete,
            exact_observations=len(scan.observations),
            outside_dorm_observations=0,
            ambiguous_observations=0,
            unresolved_observations=0,
            unmatched_observations=0,
            stale_fleet_indices=(),
            target_count=1,
            lookup_targets=self.lookup_targets,
        )

    def record_targeted_outside(self, instance, target, **kwargs):
        self.recorded.append((instance, target, kwargs))
        return SimpleNamespace()


class _DormController:
    def __init__(self):
        self.open_scans = []
        self.close_calls = 0
        self.ensure_calls = []

    def open_candidate_selection(self, scan):
        self.open_scans.append(scan)

    def close_train(self):
        self.close_calls += 1

    def ui_ensure(self, page):
        self.ensure_calls.append(page)


class _Lookup:
    def __init__(self, observations):
        self.observations = list(observations)
        self.targets = []
        self.exits = 0

    def lookup(self, target):
        self.targets.append(target)
        return self.observations.pop(0)

    def exit_to_main(self):
        self.exits += 1


def _target():
    return TargetedMoraleLookupTarget(
        fleet_index=6,
        side=FormationFleetSide.MAIN,
        position=1,
        canonical_identity=_identity(1),
        canonical_name="Argus",
        ship_form=ShipForm.BASE,
    )


def _bootstrap(
    *,
    scan,
    before=None,
    after=None,
    lookup_targets=(),
    lookup_observations=(),
):
    config = _Config()
    before = before or _state(_target_slot())
    after = after or _state(_target_slot(exact=True, dorm_scan_id=scan.id))
    morale = _MoraleService(before, after)
    reconciliation = _Reconciliation(lookup_targets)
    context = SimpleNamespace(morale_service=morale, reconciliation_service=reconciliation)
    controller = _DormController()
    lookup = _Lookup(lookup_observations)

    bootstrapper = CampaignMoraleBootstrapper(
        config,
        object(),
        controller,
        context_factory=lambda require_ready=False: context,
        lookup_factory=lambda config, device=None: lookup,
    )
    return bootstrapper, config, controller, reconciliation, lookup


def test_bootstrap_filters_unrelated_dorm_cards_before_reconciliation():
    scan = _scan(
        _dorm_observation(1, 1),
        _dorm_observation(99, 2),
        _dorm_observation(100, 3, status=IdentityStatus.UNRESOLVED),
    )
    bootstrapper, config, controller, reconciliation, lookup = _bootstrap(scan=scan)

    filtered, summary = bootstrapper.run(scan)

    assert [item.canonical_identity for item in filtered.observations] == [_identity(1)]
    assert reconciliation.scans[0] is filtered
    assert summary.dorm_exact == 1
    assert summary.unmatched_unrelated == 1
    assert summary.unresolved_raw == 1
    assert summary.final_exact == summary.target_count == 1
    assert controller.close_calls == 1
    assert len(controller.ensure_calls) == 1
    assert lookup.targets == []
    assert config.delays == []


def test_missing_target_uses_raw_train_occupant_for_search_and_records_exact_outside():
    target = _target()
    scan = _scan(_dorm_observation(99, 1))
    observed = TargetedMoraleLookupObservation(
        target=target,
        morale=Decimal(150),
        location_hint=TargetedMoraleLocationHint.OUTSIDE_DORM,
        fleet_badge=6,
        matched_result_count=1,
        observed_at=NOW,
    )
    bootstrapper, config, controller, reconciliation, lookup = _bootstrap(
        scan=scan,
        lookup_targets=(target,),
        lookup_observations=(observed,),
    )

    filtered, summary = bootstrapper.run(scan)

    assert filtered.observations == ()
    assert controller.open_scans == [scan]
    assert lookup.targets == [target]
    assert lookup.exits == 1
    assert len(reconciliation.recorded) == 1
    assert reconciliation.recorded[0][2]["morale"] == Decimal(150)
    assert summary.targeted_outside == 1
    assert summary.final_exact == 1
    assert config.delays == []


def test_search_selected_fails_closed_without_outside_write_and_suppresses_restart():
    target = _target()
    scan = _scan(_dorm_observation(99, 1))
    observed = TargetedMoraleLookupObservation(
        target=target,
        morale=Decimal(150),
        location_hint=TargetedMoraleLocationHint.TRAIN,
        fleet_badge=6,
        matched_result_count=1,
        observed_at=NOW,
    )
    bootstrapper, config, controller, reconciliation, lookup = _bootstrap(
        scan=scan,
        lookup_targets=(target,),
        lookup_observations=(observed,),
    )

    with pytest.raises(CampaignMoraleBootstrapError) as exc:
        bootstrapper.run(scan)

    assert exc.value.code == "lookup_dorm_location_requires_recovery"
    assert reconciliation.recorded == []
    assert lookup.exits == 1
    assert config.delays == [{"success": False}]
    assert config.task_call("Restart") is False
    assert config.calls == []


def test_partial_dorm_scan_delays_only_campaign_task_and_does_not_schedule_restart():
    scan = _scan(_dorm_observation(1, 1), complete=False)
    bootstrapper, config, controller, _, _ = _bootstrap(scan=scan)

    with pytest.raises(CampaignMoraleBootstrapError) as exc:
        bootstrapper.run(scan)

    assert exc.value.code == "dorm_scan_incomplete"
    assert config.delays == [{"success": False}]
    assert config.task_call("Restart") is False
    assert config.calls == []
    assert controller.close_calls == 1
    assert len(controller.ensure_calls) == 1
