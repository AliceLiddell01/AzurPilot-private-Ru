from __future__ import annotations

import numpy as np
import pytest

from module.dock_inventory import attributes
from module.dock_inventory.attributes import (
    DockLevelOcrError,
    DockStarScanner,
)


class _PrimaryLevelOcr:
    values: list[int] = []
    calls: list[tuple[tuple[int, int, int, int], ...]] = []

    def __init__(self, buttons, **_kwargs) -> None:
        self.buttons = tuple(buttons)

    def ocr(self, _frame):
        type(self).calls.append(self.buttons)
        return list(type(self).values)


class _ProofDigit:
    values_by_threshold: dict[int, list[int]] = {}
    calls: list[
        tuple[tuple[tuple[int, int, int, int], ...], dict[str, object]]
    ] = []

    def __init__(self, buttons, **kwargs) -> None:
        self.buttons = tuple(buttons)
        self.kwargs = dict(kwargs)

    def ocr(self, _frame):
        type(self).calls.append((self.buttons, self.kwargs))
        threshold = self.kwargs["threshold"]
        assert type(threshold) is int
        return list(type(self).values_by_threshold[threshold])


def _install_level_fakes(
    monkeypatch,
    primary: list[int],
    proof_runs: dict[int, list[int]],
) -> None:
    _PrimaryLevelOcr.values = list(primary)
    _PrimaryLevelOcr.calls = []
    _ProofDigit.values_by_threshold = {
        threshold: list(values)
        for threshold, values in proof_runs.items()
    }
    _ProofDigit.calls = []
    monkeypatch.setattr(attributes, "LevelOcr", _PrimaryLevelOcr)
    monkeypatch.setattr(attributes, "Digit", _ProofDigit)


def test_level_adapter_proves_all_values_on_digit_only_roi(monkeypatch) -> None:
    proof = [125, 99, 89]
    _install_level_fakes(
        monkeypatch,
        [125, 0, 0],
        {96: proof, 128: proof, 160: proof},
    )
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
        (499, 300, 557, 331),
    )

    result = attributes.DockLevelOcrAdapter().read_levels(frame, areas)

    assert result == (125, 99, 89)
    assert _PrimaryLevelOcr.calls == [areas]
    expected_proof_areas = (
        (194, 100, 228, 122),
        (358, 200, 392, 222),
        (523, 300, 557, 322),
    )
    assert [call[0] for call in _ProofDigit.calls] == [
        expected_proof_areas,
        expected_proof_areas,
        expected_proof_areas,
    ]


def test_level_adapter_independently_confirms_positive_primary_values(
    monkeypatch,
) -> None:
    proof = [125, 120]
    _install_level_fakes(
        monkeypatch,
        [125, 120],
        {96: proof, 128: proof, 160: proof},
    )
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
    )

    assert attributes.DockLevelOcrAdapter().read_levels(frame, areas) == (125, 120)
    assert len(_ProofDigit.calls) == 3


def test_level_adapter_proof_count_mismatch_is_typed_error(monkeypatch) -> None:
    _install_level_fakes(
        monkeypatch,
        [0, 0],
        {
            96: [99],
            128: [99, 89],
            160: [99, 89],
        },
    )
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
    )

    with pytest.raises(DockLevelOcrError, match="proof level OCR results"):
        attributes.DockLevelOcrAdapter().read_levels(frame, areas)


def _valid_four_star_first_component() -> np.ndarray:
    mask = np.zeros((26, 138), dtype=np.uint8)
    # 9x9 component: area=81, width/height=9, centroid=(48, 16).
    mask[12:21, 44:53] = 1
    return mask


def _template_response() -> np.ndarray:
    return np.zeros((8, 120), dtype=np.float32)


def test_short_four_star_candidate_fails_closed_when_six_endpoints_exist() -> None:
    scanner = DockStarScanner()
    mask = _valid_four_star_first_component()
    matched = _template_response()

    half = scanner.STAR_TEMPLATE_SIZE // 2
    first_six = round(scanner.SUPPORTED_TOTAL_FIRST_CENTERS[6])
    last_six = round(
        scanner.SUPPORTED_TOTAL_FIRST_CENTERS[6] + 5 * scanner.STAR_SPACING
    )
    top = 16 - half
    matched[top, first_six - half] = scanner.FILLED_WEAK_MATCH_MIN + 0.05
    matched[top, last_six - half] = scanner.FILLED_WEAK_MATCH_MIN + 0.05

    assert scanner._first_filled_star(mask, matched) is None


def test_genuine_four_star_candidate_is_not_rejected_without_six_endpoints() -> None:
    scanner = DockStarScanner()
    mask = _valid_four_star_first_component()
    matched = _template_response()

    result = scanner._first_filled_star(mask, matched)

    assert result is not None
    assert result[2] == 4


def test_one_six_endpoint_is_not_enough_to_reject_four_star_candidate() -> None:
    scanner = DockStarScanner()
    mask = _valid_four_star_first_component()
    matched = _template_response()

    half = scanner.STAR_TEMPLATE_SIZE // 2
    first_six = round(scanner.SUPPORTED_TOTAL_FIRST_CENTERS[6])
    top = 16 - half
    matched[top, first_six - half] = scanner.FILLED_WEAK_MATCH_MIN + 0.05

    result = scanner._first_filled_star(mask, matched)

    assert result is not None
    assert result[2] == 4