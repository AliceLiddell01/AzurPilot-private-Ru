"""Asset-driven selected/unselected state classification for Options controls."""

from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np

from module.game_settings.assets import (
    TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED,
    TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED,
)
from module.game_settings.image_utils import crop_checked, rgb_to_gray
from module.game_settings.model import GameSettingValue
from module.game_settings.options_detector import (
    GameSettingOptionObservation,
    GameSettingRowObservation,
    GameSettingRowSpec,
    OcrTextBox,
    observe_game_setting_row,
)


_CONTROL_TEMPLATE_MIN_SCORE = 0.42
_CONTROL_SELECTED_MIN_MARGIN = 0.15
_CONTROL_UNSELECTED_MIN_MARGIN = 0.10
_CONTROL_RAW_MIN_SCORE = 0.65
_CONTROL_RAW_MIN_MARGIN = 0.20
_CONTROL_SEARCH_HALF_SIZE = 22
_CONTROL_EDGE_LOW = 80
_CONTROL_EDGE_HIGH = 160


@lru_cache(maxsize=2)
def _load_control_template(path: str) -> tuple[np.ndarray, np.ndarray]:
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Не удалось загрузить Game Settings control asset: {path}")
    return image, cv2.Canny(image, _CONTROL_EDGE_LOW, _CONTROL_EDGE_HIGH)


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


def _match_template(search: np.ndarray, template: np.ndarray) -> float:
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return 0.0
    result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    return float(np.max(result))


def _classify_score_pair(
    selected_score: float,
    unselected_score: float,
    *,
    min_score: float,
    selected_min_margin: float,
    unselected_min_margin: float,
) -> float | None:
    if max(selected_score, unselected_score) < min_score:
        return None

    confidence = selected_score - unselected_score
    if confidence > 0.0:
        if confidence < selected_min_margin:
            return None
    elif confidence < 0.0:
        if -confidence < unselected_min_margin:
            return None
    else:
        return None
    return confidence


def _classify_control_scores(
    selected_score: float,
    unselected_score: float,
) -> float | None:
    """Classify edge-template scores with asymmetric fail-closed margins."""

    return _classify_score_pair(
        selected_score,
        unselected_score,
        min_score=_CONTROL_TEMPLATE_MIN_SCORE,
        selected_min_margin=_CONTROL_SELECTED_MIN_MARGIN,
        unselected_min_margin=_CONTROL_UNSELECTED_MIN_MARGIN,
    )


def _classify_raw_control_scores(
    selected_score: float,
    unselected_score: float,
) -> float | None:
    """Accept only strong raw-template evidence before falling back to edges."""

    return _classify_score_pair(
        selected_score,
        unselected_score,
        min_score=_CONTROL_RAW_MIN_SCORE,
        selected_min_margin=_CONTROL_RAW_MIN_MARGIN,
        unselected_min_margin=_CONTROL_RAW_MIN_MARGIN,
    )


def control_selection_confidence(
    image: np.ndarray,
    click_bounds: tuple[int, int, int, int],
) -> float | None:
    """Return signed selected-state confidence for one known control marker.

    Positive means the selected asset wins, negative means the unselected asset
    wins, and ``None`` means the visual evidence is not strong enough.

    Raw grayscale template matching is used first because the real selected and
    unselected diamonds differ strongly in glow/fill while sharing many edges.
    Edge matching remains a fail-closed fallback for frames where background
    variation weakens the raw-template correlation.
    """

    search = crop_checked(image, _control_search_bounds(click_bounds))
    if search is None or search.size == 0:
        return None
    gray = rgb_to_gray(search)

    selected_raw, selected_edges = _load_control_template(
        TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED.file
    )
    unselected_raw, unselected_edges = _load_control_template(
        TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED.file
    )

    raw_confidence = _classify_raw_control_scores(
        _match_template(gray, selected_raw),
        _match_template(gray, unselected_raw),
    )
    if raw_confidence is not None:
        return raw_confidence

    edges = cv2.Canny(gray, _CONTROL_EDGE_LOW, _CONTROL_EDGE_HIGH)
    return _classify_control_scores(
        _match_template(edges, selected_edges),
        _match_template(edges, unselected_edges),
    )


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
            activity = 0.0
        else:
            activity = abs(confidence)
            if confidence > 0.0:
                selected_values.append(option.value)
        rebuilt.append(
            GameSettingOptionObservation(
                value=option.value,
                bounds=option.bounds,
                click_bounds=option.click_bounds,
                marker_activity=activity,
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
