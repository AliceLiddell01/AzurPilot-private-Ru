from __future__ import annotations

import numpy as np
import pytest

from module.dock_inventory import attributes
from module.dock_inventory.attributes import DockLevelOcrError


class _FakeLevelOcr:
    values: tuple[object, ...] = ()
    calls: list[dict[str, object]] = []

    def __init__(self, buttons, **kwargs) -> None:
        self.buttons = tuple(buttons)
        self.kwargs = dict(kwargs)

    def ocr(self, _frame):
        type(self).calls.append(
            {"buttons": self.buttons, "kwargs": self.kwargs}
        )
        return list(type(self).values)


class _FakeDigit:
    runs: dict[int, tuple[object, ...]] = {}
    calls: list[dict[str, object]] = []

    def __init__(self, buttons, **kwargs) -> None:
        self.buttons = tuple(buttons)
        self.kwargs = dict(kwargs)

    def ocr(self, _frame):
        threshold = self.kwargs["threshold"]
        assert type(threshold) is int
        type(self).calls.append(
            {"buttons": self.buttons, "kwargs": self.kwargs}
        )
        return list(type(self).runs[threshold])


def _install_ocr_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    primary: tuple[object, ...],
    proof_runs: dict[int, tuple[object, ...]],
) -> None:
    _FakeLevelOcr.values = primary
    _FakeLevelOcr.calls = []
    _FakeDigit.runs = dict(proof_runs)
    _FakeDigit.calls = []
    monkeypatch.setattr(attributes, "LevelOcr", _FakeLevelOcr)
    monkeypatch.setattr(attributes, "Digit", _FakeDigit)


def test_v9_visual_level_failures_are_recovered_by_independent_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = (20, 0, 0, 0, 0, 0)
    expected = (120, 120, 120, 70, 70, 70)
    proof_runs = {
        96: expected,
        128: expected,
        160: expected,
    }
    _install_ocr_doubles(
        monkeypatch,
        primary=primary,
        proof_runs=proof_runs,
    )
    areas = tuple(
        (100 + index * 100, 50, 160 + index * 100, 81)
        for index in range(6)
    )

    result = attributes.DockLevelOcrAdapter().read_levels(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        areas,
    )

    assert result == expected
    assert len(_FakeLevelOcr.calls) == 1
    assert _FakeLevelOcr.calls[0]["kwargs"] == {
        "name": "DOCK_LEVEL_OCR",
        "threshold": 64,
    }
    expected_proof_areas = tuple(
        (left + 24, top, right, top + 22)
        for left, top, right, _bottom in areas
    )
    assert [call["buttons"] for call in _FakeDigit.calls] == [
        expected_proof_areas,
        expected_proof_areas,
        expected_proof_areas,
    ]
    assert [call["kwargs"]["lang"] for call in _FakeDigit.calls] == [
        "azur_lane",
        "azur_lane",
        "azur_lane",
    ]
    assert [call["kwargs"]["name"] for call in _FakeDigit.calls] == [
        "DOCK_LEVEL_DIGIT_PROOF_96",
        "DOCK_LEVEL_DIGIT_PROOF_128",
        "DOCK_LEVEL_DIGIT_PROOF_160",
    ]
    assert [call["kwargs"]["threshold"] for call in _FakeDigit.calls] == [
        96,
        128,
        160,
    ]


def test_level_reconciliation_repairs_dropped_leading_one() -> None:
    reconcile = attributes.DockLevelOcrAdapter._reconcile_value

    assert reconcile(20, (120, 120, 120)) == 120
    assert reconcile(24, (124, 124, 124)) == 124
    assert reconcile(25, (125, 125, 125)) == 125


def test_level_reconciliation_fails_closed_on_unexplained_disagreement() -> None:
    reconcile = attributes.DockLevelOcrAdapter._reconcile_value

    assert reconcile(70, (71, 72, 73)) == 0
    assert reconcile(70, (71, 71, 72)) == 0


def test_level_reconciliation_accepts_primary_with_one_independent_agreement() -> None:
    reconcile = attributes.DockLevelOcrAdapter._reconcile_value

    assert reconcile(120, (120, 119, 121)) == 120


def test_level_primary_result_count_mismatch_is_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ocr_doubles(
        monkeypatch,
        primary=(120,),
        proof_runs={
            96: (120, 70),
            128: (120, 70),
            160: (120, 70),
        },
    )

    with pytest.raises(DockLevelOcrError, match="primary level OCR results"):
        attributes.DockLevelOcrAdapter().read_levels(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            ((100, 50, 160, 81), (200, 50, 260, 81)),
        )

    assert _FakeDigit.calls == []


def test_level_proof_result_count_mismatch_is_operational_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_ocr_doubles(
        monkeypatch,
        primary=(120, 70),
        proof_runs={
            96: (120,),
            128: (120, 70),
            160: (120, 70),
        },
    )

    with pytest.raises(DockLevelOcrError, match="proof level OCR results"):
        attributes.DockLevelOcrAdapter().read_levels(
            np.zeros((720, 1280, 3), dtype=np.uint8),
            ((100, 50, 160, 81), (200, 50, 260, 81)),
        )
