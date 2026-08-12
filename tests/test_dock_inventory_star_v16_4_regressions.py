from __future__ import annotations

import pytest

from module.dock_inventory.attributes import DockStarScanner


@pytest.mark.parametrize(
    ("mode", "shape_score", "fill_ratio", "upper_fill_ratio", "fill_match_score"),
    (
        ("upper_heavy", 0.655724, 0.311111, 0.400000, 0.267088),
        ("lower_heavy_strong_match", 0.628430, 0.288889, 0.166667, 0.315208),
    ),
)
def test_v16_4_real_filled_glyphs_fit_narrow_visual_rules(
    mode: str,
    shape_score: float,
    fill_ratio: float,
    upper_fill_ratio: float,
    fill_match_score: float,
) -> None:
    scanner = DockStarScanner()

    assert shape_score >= scanner.SHAPE_SCORE_MIN
    assert not (
        fill_match_score >= scanner.FILLED_MATCH_MIN
        and fill_ratio >= scanner.FILLED_MATCH_RATIO_MIN
    )

    if mode == "upper_heavy":
        assert fill_match_score >= scanner.FILLED_UPPER_HEAVY_MATCH_MIN
        assert fill_ratio >= scanner.FILLED_UPPER_HEAVY_RATIO_MIN
        assert upper_fill_ratio >= scanner.FILLED_UPPER_HEAVY_UPPER_RATIO_MIN
    else:
        assert mode == "lower_heavy_strong_match"
        assert fill_match_score >= scanner.FILLED_LOWER_HEAVY_STRONG_MATCH_MIN
        assert fill_ratio >= scanner.FILLED_LOWER_HEAVY_RATIO_MIN
        assert upper_fill_ratio >= scanner.FILLED_LOWER_HEAVY_UPPER_RATIO_MIN


def test_v16_4_generic_match_boundary_stays_fail_closed() -> None:
    scanner = DockStarScanner()

    assert scanner.FILLED_MATCH_RATIO_MIN == pytest.approx(0.32)
    # Existing image-level ambiguous-circle regression has approximately these
    # metrics; both new v16.4 branches must remain outside that evidence.
    ambiguous_fill = 0.288889
    ambiguous_upper = 0.300000
    ambiguous_match = 0.297788

    assert ambiguous_fill < scanner.FILLED_UPPER_HEAVY_RATIO_MIN
    assert ambiguous_match < scanner.FILLED_LOWER_HEAVY_STRONG_MATCH_MIN
    assert not (
        ambiguous_fill >= scanner.FILLED_RATIO_MIN
        and ambiguous_upper >= scanner.FILLED_UPPER_RATIO_MIN
    )
