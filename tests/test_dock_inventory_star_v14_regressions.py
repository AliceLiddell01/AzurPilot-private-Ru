from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from module.dock_inventory.attributes import (
    DockStarGlyphState,
    DockStarScanner,
    DockStarStatus,
)
from module.dock_inventory.model import StarObservation


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dock_inventory" / "v14_stars"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "case_6_filled_lower_heavy.png",
            StarObservation(filled=6, empty=0, total=6),
        ),
        (
            "case_5_three_filled_low_centroid.png",
            StarObservation(filled=3, empty=2, total=5),
        ),
    ],
)
def test_v14_acceptance_star_crops_are_observed_fail_closed(
    filename: str,
    expected: StarObservation,
) -> None:
    """Lock the two distinct real v14 failure modes without ship-specific logic."""

    bgr = cv2.imread(str(FIXTURE_DIR / filename), cv2.IMREAD_COLOR)
    assert bgr is not None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert rgb.shape == (26, 138, 3)

    result = DockStarScanner()._scan_area(
        rgb,
        (0, 0, 138, 26),
        width=138,
        height=26,
    )

    assert result.status is DockStarStatus.OBSERVED
    assert result.stars == expected
    assert result.detected_total == expected.total
    assert len(result.glyphs) == expected.total
    assert all(
        glyph.state is not DockStarGlyphState.UNKNOWN for glyph in result.glyphs
    )
    assert sum(
        glyph.state is DockStarGlyphState.FILLED for glyph in result.glyphs
    ) == expected.filled
    assert sum(
        glyph.state is DockStarGlyphState.EMPTY for glyph in result.glyphs
    ) == expected.empty
