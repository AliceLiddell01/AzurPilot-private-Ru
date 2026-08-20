import argparse

import pytest

from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from tools.acceptance.device import AcceptanceFailure
from tools.acceptance.formation import _confirm_snapshot, _print_snapshot, _snapshot_payload


def _snapshot(
    *,
    matched: bool,
    displayed_name: str = "Alabama",
    canonical_name: str = "Alabama",
) -> FormationFleetSnapshot:
    if matched:
        first = FormationFleetSlotObservation(
            side=FormationFleetSide.MAIN,
            position=1,
            occupied=True,
            identity_status=IdentityStatus.MATCHED,
            raw_name_ocr=displayed_name,
            displayed_name=displayed_name,
            canonical_identity=CanonicalShipIdentity("azur_lane_ship_group:1"),
            canonical_name=canonical_name,
        )
    else:
        first = FormationFleetSlotObservation(
            side=FormationFleetSide.MAIN,
            position=1,
            occupied=True,
            identity_status=IdentityStatus.UNRESOLVED,
            raw_name_ocr="",
            displayed_name="",
        )
    empty = (
        FormationFleetSlotObservation(FormationFleetSide.MAIN, 2, False),
        FormationFleetSlotObservation(FormationFleetSide.MAIN, 3, False),
        FormationFleetSlotObservation(FormationFleetSide.VANGUARD, 1, False),
        FormationFleetSlotObservation(FormationFleetSide.VANGUARD, 2, False),
        FormationFleetSlotObservation(FormationFleetSide.VANGUARD, 3, False),
    )
    return FormationFleetSnapshot(
        fleet_index=6,
        slots=(first, *empty),
        catalog_fingerprint="0" * 64,
    )


def test_snapshot_payload_preserves_slot_identity_and_empty_slots() -> None:
    payload = _snapshot_payload(_snapshot(matched=True))

    assert payload["fleet_index"] == 6
    assert payload["occupied_count"] == 1
    assert payload["complete"] is True
    assert payload["slots"][0]["canonical_id"] == "azur_lane_ship_group:1"
    assert payload["slots"][1]["occupied"] is False


def test_print_snapshot_keeps_exact_displayed_retrofit_name(capsys) -> None:
    snapshot = _snapshot(
        matched=True,
        displayed_name="Belfast (Retrofit)",
        canonical_name="Belfast",
    )

    _print_snapshot(snapshot)

    output = capsys.readouterr().out
    assert "Belfast (Retrofit) -> Belfast" in output
    assert "[сопоставлен]" in output


def test_non_interactive_confirmation_requires_exact_match() -> None:
    args = argparse.Namespace(non_interactive=True, confirmed_match="MATCH")

    assert _confirm_snapshot(_snapshot(matched=True), args) == "MATCH"


def test_incomplete_snapshot_fails_before_manual_confirmation() -> None:
    args = argparse.Namespace(non_interactive=True, confirmed_match="MATCH")

    with pytest.raises(AcceptanceFailure, match="нераспознанные"):
        _confirm_snapshot(_snapshot(matched=False), args)
