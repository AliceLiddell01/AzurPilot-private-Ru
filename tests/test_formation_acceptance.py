import argparse
from types import SimpleNamespace

import pytest

from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.formation.scanner import FormationFleetOcrError
from tools.acceptance import formation as formation_acceptance
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


class _FailingAcceptanceController:
    def __init__(self, config_name, device=None) -> None:
        self.config = SimpleNamespace(SERVER="en")
        self.device = SimpleNamespace()

    def scan_surface_fleet(self, fleet_index, *, close_info=True):
        raise FormationFleetOcrError("ошибка сканирования")


def test_scan_failure_still_reports_profile_config_mutation(monkeypatch) -> None:
    hashes = iter(("before", "after"))
    args = argparse.Namespace(
        profile="test",
        fleet=6,
        serial="fixture",
        serial_from_config=False,
        expected_head=None,
        non_interactive=True,
        confirmed_match="MATCH",
    )

    monkeypatch.setattr(formation_acceptance, "_validate_profile_name", lambda profile: None)
    monkeypatch.setattr(formation_acceptance, "_git_head_sha", lambda: "1" * 40)
    monkeypatch.setattr(formation_acceptance, "_sha256", lambda path: next(hashes))
    monkeypatch.setattr(formation_acceptance, "_load_profile", lambda profile: {})
    monkeypatch.setattr(formation_acceptance, "_resolve_serial", lambda args, profile: "fixture")
    monkeypatch.setattr(
        formation_acceptance,
        "FormationFleetController",
        _FailingAcceptanceController,
    )
    monkeypatch.setattr(
        formation_acceptance,
        "_close_info_without_masking",
        lambda runner, primary: None,
    )

    with pytest.raises(FormationFleetOcrError, match="ошибка сканирования") as error_info:
        formation_acceptance.run_acceptance(args)

    assert any(
        "изменила постоянный config профиля" in note
        for note in error_info.value.__notes__
    )


def test_profile_load_failure_still_reports_profile_config_mutation(monkeypatch) -> None:
    hashes = iter(("before", "after"))
    args = argparse.Namespace(
        profile="test",
        fleet=6,
        serial="fixture",
        serial_from_config=False,
        expected_head=None,
        non_interactive=True,
        confirmed_match="MATCH",
    )

    monkeypatch.setattr(formation_acceptance, "_validate_profile_name", lambda profile: None)
    monkeypatch.setattr(formation_acceptance, "_git_head_sha", lambda: "1" * 40)
    monkeypatch.setattr(formation_acceptance, "_sha256", lambda path: next(hashes))

    def fail_profile_load(profile):
        raise AcceptanceFailure("ошибка загрузки профиля")

    monkeypatch.setattr(formation_acceptance, "_load_profile", fail_profile_load)

    with pytest.raises(AcceptanceFailure, match="ошибка загрузки профиля") as error_info:
        formation_acceptance.run_acceptance(args)

    assert any(
        "изменила постоянный config профиля" in note
        for note in error_info.value.__notes__
    )


def test_keyboard_interrupt_stays_primary_when_config_changes(monkeypatch) -> None:
    hashes = iter(("before", "after"))
    args = argparse.Namespace(
        profile="test",
        fleet=6,
        serial="fixture",
        serial_from_config=False,
        expected_head=None,
        non_interactive=True,
        confirmed_match="MATCH",
    )

    monkeypatch.setattr(formation_acceptance, "_validate_profile_name", lambda profile: None)
    monkeypatch.setattr(formation_acceptance, "_git_head_sha", lambda: "1" * 40)
    monkeypatch.setattr(formation_acceptance, "_sha256", lambda path: next(hashes))

    def interrupt_profile_load(profile):
        raise KeyboardInterrupt

    monkeypatch.setattr(formation_acceptance, "_load_profile", interrupt_profile_load)

    with pytest.raises(KeyboardInterrupt) as error_info:
        formation_acceptance.run_acceptance(args)

    assert any(
        "изменила постоянный config профиля" in note
        for note in error_info.value.__notes__
    )


def test_config_hash_read_failure_does_not_mask_scan_error(monkeypatch) -> None:
    args = argparse.Namespace(
        profile="test",
        fleet=6,
        serial="fixture",
        serial_from_config=False,
        expected_head=None,
        non_interactive=True,
        confirmed_match="MATCH",
    )
    hash_calls = 0

    def hash_with_final_read_failure(path):
        nonlocal hash_calls
        hash_calls += 1
        if hash_calls == 1:
            return "before"
        raise OSError("ошибка чтения config")

    monkeypatch.setattr(formation_acceptance, "_validate_profile_name", lambda profile: None)
    monkeypatch.setattr(formation_acceptance, "_git_head_sha", lambda: "1" * 40)
    monkeypatch.setattr(formation_acceptance, "_sha256", hash_with_final_read_failure)
    monkeypatch.setattr(formation_acceptance, "_load_profile", lambda profile: {})
    monkeypatch.setattr(formation_acceptance, "_resolve_serial", lambda args, profile: "fixture")
    monkeypatch.setattr(
        formation_acceptance,
        "FormationFleetController",
        _FailingAcceptanceController,
    )
    monkeypatch.setattr(
        formation_acceptance,
        "_close_info_without_masking",
        lambda runner, primary: None,
    )

    with pytest.raises(FormationFleetOcrError, match="ошибка сканирования") as error_info:
        formation_acceptance.run_acceptance(args)

    assert any(
        "Не удалось проверить неизменность постоянного config профиля" in note
        for note in error_info.value.__notes__
    )
