"""Read-only visual detectors для конкретных игровых настроек."""

from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np

from module.game_settings.assets import (
    TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_OFF_STATE,
    TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ON,
)
from module.game_settings.model import GameSettingState
from module.game_settings.traversal import OPTIONS_VIEWPORT_AREA


_CUSTOM_SHIP_NAMES_LABEL_MIN_SIMILARITY = 0.70
_CUSTOM_SHIP_NAMES_STATE_MIN_SIMILARITY = 0.70
_CUSTOM_SHIP_NAMES_STATE_MIN_MARGIN = 0.18

# Production visual evidence is cut only from confirmed real 1280x720
# screenshots. The ON row provides the state-independent text anchor. The
# OFF-state block is an exact crop containing both toggle choices in OFF state.
_CUSTOM_SHIP_NAMES_LABEL_AREA = (6, 5, 254, 38)
_CUSTOM_SHIP_NAMES_STATE_AREA = (275, 0, 440, 42)

# Geometry relative to the text-anchor match. Matching the label against a
# different state/background can move the best edge position by a few pixels,
# so state matching searches a small bounded neighborhood instead of assuming
# an exact row-top coordinate.
_CUSTOM_SHIP_NAMES_STATE_SEARCH = (260, -10, 443, 50)


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
    """Разрешить mutually-exclusive ON/OFF по реальным state assets."""

    off_matched = off_similarity >= _CUSTOM_SHIP_NAMES_STATE_MIN_SIMILARITY
    on_matched = on_similarity >= _CUSTOM_SHIP_NAMES_STATE_MIN_SIMILARITY

    # Neither-state и both-state одинаково недостоверны.
    if off_matched == on_matched:
        return GameSettingState.UNKNOWN

    if abs(off_similarity - on_similarity) < _CUSTOM_SHIP_NAMES_STATE_MIN_MARGIN:
        return GameSettingState.UNKNOWN

    if off_matched:
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


def _load_template(path: str, state: str) -> np.ndarray:
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(
            f"Не удалось загрузить {state} asset для Custom Ship Names."
        )
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


@lru_cache(maxsize=1)
def _load_custom_ship_names_on_template() -> np.ndarray:
    return _load_template(
        TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_ON.file,
        "ON",
    )


@lru_cache(maxsize=1)
def _load_custom_ship_names_off_state_template() -> np.ndarray:
    return _load_template(
        TEMPLATE_GAME_SETTINGS_CUSTOM_SHIP_NAMES_OFF_STATE.file,
        "OFF",
    )


def detect_custom_ship_names(
    image: np.ndarray,
) -> GameSettingState | None:
    """Определить Custom Ship Names без изменения настройки.

    ``None`` означает, что уникальная строка не присутствует в текущем
    viewport. ``UNKNOWN`` означает, что строка найдена, но состояние нельзя
    достоверно разрешить как mutually-exclusive ON/OFF.
    """

    if image.shape[:2] != (720, 1280):
        raise ValueError(
            "Custom Ship Names detector ожидает screenshot 1280 x 720."
        )

    on_template = _load_custom_ship_names_on_template()
    off_state_template = _load_custom_ship_names_off_state_template()

    vx1, vy1, vx2, vy2 = OPTIONS_VIEWPORT_AREA
    viewport = image[vy1:vy2, vx1:vx2]

    label_similarity, label_point = _template_score(
        viewport,
        _crop(on_template, _CUSTOM_SHIP_NAMES_LABEL_AREA),
    )
    if label_similarity < _CUSTOM_SHIP_NAMES_LABEL_MIN_SIMILARITY:
        return None

    label_origin = (
        vx1 + label_point[0],
        vy1 + label_point[1],
    )
    state_crop = _crop_checked(
        image,
        _relative_area(label_origin, _CUSTOM_SHIP_NAMES_STATE_SEARCH),
    )
    if state_crop is None:
        return GameSettingState.UNKNOWN

    on_state_template = _crop(on_template, _CUSTOM_SHIP_NAMES_STATE_AREA)
    off_similarity, _ = _template_score(state_crop, off_state_template)
    on_similarity, _ = _template_score(state_crop, on_state_template)

    return _resolve_custom_ship_names_state(
        off_similarity=off_similarity,
        on_similarity=on_similarity,
    )
