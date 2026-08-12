from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from module.combat.level import LevelOcr
from module.dock_inventory.attributes import DockLevelOcrAdapter
from module.dock_inventory.level_ocr import DockLevelOcr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dock_inventory" / "v14_levels"

CASES = (
    (
        "120_a.png.b64",
        1,
        "3034a651653e19727d2691705adc8c6831944ffe02af7f0f15615a4e22463d97",
        120,
        34,
    ),
    (
        "58_a.png.b64",
        8,
        "aa50b3c0e4e627141921c1311cda7e7017ad9e138fd8efd9d72ac499b4ed7caa",
        58,
        26,
    ),
)


def _read_fixture_base64(filename: str, part_count: int) -> str:
    if part_count == 1:
        return (FIXTURE_DIR / filename).read_text(encoding="ascii")

    parts = sorted(FIXTURE_DIR.glob(f"{filename}.part*"))
    assert len(parts) == part_count
    return "".join(part.read_text(encoding="ascii") for part in parts)


def _load_rgb(filename: str, part_count: int, expected_sha256: str) -> np.ndarray:
    encoded = _read_fixture_base64(filename, part_count)
    payload = base64.b64decode(encoded, validate=True)
    assert hashlib.sha256(payload).hexdigest() == expected_sha256

    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert bgr is not None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (31, 58, 3)
    return rgb


@pytest.mark.parametrize(
    (
        "filename",
        "part_count",
        "fixture_sha256",
        "_expected_level",
        "expected_width",
    ),
    CASES,
)
def test_dock_level_preprocessing_isolates_real_v14_digit_regions(
    filename: str,
    part_count: int,
    fixture_sha256: str,
    _expected_level: int,
    expected_width: int,
) -> None:
    rgb = _load_rgb(filename, part_count, fixture_sha256)

    combat = LevelOcr((0, 0, 58, 31), name="TEST_COMBAT_LEVEL").pre_process(rgb)
    dock = DockLevelOcr((0, 0, 58, 31), name="TEST_DOCK_LEVEL").pre_process(rgb)

    assert combat.shape == (1, 1)
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


def test_dock_level_ocr_reads_real_v14_failures() -> None:
    images = [
        _load_rgb(filename, part_count, digest)
        for filename, part_count, digest, *_rest in CASES
    ]
    expected = [
        expected_level
        for _filename, _part_count, _digest, expected_level, _width in CASES
    ]
    dummy_areas = [(0, 0, 58, 31)] * len(images)

    result = DockLevelOcr(
        dummy_areas,
        name="DOCK_LEVEL_OCR",
        threshold=64,
    ).ocr(images, direct_ocr=True)

    assert result == expected


@pytest.mark.parametrize(
    ("filename", "part_count", "fixture_sha256", "expected_level", "_width"),
    CASES,
)
def test_production_level_adapter_uses_dock_specific_ocr_on_v14_failures(
    filename: str,
    part_count: int,
    fixture_sha256: str,
    expected_level: int,
    _width: int,
) -> None:
    rgb = _load_rgb(filename, part_count, fixture_sha256)

    result = DockLevelOcrAdapter().read_levels(rgb, ((0, 0, 58, 31),))

    assert result == (expected_level,)
