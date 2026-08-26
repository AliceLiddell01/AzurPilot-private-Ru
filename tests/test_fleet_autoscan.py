from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from module.application.fleet_autoscan import (
    FLEET_AUTOSCAN_SOURCE,
    FleetAutoScanConfig,
    FleetAutoScanCoordinator,
)
from module.application.fleet_state import FleetScanBatchResult, FleetStateObservation
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)


def _snapshot(fleet_index: int, *, complete: bool) -> FormationFleetSnapshot:
    status = IdentityStatus.MATCHED if complete else IdentityStatus.UNRESOLVED
    occupied = FormationFleetSlotObservation(
        side=FormationFleetSide.MAIN,
        position=1,
        occupied=True,
        identity_status=status,
        raw_name_ocr="Enterprise",
        displayed_name="Enterprise",
        canonical_identity=(
            CanonicalShipIdentity("azur_lane_ship_group:1") if complete else None
        ),
        canonical_name="Enterprise" if complete else None,
        ship_form=ShipForm.BASE if complete else None,
    )
    empty = tuple(
        FormationFleetSlotObservation(side=side, position=position, occupied=False)
        for side, position in (
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
    )
    return FormationFleetSnapshot(
        fleet_index=fleet_index,
        slots=(occupied, *empty),
        catalog_fingerprint="a" * 64,
    )


def _batch(selection: FleetSelection) -> FleetScanBatchResult:
    run_id = uuid4()
    observations = tuple(
        FleetStateObservation(
            id=uuid4(),
            run_id=run_id,
            instance_id=uuid4(),
            idempotency_key=f"test:{run_id}:{fleet_index}",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
            snapshot=_snapshot(fleet_index, complete=fleet_index != 2),
        )
        for fleet_index in selection.fleet_indices
    )
    return FleetScanBatchResult(
        run_id=run_id,
        selection=selection,
        observations=observations,
        failed_fleet_index=None,
        failure_code=None,
    )


class _StateService:
    def __init__(self) -> None:
        self.calls = []

    def scan(self, instance, selection, *, source):
        self.calls.append((instance, selection.fleet_indices, source))
        return _batch(selection)


def test_config_normalizes_selection_and_rejects_invalid_values() -> None:
    config = FleetAutoScanConfig.from_raw([6, 2, 6, 1])
    assert config.selection.fleet_indices == (1, 2, 6)

    with pytest.raises(TypeError):
        FleetAutoScanConfig.from_raw("1,2")
    with pytest.raises(ValueError):
        FleetAutoScanConfig.from_raw([])
    with pytest.raises(ValueError):
        FleetAutoScanConfig.from_raw([1, 7])


def test_scheduler_coordinator_scans_selection_once_without_due_engine() -> None:
    state = _StateService()
    coordinator = FleetAutoScanCoordinator(state)

    result = coordinator.run("profile", FleetAutoScanConfig.from_raw([1, 2, 3]))

    assert state.calls == [("profile", (1, 2, 3), FLEET_AUTOSCAN_SOURCE)]
    assert result.selection.fleet_indices == (1, 2, 3)
    assert result.complete_fleet_indices == (1, 3)
    assert result.incomplete_fleet_indices == (2,)
