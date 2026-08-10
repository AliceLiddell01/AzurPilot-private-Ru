"""Asset-driven selected/unselected state classification for Options controls."""

from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np

from module.game_settings.assets import (
    TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED,
    TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED,
)
from module.game_settings.model import GameSettingValue
from module.game_settings.options_detector import (
    GameSettingOptionObservation,
    GameSettingRowObservation,
    GameSettingRowSpec,
    OcrTextBox,
    observe_game_setting_row,
)


_CONTROL_TEMPLATE_MIN_SCORE = 0.42
_CONTROL_TEMPLATE_MIN_MARGIN = 0.15
_CONTROL_SEARCH_HALF_SIZE = 22
_CONTROL_EDGE_LOW = 80
_CONTROL_EDGE_HIGH = 160


@lru_cache(maxsize=2)
def _load_control_template(path: str) -> np.ndarray:
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Не удалось загрузить Game Settings control asset: {path}")
    return cv2.Canny(image, _CONTROL_EDGE_LOW, _CONTROL_EDGE_HIGH)


def _crop_checked(
    image: np.ndarray,
    bounds: tuple[int, int, int, int],
) -> np.ndarray | None:
    x1, y1, x2, y2 = bounds
    if x1 < 0 or y1 < 0 or x2 > image.shape[1] or y2 > image.shape[0]:
        return None
    if x1 >= x2 or y1 >= y2:
        return None
    return image[y1:y2, x1:x2]


def _control_search_bounds(
    click_bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = click_bounds
    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0
    return (
        int(round(center_x - _CONTROL_SEARCH_HALF_SIZE)),
        int(round(center_y - _CONTROL_SEARCH_HALF_SIZE)),
        int(round(center_x + _CONTROL_SEARCH_HALF_SIZE)),
        int(round(center_y + _CONTROL_SEARCH_HALF_SIZE)),
    )


def _template_score(search: np.ndarray, template: np.ndarray) -> float:
    if search.ndim == 3:
        gray = cv2.cvtColor(search[:, :, :3], cv2.COLOR_RGB2GRAY)
    else:
        gray = search
    edges = cv2.Canny(gray, _CONTROL_EDGE_LOW, _CONTROL_EDGE_HIGH)
    if edges.shape[0] < template.shape[0] or edges.shape[1] < template.shape[1]:
        return 0.0
    result = cv2.matchTemplate(edges, template, cv2.TM_CCOEFF_NORMED)
    return float(np.max(result))


def control_selection_confidence(
    image: np.ndarray,
    click_bounds: tuple[int, int, int, int],
) -> float | None:
    """Return signed selected-state confidence for one known control marker.

    Positive means the selected asset wins, negative means the unselected asset
    wins, and ``None`` means the visual evidence is not strong enough.
    """

    search = _crop_checked(image, _control_search_bounds(click_bounds))
    if search is None or search.size == 0:
        return None

    selected = _load_control_template(TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED.file)
    unselected = _load_control_template(TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED.file)
    selected_score = _template_score(search, selected)
    unselected_score = _template_score(search, unselected)
    if max(selected_score, unselected_score) < _CONTROL_TEMPLATE_MIN_SCORE:
        return None

    confidence = selected_score - unselected_score
    if abs(confidence) < _CONTROL_TEMPLATE_MIN_MARGIN:
        return None
    return confidence


def _unknown_for(spec: GameSettingRowSpec) -> GameSettingValue:
    return type(spec.options[0].value).UNKNOWN


def _reclassify_observation(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    observation: GameSettingRowObservation,
) -> GameSettingRowObservation:
    rebuilt: list[GameSettingOptionObservation] = []
    selected_values: list[GameSettingValue] = []
    all_classified = bool(observation.options)

    for option in observation.options:
        confidence = control_selection_confidence(image, option.click_bounds)
        if confidence is None:
            all_classified = False
            signed_confidence = 0.0
        else:
            signed_confidence = confidence
            if confidence > 0.0:
                selected_values.append(option.value)
        rebuilt.append(
            GameSettingOptionObservation(
                value=option.value,
                bounds=option.bounds,
                click_bounds=option.click_bounds,
                marker_activity=signed_confidence,
            )
        )

    value = _unknown_for(spec)
    if all_classified and len(selected_values) == 1:
        value = selected_values[0]

    return GameSettingRowObservation(
        value=value,
        row_bounds=observation.row_bounds,
        options=tuple(rebuilt),
    )


def observe_game_setting_row_with_control_assets(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    *,
    detections: tuple[OcrTextBox, ...] | None = None,
) -> GameSettingRowObservation | None:
    """Use OCR only for row/option geometry and assets as state authority."""

    geometry = observe_game_setting_row(image, spec, detections=detections)
    if geometry is None:
        return None
    return _reclassify_observation(image, spec, geometry)


def detect_game_setting_row_with_control_assets(
    image: np.ndarray,
    spec: GameSettingRowSpec,
) -> GameSettingValue | None:
    observation = observe_game_setting_row_with_control_assets(image, spec)
    if observation is None:
        return None
    return observation.value
