from decimal import Decimal

import numpy as np
import pytest

from module.dock_inventory.model import IdentityStatus
from module.dorm.morale_model import DormFloor
from module.dorm.morale_scanner import (
    DormMoraleInputError,
    DormMoraleOcrError,
    DormMoraleScanner,
    parse_morale_value,
    parse_recovery_speed,
)


class _Names:
    def __init__(self, values):
        self.values = values

    def read_names(self, _frame, areas):
        return tuple(self.values[: len(areas)])


class _Values:
    def __init__(self, values):
        self.values = values

    def read_values(self, _frame, areas):
        return tuple(self.values[: len(areas)])


def _frame(*, occupied=(1, 3)):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    scanner = DormMoraleScanner()
    for ordinal in occupied:
        x1, y1, x2, y2 = scanner.layout.cards[ordinal - 1].presence_area
        frame[y1:y2, x1:x2] = (0, 255, 0)
    return frame


def test_parser_normalizes_points_per_hour_and_fails_closed():
    assert parse_morale_value("150") == Decimal(150)
    assert parse_recovery_speed(" 40 / h ") == Decimal(40)
    for malformed in ("", "151", "?", "40/h"):
        with pytest.raises(DormMoraleOcrError):
            parse_morale_value(malformed)
    for malformed in ("", "40", "fast/h", "1501/h"):
        with pytest.raises(DormMoraleOcrError):
            parse_recovery_speed(malformed)


def test_scanner_detects_present_cards_and_does_not_invent_base_form():
    scanner = DormMoraleScanner(
        name_ocr=_Names(("Arizona", "Nubian")),
        morale_ocr=_Values((Decimal(120), Decimal(130))),
        recovery_ocr=_Values((Decimal(40), Decimal(50))),
    )
    result = scanner.scan(_frame(), floor=DormFloor.FLOOR_1)
    assert [(item.ordinal, item.displayed_name) for item in result.observations] == [
        (1, "Arizona"),
        (3, "Nubian"),
    ]
    assert all(
        item.identity_status is IdentityStatus.MATCHED for item in result.observations
    )
    assert all(item.ship_form is None for item in result.observations)
    assert result.observations[0].floor is DormFloor.FLOOR_1


def test_scanner_normalizes_full_resolution_frame():
    scanner = DormMoraleScanner(
        name_ocr=_Names(("Arizona",)),
        morale_ocr=_Values((Decimal(150),)),
        recovery_ocr=_Values((Decimal(40),)),
    )
    frame = np.repeat(np.repeat(_frame(occupied=(1,)), 3, axis=0), 3, axis=1)
    assert scanner.scan(frame, floor=DormFloor.FLOOR_2).observations[0].ordinal == 1


@pytest.mark.parametrize("frame", [None, np.zeros((10, 10)), np.zeros((720, 1279, 3))])
def test_scanner_rejects_invalid_frame(frame):
    with pytest.raises(DormMoraleInputError):
        DormMoraleScanner().scan(frame, floor=DormFloor.FLOOR_1)


def test_scanner_rejects_ocr_count_mismatch():
    scanner = DormMoraleScanner(
        name_ocr=_Names(("Arizona",)),
        morale_ocr=_Values(()),
        recovery_ocr=_Values((Decimal(40),)),
    )
    with pytest.raises(DormMoraleOcrError):
        scanner.scan(_frame(occupied=(1,)), floor=DormFloor.FLOOR_1)


def test_scanner_preserves_unresolved_identity():
    scanner = DormMoraleScanner(
        name_ocr=_Names(("Definitely Not A Ship",)),
        morale_ocr=_Values((Decimal(100),)),
        recovery_ocr=_Values((Decimal(40),)),
    )
    item = scanner.scan(_frame(occupied=(1,)), floor=DormFloor.FLOOR_1).observations[0]
    assert item.identity_status is IdentityStatus.UNRESOLVED
    assert item.canonical_identity is None
