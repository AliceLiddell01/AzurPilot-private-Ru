from __future__ import annotations

import pytest

from module.application.fleet_page import FleetSlotState, FleetSlotViewModel
from module.dock_inventory.model import ShipForm
from module.formation.model import FormationFleetSide
from module.webui.app_fleet_page import fleet_slot_text


@pytest.mark.parametrize(
    "canonical_name",
    ("San Diego", "Hammann", "Unicorn", "Generic Test Ship"),
)
def test_retrofit_form_projects_canonical_display_name_for_any_ship(
    canonical_name: str,
) -> None:
    slot = FleetSlotViewModel(
        side=FormationFleetSide.VANGUARD,
        position=1,
        state=FleetSlotState.MATCHED,
        canonical_identity="azur_lane_ship_group:900001",
        canonical_name=canonical_name,
        displayed_name=f"{canonical_name} (Retrofit)",
        ship_form=ShipForm.RETROFIT,
    )

    assert slot.canonical_identity == "azur_lane_ship_group:900001"
    assert slot.canonical_name == canonical_name
    assert slot.canonical_display_name == f"{canonical_name} (Retrofit)"
    assert fleet_slot_text(slot) == f"{canonical_name} (Retrofit)"


def test_base_form_projects_name_without_retrofit_suffix() -> None:
    slot = FleetSlotViewModel(
        side=FormationFleetSide.MAIN,
        position=1,
        state=FleetSlotState.MATCHED,
        canonical_identity="azur_lane_ship_group:900002",
        canonical_name="Generic Base Ship",
        displayed_name="Generic Base Ship",
        ship_form=ShipForm.BASE,
    )

    assert slot.canonical_display_name == "Generic Base Ship"
    assert fleet_slot_text(slot) == "Generic Base Ship"
