from __future__ import annotations

import pytest

from module.dock_inventory.attributes import DockStarScanner


@pytest.mark.parametrize(
    ("shape_score", "fill_ratio", "upper_fill_ratio", "fill_match_score"),
    (
        (0.655724, 0.311111, 0.400000, 0.267088),
        (0.628430, 0.288889, 0.166667, 0.315208),
    ),
)
def test_v16_4_real_filled_glyphs_fit_high_match_visual_rule(
    shape_score: float,
    fill_ratio: float,
    upper_fill_ratio: float,
    fill_match_score: float,
) -> None:
    scanner = DockStarScanner()

    assert shape_score >= scanner.SHAPE_SCORE_MIN
    assert fill_match_score >= scanner.FILLED_MATCH_MIN
    assert fill_ratio >= scanner.FILLED_MATCH_RATIO_MIN
    assert fill_ratio > scanner.EMPTY_RATIO_MAX

    # На v16.4 оба glyph были UNKNOWN только из-за старого 0.32 cutoff:
    # strong и weak branches сами по себе эти реальные lower-density fills
    # не принимали, а high-match evidence уже было достаточным.
    assert not (
        fill_ratio >= scanner.FILLED_RATIO_MIN
        and upper_fill_ratio >= scanner.FILLED_UPPER_RATIO_MIN
    )
    assert not (
        scanner.FILLED_WEAK_MATCH_MIN
        <= fill_match_score
        < scanner.FILLED_MATCH_MIN
    )


def test_v16_4_calibration_stays_above_empty_boundary() -> None:
    scanner = DockStarScanner()

    assert scanner.FILLED_MATCH_RATIO_MIN == pytest.approx(0.28)
    assert scanner.FILLED_MATCH_RATIO_MIN > scanner.EMPTY_RATIO_MAX
