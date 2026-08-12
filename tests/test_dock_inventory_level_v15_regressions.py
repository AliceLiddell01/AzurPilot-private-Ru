from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import cv2
import numpy as np

from module.dock_inventory.attributes import DockLevelOcrAdapter
from module.dock_inventory.level_ocr import DockLevelOcr


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "dock_inventory"
    / "v15_levels"
    / "level1_dim.png.b64"
)
FIXTURE_SHA256 = "a9180403f7758273c6912306ef2333202a82362f3110275dfba339cdc151709b"


def _load_rgb() -> np.ndarray:
    encoded = FIXTURE_PATH.read_text(encoding="ascii")
    payload = base64.b64decode(encoded, validate=True)
    assert hashlib.sha256(payload).hexdigest() == FIXTURE_SHA256
    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert bgr is not None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert rgb.shape == (31, 58, 3)
    return rgb


def test_v15_low_contrast_level1_gets_single_digit_preprocessing() -> None:
    result = DockLevelOcr(
        (0, 0, 58, 31),
        name="TEST_V15_LEVEL1",
        threshold=64,
    ).pre_process(_load_rgb())

    assert result.shape == (21, 13)
    assert set(np.unique(result)).issubset({0, 255})
    assert np.count_nonzero(result == 0) > 0


def test_v15_low_contrast_level1_is_read_by_bundled_ocr() -> None:
    rgb = _load_rgb()
    result = DockLevelOcr(
        [(0, 0, 58, 31)],
        name="DOCK_LEVEL_OCR",
        threshold=64,
    ).ocr([rgb], direct_ocr=True)

    assert result == 1


def test_production_adapter_reads_v15_low_contrast_level1() -> None:
    result = DockLevelOcrAdapter().read_levels(
        _load_rgb(),
        ((0, 0, 58, 31),),
    )

    assert result == (1,)
