"""Shared image helpers for Game Settings detector paths."""

from __future__ import annotations

import cv2
import numpy as np


def crop_checked(
    image: np.ndarray,
    bounds: tuple[int, int, int, int],
) -> np.ndarray | None:
    """Return a bounded crop or ``None`` when the rectangle is invalid."""

    x1, y1, x2, y2 = bounds
    if x1 < 0 or y1 < 0 or x2 > image.shape[1] or y2 > image.shape[0]:
        return None
    if x1 >= x2 or y1 >= y2:
        return None
    return image[y1:y2, x1:x2]


def rgb_to_gray(image: np.ndarray) -> np.ndarray:
    """Convert repository RGB screenshots to grayscale without changing 2-D input."""

    if image.ndim == 3:
        return cv2.cvtColor(image[:, :, :3], cv2.COLOR_RGB2GRAY)
    return image
