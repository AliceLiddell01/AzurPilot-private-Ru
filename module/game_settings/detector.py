"""Read-only visual detectors для конкретных игровых настроек."""

from __future__ import annotations

import cv2
import numpy as np

from module.game_settings.assets import (
    TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ROW,
)
from module.game_settings.model import GameSettingState
from module.game_settings.traversal import OPTIONS_VIEWPORT_AREA


_CUSTOM_SHIP_NAMES_LABEL_MIN_SIMILARITY = 0.70
_CUSTOM_SHIP_NAMES_MARKER_MIN_SIMILARITY = 0.70
_CUSTOM_SHIP_NAMES_MARKER_MIN_MARGIN = 0.18

# Единственный production asset — exact real crop строки Custom Ship Names.
# Эти две области являются его неизменёнными sub-crops.
_CUSTOM_SHIP_NAMES_LABEL_AREA = (6, 5, 254, 38)
_CUSTOM_SHIP_NAMES_SELECTED_MARKER_AREA = (402, 4, 434, 38)

# Геометрия относительно top-left label после его нахождения в viewport.
# Reference frame: 1280x720 user screenshot, label origin=(232, 495).
_CUSTOM_SHIP_NAMES_OFF_SEARCH = (296, -5, 337, 37)
_CUSTOM_SHIP_NAMES_ON_SEARCH = (391, -5, 433, 37)


def _edges(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        gray = image
    else:
        gray = cv2.cvtColor(image[:, :, :3], cv2.COLOR_RGB2GRAY)
    return cv2.Canny(gray, 80, 160)


def _template_score(
    image: np.ndarray,
    template: np.ndarray,
) -> tuple[float, tuple[int, int]]:
    if (
        image.shape[0] < template.shape[0]
        or image.shape[1] < template.shape[1]
    ):
        return 0.0, (0, 0)

    result = cv2.matchTemplate(
        _edges(image),
        _edges(template),
        cv2.TM_CCOEFF_NORMED,
    )
    _minimum, maximum, _minimum_point, maximum_point = cv2.minMaxLoc(result)
    return float(maximum), maximum_point


def _resolve_custom_ship_names_state(
    off_similarity: float,
    on_similarity: float,
) -> GameSettingState:
    best = max(off_similarity, on_similarity)
    if best < _CUSTOM_SHIP_NAMES_MARKER_MIN_SIMILARITY:
        return GameSettingState.UNKNOWN
    if abs(off_similarity - on_similarity) < _CUSTOM_SHIP_NAMES_MARKER_MIN_MARGIN:
        return GameSettingState.UNKNOWN
    if off_similarity > on_similarity:
        return GameSettingState.OFF
    return GameSettingState.ON


def _crop(
    image: np.ndarray,
    area: tuple[int, int, int, int],
) -> np.ndarray:
    x1, y1, x2, y2 = area
    return image[y1:y2, x1:x2]


def _relative_area(
    origin: tuple[int, int],
    relative: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    x, y = origin
    x1, y1, x2, y2 = relative
    return x + x1, y + y1, x + x2, y + y2


def _crop_checked(
    image: np.ndarray,
    area: tuple[int, int, int, int],
) -> np.ndarray | None:
    x1, y1, x2, y2 = area
    if x1 < 0 or y1 < 0 or x2 > image.shape[1] or y2 > image.shape[0]:
        return None
    if x1 >= x2 or y1 >= y2:
        return None
    return image[y1:y2, x1:x2]


def detect_custom_ship_names(
    image: np.ndarray,
) -> GameSettingState | None:
    """Определить Custom Ship Names без изменения настройки.

    ``None`` означает, что уникальная строка не присутствует в текущем
    viewport. ``UNKNOWN`` означает, что строка найдена, но selected-marker
    нельзя надёжно отнести к Off или On.

    Production asset — один exact real crop именно строки Custom Ship Names.
    Из него без синтеза берутся уникальный label и selected-marker текущего
    On-slot. После нахождения label тот же marker ищется только в двух штатных
    slot-областях этой же строки. Другие настройки не используются как
    substitute/reference для отсутствующего состояния.
    """

    if image.shape[:2] != (720, 1280):
        raise ValueError(
            "Custom Ship Names detector ожидает screenshot 1280 x 720."
        )

    row_template = TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ROW.image
    label_template = _crop(row_template, _CUSTOM_SHIP_NAMES_LABEL_AREA)
    marker_template = _crop(
        row_template,
        _CUSTOM_SHIP_NAMES_SELECTED_MARKER_AREA,
    )

    vx1, vy1, vx2, vy2 = OPTIONS_VIEWPORT_AREA
    viewport = image[vy1:vy2, vx1:vx2]
    label_similarity, label_point = _template_score(viewport, label_template)
    if label_similarity < _CUSTOM_SHIP_NAMES_LABEL_MIN_SIMILARITY:
        return None

    label_origin = (
        vx1 + label_point[0],
        vy1 + label_point[1],
    )
    off_crop = _crop_checked(
        image,
        _relative_area(label_origin, _CUSTOM_SHIP_NAMES_OFF_SEARCH),
    )
    on_crop = _crop_checked(
        image,
        _relative_area(label_origin, _CUSTOM_SHIP_NAMES_ON_SEARCH),
    )
    if off_crop is None or on_crop is None:
        return GameSettingState.UNKNOWN

    off_similarity, _ = _template_score(off_crop, marker_template)
    on_similarity, _ = _template_score(on_crop, marker_template)

    return _resolve_custom_ship_names_state(
        off_similarity=off_similarity,
        on_similarity=on_similarity,
    )
