"""Row-anchored OCR detectors for current EN Settings -> Options UI.

The public audit model exposes only typed values. OCR similarities and geometry
stay internal to this module. One OCR pass and one row grouping pass are cached
per stabilized viewport so all unresolved checks inspect the same screenshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TypeAlias

import cv2
import numpy as np

from module.game_settings.model import (
    FrameRateValue,
    GameSettingState,
    GameSettingValue,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.traversal import OPTIONS_VIEWPORT_AREA


_NATIVE_SHAPE = (720, 1280)
_ROW_MATCH_THRESHOLD = 0.78
_LABEL_PREFIX_MIN_SIMILARITY = 0.66
_OPTION_MATCH_THRESHOLD = 0.80
_ROW_CENTER_TOLERANCE = 20
_OPTION_GROUP_MAX_GAP = 18
_LABEL_PREFIX_MAX_BOXES = 8
_MARKER_X_GAP = 2
_MARKER_WIDTH = 30
_MARKER_HALF_HEIGHT = 15
_MARKER_ACTIVITY_THRESHOLD = 28
_MARKER_MIN_MARGIN = 0.035
_MARKER_MIN_ACTIVITY = 0.035
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class OcrTextBox:
    text: str
    bounds: tuple[int, int, int, int]
    score: float

    @property
    def center_y(self) -> float:
        return (self.bounds[1] + self.bounds[3]) / 2.0


@dataclass(frozen=True, slots=True)
class GameSettingOptionSpec:
    value: GameSettingValue
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GameSettingRowSpec:
    label_aliases: tuple[str, ...]
    options: tuple[GameSettingOptionSpec, ...]


@dataclass(frozen=True, slots=True)
class GameSettingOptionObservation:
    value: GameSettingValue
    bounds: tuple[int, int, int, int]
    click_bounds: tuple[int, int, int, int]
    marker_activity: float


@dataclass(frozen=True, slots=True)
class GameSettingRowObservation:
    value: GameSettingValue
    row_bounds: tuple[int, int, int, int]
    options: tuple[GameSettingOptionObservation, ...]

    def option_for(self, value: GameSettingValue) -> GameSettingOptionObservation | None:
        for option in self.options:
            if option.value is value:
                return option
        return None


@dataclass(frozen=True, slots=True)
class _TextCandidate:
    indices: tuple[int, ...]
    text: str
    bounds: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _MatchedRow:
    boxes: tuple[OcrTextBox, ...]
    label_box_count: int


OcrDetection: TypeAlias = tuple[str, list[list[float]], float]
RowGroups: TypeAlias = tuple[tuple[OcrTextBox, ...], ...]


def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub("", text.casefold())


def _label_similarity(text: str, alias: str) -> float:
    left = _normalize(text)
    right = _normalize(alias)
    if not left or not right:
        return 0.0
    if right in left:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _strict_similarity(text: str, alias: str) -> float:
    left = _normalize(text)
    right = _normalize(alias)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _bounds_from_box(box: list[list[float]]) -> tuple[int, int, int, int]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return (
        int(round(min(xs))),
        int(round(min(ys))),
        int(round(max(xs))),
        int(round(max(ys))),
    )


def _validate_frame(image: np.ndarray) -> None:
    if image.shape[:2] != _NATIVE_SHAPE:
        raise ValueError("Game Settings detector ожидает screenshot 1280 x 720.")


def _within_viewport(bounds: tuple[int, int, int, int]) -> bool:
    vx1, vy1, vx2, vy2 = OPTIONS_VIEWPORT_AREA
    x1, y1, x2, y2 = bounds
    center_x = (x1 + x2) / 2.0
    center_y = (y1 + y2) / 2.0
    return vx1 <= center_x <= vx2 and vy1 <= center_y <= vy2


def _convert_detections(detections: list[OcrDetection]) -> tuple[OcrTextBox, ...]:
    converted: list[OcrTextBox] = []
    for text, box, score in detections:
        bounds = _bounds_from_box(box)
        if not _within_viewport(bounds) or not text.strip():
            continue
        converted.append(OcrTextBox(text=text, bounds=bounds, score=float(score)))
    return tuple(converted)


def _same_line_groups(detections: tuple[OcrTextBox, ...]) -> RowGroups:
    ordered = sorted(detections, key=lambda item: (item.center_y, item.bounds[0]))
    groups: list[list[OcrTextBox]] = []
    for item in ordered:
        for group in groups:
            mean_y = sum(box.center_y for box in group) / len(group)
            if abs(item.center_y - mean_y) <= _ROW_CENTER_TOLERANCE:
                group.append(item)
                break
        else:
            groups.append([item])
    return tuple(
        tuple(sorted(group, key=lambda item: item.bounds[0]))
        for group in groups
    )


class _FrameOcrCache:
    """Keep one viewport frame, OCR result, groups and one reusable OCR engine."""

    def __init__(self) -> None:
        self._image: np.ndarray | None = None
        self._detections: tuple[OcrTextBox, ...] = ()
        self._groups: RowGroups = ()
        self._ocr = None

    def clear(self) -> None:
        self._image = None
        self._detections = ()
        self._groups = ()

    def get(self, image: np.ndarray) -> tuple[OcrTextBox, ...]:
        _validate_frame(image)
        if image is self._image:
            return self._detections

        if self._ocr is None:
            from module.ocr.al_ocr import AlOcr

            self._ocr = AlOcr(name="azur_lane")
        raw = self._ocr.det(image)
        detections = _convert_detections(raw)
        self._image = image
        self._detections = detections
        self._groups = _same_line_groups(detections)
        return detections

    def get_groups(self, image: np.ndarray) -> RowGroups:
        self.get(image)
        return self._groups


_FRAME_OCR_CACHE = _FrameOcrCache()


def clear_game_settings_ocr_cache() -> None:
    _FRAME_OCR_CACHE.clear()


def _group_text(group: tuple[OcrTextBox, ...]) -> str:
    return " ".join(item.text for item in group)


def _label_prefix_box_count(
    group: tuple[OcrTextBox, ...],
    aliases: tuple[str, ...],
) -> int:
    """Return the best left-prefix length occupied by the row label.

    This keeps option aliases such as ``Enable`` from matching an OCR token in
    the label ``Enable Idle Screen``. Only boxes after this prefix are eligible
    as option controls.
    """

    best_score = 0.0
    best_count = 0
    limit = min(len(group), _LABEL_PREFIX_MAX_BOXES)
    for count in range(1, limit + 1):
        text = _group_text(group[:count])
        score = max(_strict_similarity(text, alias) for alias in aliases)
        if score > best_score:
            best_score = score
            best_count = count
    if best_score < _LABEL_PREFIX_MIN_SIMILARITY:
        return 0
    return best_count


def _find_row_group(
    groups: RowGroups,
    aliases: tuple[str, ...],
) -> _MatchedRow | None:
    candidates: list[tuple[float, _MatchedRow]] = []
    for group in groups:
        text = _group_text(group)
        score = max(_label_similarity(text, alias) for alias in aliases)
        if score < _ROW_MATCH_THRESHOLD:
            continue
        label_box_count = _label_prefix_box_count(group, aliases)
        if label_box_count <= 0 or label_box_count >= len(group):
            continue
        candidates.append(
            (
                score,
                _MatchedRow(
                    boxes=group,
                    label_box_count=label_box_count,
                ),
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 0.04:
        return None
    return candidates[0][1]


def _union_bounds(boxes: tuple[OcrTextBox, ...]) -> tuple[int, int, int, int]:
    return (
        min(item.bounds[0] for item in boxes),
        min(item.bounds[1] for item in boxes),
        max(item.bounds[2] for item in boxes),
        max(item.bounds[3] for item in boxes),
    )


def _option_candidates(row: tuple[OcrTextBox, ...]) -> tuple[_TextCandidate, ...]:
    candidates: list[_TextCandidate] = []
    for index, current in enumerate(row):
        candidates.append(
            _TextCandidate(
                indices=(index,),
                text=current.text,
                bounds=current.bounds,
            )
        )
        if index + 1 >= len(row):
            continue
        following = row[index + 1]
        gap = following.bounds[0] - current.bounds[2]
        if gap > _OPTION_GROUP_MAX_GAP:
            continue
        group = (current, following)
        candidates.append(
            _TextCandidate(
                indices=(index, index + 1),
                text=_group_text(group),
                bounds=_union_bounds(group),
            )
        )
    return tuple(candidates)


def _option_specificity(option: GameSettingOptionSpec) -> int:
    return max(len(_normalize(alias)) for alias in option.aliases)


def _resolve_option_bounds(
    option_boxes: tuple[OcrTextBox, ...],
    options: tuple[GameSettingOptionSpec, ...],
) -> tuple[tuple[int, int, int, int], ...] | None:
    """Jointly assign option boxes without reuse or label-box candidates."""

    candidates = _option_candidates(option_boxes)
    used_indices: set[int] = set()
    assigned: dict[int, tuple[int, int, int, int]] = {}
    option_order = sorted(
        range(len(options)),
        key=lambda index: _option_specificity(options[index]),
        reverse=True,
    )

    for option_index in option_order:
        option = options[option_index]
        scored: list[tuple[float, _TextCandidate]] = []
        for candidate in candidates:
            if used_indices.intersection(candidate.indices):
                continue
            score = max(
                _strict_similarity(candidate.text, alias)
                for alias in option.aliases
            )
            if score >= _OPTION_MATCH_THRESHOLD:
                scored.append((score, candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 0.05:
            return None
        selected = scored[0][1]
        assigned[option_index] = selected.bounds
        used_indices.update(selected.indices)

    return tuple(assigned[index] for index in range(len(options)))


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


def _marker_activity(
    image: np.ndarray,
    option_bounds: tuple[int, int, int, int],
) -> float | None:
    _x1, y1, x2, y2 = option_bounds
    center_y = int(round((y1 + y2) / 2.0))
    marker_bounds = (
        x2 + _MARKER_X_GAP,
        center_y - _MARKER_HALF_HEIGHT,
        x2 + _MARKER_X_GAP + _MARKER_WIDTH,
        center_y + _MARKER_HALF_HEIGHT,
    )
    marker = _crop_checked(image, marker_bounds)
    if marker is None or marker.size == 0:
        return None
    if marker.ndim == 3:
        gray = cv2.cvtColor(marker[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        gray = marker

    border = np.concatenate(
        (gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1])
    )
    background = float(np.median(border))
    activity = np.abs(gray.astype(np.float32) - background)
    return float(np.mean(activity >= _MARKER_ACTIVITY_THRESHOLD))


def _click_bounds(
    option_bounds: tuple[int, int, int, int],
    row_bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int] | None:
    vx1, vy1, vx2, vy2 = OPTIONS_VIEWPORT_AREA
    x1, y1, x2, y2 = option_bounds
    rx1, ry1, rx2, ry2 = row_bounds
    left = max(vx1, rx1, x1 - 8)
    top = max(vy1, ry1 - 4, y1 - 8)
    right = min(vx2, rx2 + 40, x2 + _MARKER_WIDTH + 10)
    bottom = min(vy2, ry2 + 4, y2 + 8)
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def _unknown_for(spec: GameSettingRowSpec) -> GameSettingValue:
    return type(spec.options[0].value).UNKNOWN


def observe_game_setting_row(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    *,
    detections: tuple[OcrTextBox, ...] | None = None,
) -> GameSettingRowObservation | None:
    """Locate one unique row and resolve its selected option fail-closed."""

    _validate_frame(image)
    if not spec.options:
        raise ValueError("GameSettingRowSpec должен содержать options")
    value_type = type(spec.options[0].value)
    if any(type(option.value) is not value_type for option in spec.options):
        raise TypeError("Все options одной строки должны принадлежать одной value family")

    if detections is None:
        groups = _FRAME_OCR_CACHE.get_groups(image)
    else:
        groups = _same_line_groups(detections)
    matched = _find_row_group(groups, spec.label_aliases)
    if matched is None:
        return None

    row = matched.boxes
    row_bounds = _union_bounds(row)
    option_boxes = row[matched.label_box_count :]
    option_bounds = _resolve_option_bounds(option_boxes, spec.options)
    if option_bounds is None:
        return GameSettingRowObservation(
            value=_unknown_for(spec),
            row_bounds=row_bounds,
            options=(),
        )

    observations: list[GameSettingOptionObservation] = []
    for option, bounds in zip(spec.options, option_bounds, strict=True):
        activity = _marker_activity(image, bounds)
        click = _click_bounds(bounds, row_bounds)
        if activity is None or click is None:
            return GameSettingRowObservation(
                value=_unknown_for(spec),
                row_bounds=row_bounds,
                options=(),
            )
        observations.append(
            GameSettingOptionObservation(
                value=option.value,
                bounds=bounds,
                click_bounds=click,
                marker_activity=activity,
            )
        )

    ordered = sorted(observations, key=lambda item: item.marker_activity, reverse=True)
    if ordered[0].marker_activity < _MARKER_MIN_ACTIVITY:
        selected: GameSettingValue = _unknown_for(spec)
    elif (
        len(ordered) > 1
        and ordered[0].marker_activity - ordered[1].marker_activity < _MARKER_MIN_MARGIN
    ):
        selected = _unknown_for(spec)
    else:
        selected = ordered[0].value

    return GameSettingRowObservation(
        value=selected,
        row_bounds=row_bounds,
        options=tuple(observations),
    )


def detect_game_setting_row(
    image: np.ndarray,
    spec: GameSettingRowSpec,
) -> GameSettingValue | None:
    observation = observe_game_setting_row(image, spec)
    if observation is None:
        return None
    return observation.value


TOGGLE_OFF_ON = (
    GameSettingOptionSpec(GameSettingState.OFF, ("Off", "Disabled", "Disable")),
    GameSettingOptionSpec(GameSettingState.ON, ("On", "Enabled", "Enable")),
)

FRAME_RATE_ROW = GameSettingRowSpec(
    label_aliases=("Frame Rate Settings", "Frame Rate"),
    options=(
        GameSettingOptionSpec(FrameRateValue.FPS_30, ("30 FPS", "30FPS")),
        GameSettingOptionSpec(FrameRateValue.FPS_60, ("60 FPS", "60FPS")),
    ),
)
OPSI_REDUCE_TB_GUIDANCE_ROW = GameSettingRowSpec(
    label_aliases=("Reduce TB Guidance", "OpSi Reduce TB Guidance"),
    options=TOGGLE_OFF_ON,
)
OPSI_AUTO_USE_ITEMS_ROW = GameSettingRowSpec(
    label_aliases=(
        "Auto use items during Auto Mode",
        "Auto-submit items during auto mode",
        "Auto use items",
    ),
    options=TOGGLE_OFF_ON,
)
OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW = GameSettingRowSpec(
    label_aliases=(
        "Default to Auto Mode in Threat Safe",
        "Auto mode default on in safe seas",
        "Threat Safe",
    ),
    options=TOGGLE_OFF_ON,
)
STORY_AUTOPLAY_ROW = GameSettingRowSpec(
    label_aliases=("Story Autoplay", "Story auto-play"),
    options=(
        GameSettingOptionSpec(
            StoryAutoplayValue.DISABLED,
            ("Disable", "Disabled", "Off"),
        ),
        GameSettingOptionSpec(
            StoryAutoplayValue.ENABLED,
            ("Enable", "Enabled", "On"),
        ),
    ),
)
TEXT_AUTO_SCROLL_SPEED_ROW = GameSettingRowSpec(
    label_aliases=("Text Auto-Scroll Speed", "Story auto-play speed"),
    options=(
        GameSettingOptionSpec(TextAutoScrollSpeedValue.SLOW, ("Slow",)),
        GameSettingOptionSpec(TextAutoScrollSpeedValue.NORMAL, ("Normal",)),
        GameSettingOptionSpec(TextAutoScrollSpeedValue.FAST, ("Fast",)),
        GameSettingOptionSpec(
            TextAutoScrollSpeedValue.VERY_FAST,
            ("Very Fast", "Extra Fast"),
        ),
    ),
)
ENABLE_IDLE_SCREEN_ROW = GameSettingRowSpec(
    label_aliases=("Enable Idle Screen", "Enable Idle Mode"),
    options=TOGGLE_OFF_ON,
)
DUPLICATE_SHIP_DISPLAY_ROW = GameSettingRowSpec(
    label_aliases=("Duplicate Ship Display", "Duplicate character notification"),
    options=TOGGLE_OFF_ON,
)
DISPLAY_QUICK_SWITCH_PROMPT_ROW = GameSettingRowSpec(
    label_aliases=(
        "Display Quick-Switch Prompt",
        "Quick-change second confirmation dialog",
    ),
    options=TOGGLE_OFF_ON,
)
DISPLAY_BATTLE_RESULT_CUTSCENE_ROW = GameSettingRowSpec(
    label_aliases=("Display Battle Result Cutscene", "Show settlement characters"),
    options=TOGGLE_OFF_ON,
)
CUSTOM_SHIP_NAMES_ROW = GameSettingRowSpec(
    label_aliases=("Custom Ship Names",),
    options=TOGGLE_OFF_ON,
)


ROW_SPECS_BY_KEY: dict[str, GameSettingRowSpec] = {
    "frame_rate": FRAME_RATE_ROW,
    "opsi_reduce_tb_guidance": OPSI_REDUCE_TB_GUIDANCE_ROW,
    "opsi_auto_use_items": OPSI_AUTO_USE_ITEMS_ROW,
    "opsi_default_auto_mode_threat_safe": OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW,
    "story_autoplay": STORY_AUTOPLAY_ROW,
    "text_auto_scroll_speed": TEXT_AUTO_SCROLL_SPEED_ROW,
    "enable_idle_screen": ENABLE_IDLE_SCREEN_ROW,
    "duplicate_ship_display": DUPLICATE_SHIP_DISPLAY_ROW,
    "display_quick_switch_prompt": DISPLAY_QUICK_SWITCH_PROMPT_ROW,
    "display_battle_result_cutscene": DISPLAY_BATTLE_RESULT_CUTSCENE_ROW,
    "custom_ship_names": CUSTOM_SHIP_NAMES_ROW,
}
