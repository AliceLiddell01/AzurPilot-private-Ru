from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from module.application.morale_reconciliation import TargetedMoraleLookupTarget
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dorm.morale_lookup import (
    TargetedMoraleLocationHint,
    TargetedMoraleLookupController,
    TargetedMoraleLookupError,
    TargetedMoraleLookupLayout,
    TargetedMoraleLookupScanner,
)
from module.formation.model import FormationFleetSide

NOW = datetime(2026, 8, 29, tzinfo=UTC)


def _target(*, fleet=6, form=ShipForm.BASE):
    return TargetedMoraleLookupTarget(
        fleet_index=fleet,
        side=FormationFleetSide.MAIN,
        position=1,
        canonical_identity=CanonicalShipIdentity("azur_lane_ship_group:1"),
        canonical_name="Argus",
        ship_form=form,
    )


class _NameOcr:
    def __init__(self, values):
        self.values = tuple(values)

    def read_names(self, _frame, areas):
        return self.values[: len(tuple(areas))]


class _MoraleOcr:
    def __init__(self, value):
        self.value = Decimal(value)

    def read_values(self, _frame, areas):
        return tuple(self.value for _ in areas)


class _TextOcr:
    def __init__(self, fleet=(), state=()):
        self.outputs = [tuple(fleet), tuple(state)]
        self.calls = 0

    def read_values(self, _frame, areas):
        areas = tuple(areas)
        output = self.outputs[self.calls]
        self.calls += 1
        return output[: len(areas)]


class _Resolver:
    def __init__(self, target):
        self.target = target

    def resolve(self, raw):
        matched = raw.startswith("Argus")
        return SimpleNamespace(
            status=IdentityStatus.MATCHED if matched else IdentityStatus.UNRESOLVED,
            canonical_identity=self.target.canonical_identity if matched else None,
            ship_form=self.target.ship_form if matched else None,
        )


def _frame(scanner, present_indices):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cards = scanner.layout.cards()
    # RGB зелёный гарантированно проходит presence mask.
    for index in present_indices:
        x1, y1, x2, y2 = cards[index].presence_area
        frame[y1:y2, x1:x2] = (80, 220, 80)
    return frame


def _scanner(target, *, names, morale="150", fleet=(), state=()):
    return TargetedMoraleLookupScanner(
        catalog=object(),
        name_ocr=_NameOcr(names),
        morale_ocr=_MoraleOcr(morale),
        text_ocr=_TextOcr(fleet=fleet, state=state),
        resolver=_Resolver(target),
    )


def test_search_morale_crop_matches_dock_digit_geometry():
    # Первый реальный card после REMOVE — col=1,row=0. Узкий crop должен
    # совпадать с проверенной CARD_EMOTION_GRIDS и не захватывать иконку/арт.
    card = TargetedMoraleLookupLayout().cards()[0]
    assert card.morale_area == (281, 105, 306, 128)


def test_one_filtered_result_reads_exact_150_without_fake_119():
    target = _target()
    scanner = _scanner(
        target,
        names=("Argus",),
        morale="150",
        fleet=("FLEET 6",),
        state=("",),
    )
    result = scanner.scan(_frame(scanner, (0,)), target, observed_at=NOW)
    assert result.morale == Decimal(150)
    assert result.location_hint is TargetedMoraleLocationHint.OUTSIDE_DORM
    assert result.matched_result_count == 1


def test_search_card_119_is_exact_current_not_a_synthetic_default():
    target = _target()
    scanner = _scanner(
        target,
        names=("Argus",),
        morale="119",
        fleet=("FLEET 6",),
        state=("",),
    )
    result = scanner.scan(_frame(scanner, (0,)), target, observed_at=NOW)
    assert result.morale == Decimal(119)


def test_single_result_fails_closed_when_physical_fleet_badge_mismatches():
    target = _target(fleet=6)
    scanner = _scanner(
        target,
        names=("Argus",),
        fleet=("FLEET 1",),
        state=("",),
    )

    with pytest.raises(TargetedMoraleLookupError) as exc:
        scanner.scan(_frame(scanner, (0,)), target, observed_at=NOW)

    assert exc.value.error_code == "fleet_not_proven"


class _TypedMoraleOcr:
    def read_values(self, _frame, _areas):
        raise TargetedMoraleLookupError("morale_ocr_failed", "synthetic typed error")


