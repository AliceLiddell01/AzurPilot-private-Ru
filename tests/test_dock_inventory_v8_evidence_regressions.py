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


class _FallbackDigit:
    values: list[int] = []
    calls: list[tuple[tuple[int, int, int, int], ...]] = []

    def __init__(self, buttons, **_kwargs) -> None:
        self.buttons = tuple(buttons)

    def ocr(self, _frame):
        type(self).calls.append(self.buttons)
        return list(type(self).values)


def _install_level_fakes(monkeypatch, primary: list[int], fallback: list[int]) -> None:
    _PrimaryLevelOcr.values = list(primary)
    _PrimaryLevelOcr.calls = []
    _FallbackDigit.values = list(fallback)
    _FallbackDigit.calls = []
    monkeypatch.setattr(attributes, "LevelOcr", _PrimaryLevelOcr)
    monkeypatch.setattr(attributes, "Digit", _FallbackDigit)


def test_level_adapter_retries_only_zeroes_on_digit_only_roi(monkeypatch) -> None:
    _install_level_fakes(monkeypatch, [125, 0, 0], [99, 89])
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
        (499, 300, 557, 331),
    )

    result = attributes.DockLevelOcrAdapter().read_levels(frame, areas)

    assert result == (125, 99, 89)
    assert _PrimaryLevelOcr.calls == [areas]
    assert _FallbackDigit.calls == [
        (
            (358, 202, 392, 229),
            (523, 302, 557, 329),
        )
    ]


def test_level_adapter_does_not_retry_positive_primary_values(monkeypatch) -> None:
    _install_level_fakes(monkeypatch, [125, 120], [])
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
    )

    assert attributes.DockLevelOcrAdapter().read_levels(frame, areas) == (125, 120)
    assert _FallbackDigit.calls == []


def test_level_adapter_fallback_count_mismatch_is_typed_error(monkeypatch) -> None:
    _install_level_fakes(monkeypatch, [0, 0], [99])
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    areas = (
        (170, 100, 228, 131),
        (334, 200, 392, 231),
    )

    with pytest.raises(DockLevelOcrError, match="fallback level OCR results"):
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
