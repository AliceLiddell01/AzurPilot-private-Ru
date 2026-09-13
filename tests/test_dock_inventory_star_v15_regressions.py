from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import cv2
import numpy as np

from module.dock_inventory.attributes import DockStarScanner


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dock_inventory" / "v15_stars"
FIXTURE_PREFIX = "low_level_first_star_patch.png.b64.part"
FIXTURE_PART_COUNT = 3
FIXTURE_SHA256 = "3280eb48d934cfa54a788bd5224e92046d84005c28a7292c8032b682daa66d13"
PATCH_AREA = (33, 6, 50, 23)
STAR_ROI_SHAPE = (26, 138, 3)


def _load_patch_rgb() -> np.ndarray:
    parts = tuple(
        FIXTURE_DIR / f"{FIXTURE_PREFIX}{index:02d}"
        for index in range(1, FIXTURE_PART_COUNT + 1)
    )
    assert all(part.is_file() for part in parts)
    encoded = "".join(
        "".join(part.read_text(encoding="ascii").split()) for part in parts
    )
    payload = base64.b64decode(encoded, validate=True)
    assert hashlib.sha256(payload).hexdigest() == FIXTURE_SHA256
    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert bgr is not None
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert rgb.shape == (17, 17, 3)
    return rgb


def _first_mask(
    roi: np.ndarray,
    *,
    saturation_min: int,
    value_min: int,
) -> np.ndarray:
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    return (
        (hsv[:, :, 0] >= DockStarScanner.YELLOW_HUE_MIN)
        & (hsv[:, :, 0] <= DockStarScanner.YELLOW_HUE_MAX)
        & (hsv[:, :, 1] >= saturation_min)
        & (hsv[:, :, 2] >= value_min)
    ).astype(np.uint8)


def _canonical_canvas() -> np.ndarray:
    roi = np.zeros(STAR_ROI_SHAPE, dtype=np.uint8)
    left, top, right, bottom = PATCH_AREA
    roi[top:bottom, left:right] = _load_patch_rgb()
    return roi


def test_v15_low_level_first_star_was_not_proven_by_old_thresholds() -> None:
    scanner = DockStarScanner()
    roi = _canonical_canvas()
    strict_mask = _first_mask(roi, saturation_min=100, value_min=200)
    matched = np.zeros(
        (
            roi.shape[0] - scanner.STAR_TEMPLATE_SIZE + 1,
            roi.shape[1] - scanner.STAR_TEMPLATE_SIZE + 1,
        ),
        dtype=np.float32,
    )

    assert scanner._first_filled_star(strict_mask, matched) is None


def test_v15_low_level_first_star_proves_five_star_layout_with_current_calibration() -> None:
    scanner = DockStarScanner()
    roi = _canonical_canvas()
    calibrated_mask = _first_mask(
        roi,
        saturation_min=scanner.FIRST_GLYPH_SATURATION_MIN,
        value_min=scanner.FIRST_GLYPH_VALUE_MIN,
    )
    matched = np.zeros(
        (
            roi.shape[0] - scanner.STAR_TEMPLATE_SIZE + 1,
            roi.shape[1] - scanner.STAR_TEMPLATE_SIZE + 1,
        ),
        dtype=np.float32,
    )

    first = scanner._first_filled_star(calibrated_mask, matched)

    assert first == (42, 15, 5)