def test_morale_lookup_preserves_declared_ocr_error():
    target = _target()
    scanner = _scanner(target, names=("Argus",), fleet=("FLEET 6",), state=("",))
    scanner.morale_ocr = _TypedMoraleOcr()

    with pytest.raises(TargetedMoraleLookupError) as exc:
        scanner.scan(_frame(scanner, (0,)), target, observed_at=NOW)

    assert exc.value.error_code == "morale_ocr_failed"
    assert str(exc.value) == "synthetic typed error"


@pytest.mark.parametrize(
    ("overlay", "expected"),
    (
        ("SELECTED", TargetedMoraleLocationHint.TRAIN),
        ("Resting", TargetedMoraleLocationHint.REST),
    ),
)
def test_selection_overlay_prevents_false_outside_classification(overlay, expected):
    target = _target()
    scanner = _scanner(
        target,
        names=("Argus",),
        fleet=("FLEET 6",),
        state=(overlay,),
    )
    result = scanner.scan(_frame(scanner, (0,)), target, observed_at=NOW)
    assert result.location_hint is expected


def test_duplicate_copies_use_physical_fleet_badge_as_discriminator():
    target = _target(fleet=6)
    scanner = _scanner(
        target,
        names=("Argus", "Argus"),
        fleet=("FLEET 1", "FLEET 6"),
        state=("", ""),
    )
    result = scanner.scan(_frame(scanner, (0, 1)), target, observed_at=NOW)
    assert result.fleet_badge == 6
    assert result.matched_result_count == 2


def test_duplicate_copies_fail_closed_when_fleet_badge_is_not_unique():
    target = _target(fleet=6)
    scanner = _scanner(
        target,
        names=("Argus", "Argus"),
        fleet=("", ""),
        state=("", ""),
    )
    with pytest.raises(TargetedMoraleLookupError) as exc:
        scanner.scan(_frame(scanner, (0, 1)), target, observed_at=NOW)
    assert exc.value.error_code == "duplicate_ambiguous"
    assert "неоднозначны" in str(exc.value)


def test_no_result_fails_closed():
    target = _target()
    scanner = _scanner(target, names=())
    with pytest.raises(TargetedMoraleLookupError) as exc:
        scanner.scan(np.zeros((720, 1280, 3), dtype=np.uint8), target, observed_at=NOW)
    assert exc.value.error_code == "no_result"


def test_retrofit_query_uses_catalog_canonical_name_without_separate_roster():
    target = _target(form=ShipForm.RETROFIT)
    assert target.search_query == "Argus"


class _Device:
    def __init__(self):
        self.inputs = []
        self.clicks = []
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.raise_on_input = False

    def text_input_and_confirm(self, text, clear=False):
        if self.raise_on_input:
            raise RuntimeError("synthetic input failure")
        self.inputs.append((text, clear))

    def click(self, button):
        self.clicks.append(button.name)

    def screenshot(self):
        return None


class _ScannerResult:
    def scan(self, _frame, target, observed_at=None):
        return SimpleNamespace(target=target, observed_at=observed_at)


class _Controller(TargetedMoraleLookupController):
    def __init__(self, device):
        self.device = device
        self.config = SimpleNamespace()
        self._scanner = _ScannerResult()
        self.targeted_morale_scanner = self._scanner

    def activate_search(self):
        return self.device.image

    def _capture(self):
        return self.device.image


def test_lookup_focuses_search_input_and_never_clicks_result_or_confirm():
    target = _target()
    device = _Device()
    controller = _Controller(device)
    result = controller.lookup(target)
    assert result.target is target
    assert device.inputs == [("Argus", True)]
    assert device.clicks == ["MORALE_LOOKUP_SEARCH_INPUT"]


def test_lookup_wraps_search_input_failure_in_declared_error():
    target = _target()
    device = _Device()
    device.raise_on_input = True
    controller = _Controller(device)

    with pytest.raises(TargetedMoraleLookupError) as exc:
        controller.lookup(target)

    assert exc.value.error_code == "search_input_failed"


class _ActivatingController(TargetedMoraleLookupController):
    def __init__(self, frames):
        self.device = _Device()
        self.config = SimpleNamespace()
        self._frames = list(frames)

    def _capture(self):
        return self._frames.pop(0)

    def appear(self, *_args, **_kwargs):
        return True

    def _search_active(self, frame):
        return bool(frame[0, 0, 0])


def test_activate_search_waits_for_confirmed_active_state():
    inactive = np.zeros((720, 1280, 3), dtype=np.uint8)
    active = np.ones((720, 1280, 3), dtype=np.uint8)
    controller = _ActivatingController((inactive, inactive, active))

    frame = controller.activate_search()

    assert frame is active
    assert controller.device.clicks == ["MORALE_LOOKUP_SEARCH"]
