from __future__ import annotations

import pytest

from module.application.fleet_page import FleetSlotState, FleetSlotViewModel
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import FormationFleetSide, FormationFleetSlotObservation


def test_matched_formation_slot_requires_ship_form() -> None:
    with pytest.raises(ValueError, match="ship form"):
        FormationFleetSlotObservation(
            side=FormationFleetSide.MAIN,
            position=1,
            occupied=True,
            identity_status=IdentityStatus.MATCHED,
            raw_name_ocr="Generic Test Ship",
            displayed_name="Generic Test Ship",
            canonical_identity=CanonicalShipIdentity("azur_lane_ship_group:99999"),
            canonical_name="Generic Test Ship",
        )


def test_matched_view_slot_requires_ship_form() -> None:
    with pytest.raises(ValueError, match="ship form"):
        FleetSlotViewModel(
            side=FormationFleetSide.MAIN,
            position=1,
            state=FleetSlotState.MATCHED,
            canonical_identity="azur_lane_ship_group:99999",
            canonical_name="Generic Test Ship",
            displayed_name="Generic Test Ship",
            ship_form=None,
        )


def test_nonmatched_view_slot_rejects_ship_form() -> None:
    with pytest.raises(ValueError, match="Только MATCHED"):
        FleetSlotViewModel(
            side=FormationFleetSide.MAIN,
            position=1,
            state=FleetSlotState.UNRESOLVED,
            canonical_identity=None,
            canonical_name=None,
            displayed_name="Generic Test Ship",
            ship_form=ShipForm.RETROFIT,
        )
