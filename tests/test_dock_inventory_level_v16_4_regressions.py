from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from module.dock_inventory.attributes import DockLevelOcrAdapter
from module.dock_inventory.level_ocr import DockLevelOcr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dock_inventory" / "v16_4_levels"
CASES = (
    (
        "level_roon_125.png",
        "d0e2ee7339f55931d777ee70ebae46cb81f42782172ee09ef628b77dc2508ebd",
        125,
    ),
    (
        "level_hakuhou_1.png",
        "d53ff2ce2287f97c34a117e5283be64961c87f817fc3eb20823e905872de5592",
        1,
    ),
    (
        "level_max_immelmann_1.png",
        "3cb8013ab8b551b9ef8ded44f9c9253df72e1c2122c6523c6ae7314719ed397b",
        1,
    ),
    (
        "level_surrey_1.png",
        "80a65a592b85aa82e97801a6989678e95ebebe0c746cf140a150d7f66753598b",
        1,
    ),
    (
        "level_kazan_1.png",
        "1ef352a3340f998344205cf6b01ef9e14155ebdb28aecee404753bf3905d2dee",
        1,
    ),
    (
        "level_queen_anne_1.png",
        "f693d70a00621aba5c4ae725a288212409aca25cc67cbaf752a55a89610b2574",
        1,
    ),
    (
        "level_yuudachi_meta_1.png",
        "bb5439f28e34408ed9c156c4bfff0a281ad19ea32c832c3840dbbd9c148b7023",
        1,
    ),
    (
        "level_ryu_lion_1.png",
        "52d8a4688e1dbaafd0702d4edecf5c7db231f1e29f3aedf2884c6d87441e907d",
        1,
    ),
)


def _load_rgb(filename: str, expected_sha256: str) -> np.ndarray:
    payload = (FIXTURE_DIR / filename).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == expected_sha256
    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert bgr is not None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert rgb.dtype == np.uint8
    assert rgb.shape == (31, 58, 3)
    return rgb


def test_v16_4_roon_trailing_artwork_is_excluded_from_numeric_region() -> None:
    filename, digest, expected = CASES[0]
    assert expected == 125
    result = DockLevelOcr(
        (0, 0, 58, 31),
        name="TEST_V16_4_ROON_LEVEL",
        threshold=64,
    ).pre_process(_load_rgb(filename, digest))

    assert result.shape == (31, 34)
    assert np.count_nonzero(result[:, :-4] < 255) > 0
    assert np.all(result[:, -4:] == 255)


@pytest.mark.parametrize(
    ("filename", "fixture_sha256", "expected_level"),
    CASES[1:],
)
def test_v16_4_level1_artwork_cleanup_keeps_real_digit_evidence(
    filename: str,
    fixture_sha256: str,
    expected_level: int,
) -> None:
    assert expected_level == 1
    result = DockLevelOcr(
        (0, 0, 58, 31),
        name="TEST_V16_4_LEVEL1",
        threshold=64,
    ).pre_process(_load_rgb(filename, fixture_sha256))

    assert result.shape == (21, 13)
    assert set(np.unique(result)).issubset({0, 255})
    assert np.count_nonzero(result == 0) > 0
    dark_columns = np.flatnonzero(np.any(result == 0, axis=0))
    assert len(dark_columns) <= 7


def test_v16_4_level_failures_are_read_by_bundled_ocr() -> None:
    images = [_load_rgb(filename, digest) for filename, digest, _expected in CASES]
    expected = [expected for _filename, _digest, expected in CASES]

    result = DockLevelOcr(
        [(0, 0, 58, 31)] * len(images),
        name="DOCK_LEVEL_OCR",
        threshold=64,
    ).ocr(images, direct_ocr=True)

    assert result == expected


@pytest.mark.parametrize(
    ("filename", "fixture_sha256", "expected_level"),
    CASES,
)
def test_production_adapter_reads_real_v16_4_level_failures(
    filename: str,
    fixture_sha256: str,
    expected_level: int,
) -> None:
    result = DockLevelOcrAdapter().read_levels(
        _load_rgb(filename, fixture_sha256),
        ((0, 0, 58, 31),),
    )

    assert result == (expected_level,)
