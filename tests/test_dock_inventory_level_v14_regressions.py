from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from module.combat.level import LevelOcr
from module.dock_inventory.level_ocr import DockLevelOcr


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "dock_inventory" / "v14_level_crops.npz"
)

CASES = (
    ("120_a", 120, True, 34),
    ("120_b", 120, True, 34),
    ("58_a", 58, True, 26),
    ("58_b", 58, True, 26),
    ("125_ok", 125, False, 34),
    ("81_ok", 81, False, 26),
    ("1_ok", 1, False, 13),
)


def _load_rgb(key: str) -> np.ndarray:
    with np.load(FIXTURE_PATH) as fixtures:
        rgb = np.array(fixtures[key], copy=True)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (31, 58, 3)
    return rgb


@pytest.mark.parametrize(
    ("key", "_expected_level", "combat_is_blank", "expected_width"),
    CASES,
)
def test_dock_level_preprocessing_isolates_real_v14_digit_regions(
    key: str,
    _expected_level: int,
    combat_is_blank: bool,
    expected_width: int,
) -> None:
    rgb = _load_rgb(key)

    combat = LevelOcr((0, 0, 58, 31), name="TEST_COMBAT_LEVEL").pre_process(rgb)
    dock = DockLevelOcr((0, 0, 58, 31), name="TEST_DOCK_LEVEL").pre_process(rgb)

    assert (combat.shape == (1, 1)) is combat_is_blank
    assert dock.shape == (31, expected_width)
    assert np.count_nonzero(dock < 127) > 0


def test_dock_level_preprocessing_fails_closed_without_digit_evidence() -> None:
    blank = np.full((31, 58, 3), 255, dtype=np.uint8)

    result = DockLevelOcr(
        (0, 0, 58, 31),
        name="TEST_DOCK_LEVEL_BLANK",
    ).pre_process(blank)

    assert result.shape == (1, 1)
    assert int(result[0, 0]) == 255


def test_dock_level_ocr_reads_real_v14_failures_and_controls() -> None:
    images = [_load_rgb(key) for key, *_rest in CASES]
    expected = [expected_level for _key, expected_level, *_rest in CASES]
    dummy_areas = [(0, 0, 58, 31)] * len(images)

    result = DockLevelOcr(
        dummy_areas,
        name="DOCK_LEVEL_OCR",
        threshold=64,
    ).ocr(images, direct_ocr=True)

    assert result == expected
