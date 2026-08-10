"""Row-anchored detectors for the current EN Settings -> Options UI.

The current 1280 x 720 EN page contains two different control layouts:

* two independent toggle cards may share the same visual Y coordinate;
* full-width choice sections place the selection diamond to the left of the
  option text and may arrange options on one or two rows.

OCR is used to anchor labels/options. Selection state is read from the actual
selection diamonds in their structural card positions so marquee text and OCR
boxes that merge a label with ``Off`` do not make the value ambiguous.
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
_OPTION_MATCH_THRESHOLD = 0.80
_ROW_CENTER_TOLERANCE = 20
_OPTION_GROUP_MAX_GAP = 18
_LABEL_GROUP_MAX_GAP = 28
_LABEL_MAX_BOXES = 3
_MARKER_ACTIVITY_THRESHOLD = 28
_MARKER_MIN_MARGIN = 0.035
_MARKER_MIN_ACTIVITY = 0.035
_MARKER_HALF_HEIGHT = 18
_MARKER_HALF_WIDTH = 18
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

ROW_LAYOUT_TOGGLE_COLUMNS = "toggle_columns"
ROW_LAYOUT_CHOICE_CARDS = "choice_cards"

# Native 1280 x 720 geometry measured from the current EN Options cards.
_TOGGLE_LEFT_OFF_X = 548
_TOGGLE_LEFT_ON_X = 643
_TOGGLE_RIGHT_OFF_X = 1038
_TOGGLE_RIGHT_ON_X = 1133
_CHOICE_LEFT_MARKER_X = 249
_CHOICE_RIGHT_MARKER_X = 738
_PANEL_SPLIT_X = 691
_LEFT_PANEL_BOUNDS = (214, 679)
_RIGHT_PANEL_BOUNDS = (703, 1169)
_CHOICE_OPTION_MIN_DY = 10
_CHOICE_OPTION_MAX_DY = 190


@dataclass(frozen=True, slots=True)
class OcrTextBox:
    text: str
    bounds: tuple[int, int, int, int]
    score: float

    @property
    def center_x(self) -> float:
        return (self.bounds[0] + self.bounds[2]) / 2.0

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
    layout: str = ROW_LAYOUT_TOGGLE_COLUMNS

    def __post_init__(self) -> None:
        if not self.label_aliases:
            raise ValueError("GameSettingRowSpec должен содержать label_aliases")
        if not self.options:
            raise ValueError("GameSettingRowSpec должен содержать options")
        if self.layout not in (ROW_LAYOUT_TOGGLE_COLUMNS, ROW_LAYOUT_CHOICE_CARDS):
            raise ValueError(f"Неподдерживаемый layout Game Setting: {self.layout!r}")


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
    source_bounds: tuple[tuple[int, int, int, int], ...] = ()

    @property
    def center_x(self) -> float:
        return (self.bounds[0] + self.bounds[2]) / 2.0

    @property
    def center_y(self) -> float:
        return (self.bounds[1] + self.bounds[3]) / 2.0


@dataclass(frozen=True, slots=True)
class _MatchedLabel:
    text: str
    bounds: tuple[int, int, int, int]
    score: float

    @property
    def center_x(self) -> float:
        return (self.bounds[0] + self.bounds[2]) / 2.0

    @property
    def center_y(self) -> float:
        return (self.bounds[1] + self.bounds[3]) / 2.0


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


def _union_bounds_from_boxes(
    boxes: tuple[OcrTextBox, ...],
) -> tuple[int, int, int, int]:
    return (
        min(item.bounds[0] for item in boxes),
        min(item.bounds[1] for item in boxes),
        max(item.bounds[2] for item in boxes),
        max(item.bounds[3] for item in boxes),
    )


def _union_rects(
    rects: tuple[tuple[int, int, int, int], ...],
) -> tuple[int, int, int, int]:
    return (
        min(item[0] for item in rects),
        min(item[1] for item in rects),
        max(item[2] for item in rects),
        max(item[3] for item in rects),
    )


def _option_candidates(row: tuple[OcrTextBox, ...]) -> tuple[_TextCandidate, ...]:
    candidates: list[_TextCandidate] = []
    for index, current in enumerate(row):
        candidates.append(
            _TextCandidate(
                indices=(index,),
                text=current.text,
                bounds=current.bounds,
                source_bounds=(current.bounds,),
            )
        )
        if index + 1 >= len(row):
            continue
        following = row[index + 1]
        gap = following.bounds[0] - current.bounds[2]
        if gap > _OPTION_GROUP_MAX_GAP:
            continue
        pair = (current, following)
        candidates.append(
            _TextCandidate(
                indices=(index, index + 1),
                text=_group_text(pair),
                bounds=_union_bounds_from_boxes(pair),
                source_bounds=(current.bounds, following.bounds),
            )
        )
    return tuple(candidates)


def _label_candidates(groups: RowGroups) -> tuple[_TextCandidate, ...]:
    candidates: list[_TextCandidate] = []
    for group in groups:
        for start in range(len(group)):
            for count in range(1, _LABEL_MAX_BOXES + 1):
                end = start + count
                if end > len(group):
                    break
                span = group[start:end]
                if count > 1:
                    gaps = [
                        span[index + 1].bounds[0] - span[index].bounds[2]
                        for index in range(len(span) - 1)
                    ]
                    if any(gap > _LABEL_GROUP_MAX_GAP for gap in gaps):
                        break
                candidates.append(
                    _TextCandidate(
                        indices=tuple(range(start, end)),
                        text=_group_text(span),
                        bounds=_union_bounds_from_boxes(span),
                    )
                )
    return tuple(candidates)


def _find_label(
    groups: RowGroups,
    aliases: tuple[str, ...],
) -> _MatchedLabel | None:
    scored: list[tuple[float, _TextCandidate]] = []
    for candidate in _label_candidates(groups):
        score = max(_label_similarity(candidate.text, alias) for alias in aliases)
        if score >= _ROW_MATCH_THRESHOLD:
            scored.append((score, candidate))
    if not scored:
        return None

    scored.sort(
        key=lambda item: (item[0], len(_normalize(item[1].text))),
        reverse=True,
    )
    best_score, best = scored[0]
    for other_score, other in scored[1:]:
        if best_score - other_score >= 0.04:
            break
        same_region = (
            abs(best.center_y - other.center_y) <= _ROW_CENTER_TOLERANCE
            and abs(best.center_x - other.center_x) <= 180
        )
        if not same_region:
            return None
    return _MatchedLabel(text=best.text, bounds=best.bounds, score=best_score)


def _option_specificity(option: GameSettingOptionSpec) -> int:
    return max(len(_normalize(alias)) for alias in option.aliases)


def _resolve_option_candidates(
    candidates: tuple[_TextCandidate, ...],
    options: tuple[GameSettingOptionSpec, ...],
) -> tuple[tuple[int, int, int, int], ...] | None:
    used_sources: set[tuple[int, int, int, int]] = set()
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
            source_bounds = candidate.source_bounds or (candidate.bounds,)
            if used_sources.intersection(source_bounds):
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
        used_sources.update(selected.source_bounds or (selected.bounds,))

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


def _marker_activity_from_bounds(
    image: np.ndarray,
    marker_bounds: tuple[int, int, int, int],
) -> float | None:
    marker = _crop_checked(image, marker_bounds)
    if marker is None or marker.size == 0:
        return None
    if marker.ndim == 3:
        gray = cv2.cvtColor(marker[:, :, :3], cv2.COLOR_RGB2GRAY)
    else:
        gray = marker

    border = np.concatenate(
        (gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1])
    )
    background = float(np.median(border))
    activity = np.abs(gray.astype(np.float32) - background)
    return float(np.mean(activity >= _MARKER_ACTIVITY_THRESHOLD))


def _centered_marker_bounds(
    center_x: float,
    center_y: float,
) -> tuple[int, int, int, int]:
    return (
        int(round(center_x - _MARKER_HALF_WIDTH)),
        int(round(center_y - _MARKER_HALF_HEIGHT)),
        int(round(center_x + _MARKER_HALF_WIDTH)),
        int(round(center_y + _MARKER_HALF_HEIGHT)),
    )


def _toggle_marker_bounds(
    value: GameSettingValue,
    *,
    panel: str,
    center_y: float,
) -> tuple[int, int, int, int]:
    if value is GameSettingState.OFF:
        center_x = _TOGGLE_LEFT_OFF_X if panel == "left" else _TOGGLE_RIGHT_OFF_X
    elif value is GameSettingState.ON:
        center_x = _TOGGLE_LEFT_ON_X if panel == "left" else _TOGGLE_RIGHT_ON_X
    else:
        raise ValueError("Toggle marker поддерживает только ON/OFF")
    return _centered_marker_bounds(center_x, center_y)


def _choice_marker_bounds(
    option_bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = option_bounds
    center_x = (
        _CHOICE_LEFT_MARKER_X
        if (x1 + x2) / 2.0 < _PANEL_SPLIT_X
        else _CHOICE_RIGHT_MARKER_X
    )
    center_y = (y1 + y2) / 2.0
    return _centered_marker_bounds(center_x, center_y)


def _click_bounds_from_marker(
    marker_bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    vx1, vy1, vx2, vy2 = OPTIONS_VIEWPORT_AREA
    x1, y1, x2, y2 = marker_bounds
    return (
        max(vx1, x1 - 6),
        max(vy1, y1 - 6),
        min(vx2, x2 + 6),
        min(vy2, y2 + 6),
    )


def _unknown_for(spec: GameSettingRowSpec) -> GameSettingValue:
    return type(spec.options[0].value).UNKNOWN


def _select_from_observations(
    spec: GameSettingRowSpec,
    observations: list[GameSettingOptionObservation],
) -> GameSettingValue:
    ordered = sorted(observations, key=lambda item: item.marker_activity, reverse=True)
    if not ordered or ordered[0].marker_activity < _MARKER_MIN_ACTIVITY:
        return _unknown_for(spec)
    if (
        len(ordered) > 1
        and ordered[0].marker_activity - ordered[1].marker_activity < _MARKER_MIN_MARGIN
    ):
        return _unknown_for(spec)
    return ordered[0].value


def _observe_toggle_columns(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    groups: RowGroups,
) -> GameSettingRowObservation | None:
    if len(spec.options) != 2 or any(
        type(option.value) is not GameSettingState for option in spec.options
    ):
        raise TypeError("toggle_columns ожидает ровно ON/OFF options")

    label = _find_label(groups, spec.label_aliases)
    if label is None:
        return None
    panel = "left" if label.center_x < _PANEL_SPLIT_X else "right"
    panel_x1, panel_x2 = (
        _LEFT_PANEL_BOUNDS if panel == "left" else _RIGHT_PANEL_BOUNDS
    )
    row_bounds = (
        panel_x1,
        max(OPTIONS_VIEWPORT_AREA[1], int(round(label.center_y - 30))),
        panel_x2,
        min(OPTIONS_VIEWPORT_AREA[3], int(round(label.center_y + 30))),
    )

    observations: list[GameSettingOptionObservation] = []
    for option in spec.options:
        marker_bounds = _toggle_marker_bounds(
            option.value,
            panel=panel,
            center_y=label.center_y,
        )
        activity = _marker_activity_from_bounds(image, marker_bounds)
        if activity is None:
            return GameSettingRowObservation(
                value=_unknown_for(spec),
                row_bounds=row_bounds,
                options=(),
            )
        observations.append(
            GameSettingOptionObservation(
                value=option.value,
                bounds=marker_bounds,
                click_bounds=_click_bounds_from_marker(marker_bounds),
                marker_activity=activity,
            )
        )

    return GameSettingRowObservation(
        value=_select_from_observations(spec, observations),
        row_bounds=row_bounds,
        options=tuple(observations),
    )


def _choice_candidates_below_label(
    groups: RowGroups,
    label: _MatchedLabel,
) -> tuple[_TextCandidate, ...]:
    lower = label.bounds[3] + _CHOICE_OPTION_MIN_DY
    upper = label.bounds[3] + _CHOICE_OPTION_MAX_DY
    candidates: list[_TextCandidate] = []
    for group in groups:
        in_band = tuple(
            item
            for item in group
            if lower <= item.center_y <= upper
        )
        if in_band:
            candidates.extend(_option_candidates(in_band))
    return tuple(candidates)


def _observe_choice_cards(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    groups: RowGroups,
) -> GameSettingRowObservation | None:
    label = _find_label(groups, spec.label_aliases)
    if label is None:
        return None

    candidates = _choice_candidates_below_label(groups, label)
    option_bounds = _resolve_option_candidates(candidates, spec.options)
    if option_bounds is None:
        row_bounds = (
            OPTIONS_VIEWPORT_AREA[0],
            max(OPTIONS_VIEWPORT_AREA[1], label.bounds[1] - 8),
            OPTIONS_VIEWPORT_AREA[2],
            min(
                OPTIONS_VIEWPORT_AREA[3],
                label.bounds[3] + _CHOICE_OPTION_MAX_DY,
            ),
        )
        return GameSettingRowObservation(
            value=_unknown_for(spec),
            row_bounds=row_bounds,
            options=(),
        )

    observations: list[GameSettingOptionObservation] = []
    marker_rects: list[tuple[int, int, int, int]] = []
    for option, bounds in zip(spec.options, option_bounds, strict=True):
        marker_bounds = _choice_marker_bounds(bounds)
        marker_rects.append(marker_bounds)
        activity = _marker_activity_from_bounds(image, marker_bounds)
        if activity is None:
            return GameSettingRowObservation(
                value=_unknown_for(spec),
                row_bounds=(
                    OPTIONS_VIEWPORT_AREA[0],
                    label.bounds[1],
                    OPTIONS_VIEWPORT_AREA[2],
                    min(
                        OPTIONS_VIEWPORT_AREA[3],
                        label.bounds[3] + _CHOICE_OPTION_MAX_DY,
                    ),
                ),
                options=(),
            )
        observations.append(
            GameSettingOptionObservation(
                value=option.value,
                bounds=bounds,
                click_bounds=_click_bounds_from_marker(marker_bounds),
                marker_activity=activity,
            )
        )

    vertical_rect = _union_rects((label.bounds, *option_bounds, *tuple(marker_rects)))
    row_bounds = (
        OPTIONS_VIEWPORT_AREA[0],
        max(OPTIONS_VIEWPORT_AREA[1], vertical_rect[1] - 10),
        OPTIONS_VIEWPORT_AREA[2],
        min(OPTIONS_VIEWPORT_AREA[3], vertical_rect[3] + 10),
    )
    return GameSettingRowObservation(
        value=_select_from_observations(spec, observations),
        row_bounds=row_bounds,
        options=tuple(observations),
    )


def observe_game_setting_row(
    image: np.ndarray,
    spec: GameSettingRowSpec,
    *,
    detections: tuple[OcrTextBox, ...] | None = None,
) -> GameSettingRowObservation | None:
    """Locate one current EN row and resolve the selected value fail-closed."""

    _validate_frame(image)
    if not isinstance(spec, GameSettingRowSpec):
        raise TypeError("spec должен быть GameSettingRowSpec")
    value_type = type(spec.options[0].value)
    if any(type(option.value) is not value_type for option in spec.options):
        raise TypeError("Все options одной строки должны принадлежать одной value family")

    groups = (
        _FRAME_OCR_CACHE.get_groups(image)
        if detections is None
        else _same_line_groups(detections)
    )

    if spec.layout == ROW_LAYOUT_TOGGLE_COLUMNS:
        return _observe_toggle_columns(image, spec, groups)
    if spec.layout == ROW_LAYOUT_CHOICE_CARDS:
        return _observe_choice_cards(image, spec, groups)
    raise ValueError(f"Неподдерживаемый layout Game Setting: {spec.layout!r}")


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
    layout=ROW_LAYOUT_CHOICE_CARDS,
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
        "during Auto",
    ),
    options=TOGGLE_OFF_ON,
)
OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW = GameSettingRowSpec(
    label_aliases=(
        "Default to Auto Mode in Threat Safe",
        "Auto mode default on in safe seas",
        "Threat Safe",
        "Auto Mode in sec",
    ),
    options=TOGGLE_OFF_ON,
)
STORY_AUTOPLAY_ROW = GameSettingRowSpec(
    label_aliases=("Story Autoplay", "Story auto-play"),
    options=(
        GameSettingOptionSpec(
            StoryAutoplayValue.DISABLED,
            ("Disable", "Disabled"),
        ),
        GameSettingOptionSpec(
            StoryAutoplayValue.ENABLED,
            ("Enable", "Enabled"),
        ),
    ),
    layout=ROW_LAYOUT_CHOICE_CARDS,
)
TEXT_AUTO_SCROLL_SPEED_ROW = GameSettingRowSpec(
    label_aliases=("Text Auto-Scroll Speed",),
    options=(
        GameSettingOptionSpec(TextAutoScrollSpeedValue.SLOW, ("Slow",)),
        GameSettingOptionSpec(TextAutoScrollSpeedValue.NORMAL, ("Normal",)),
        GameSettingOptionSpec(TextAutoScrollSpeedValue.FAST, ("Fast",)),
        GameSettingOptionSpec(
            TextAutoScrollSpeedValue.VERY_FAST,
            ("Very Fast", "Extra Fast"),
        ),
    ),
    layout=ROW_LAYOUT_CHOICE_CARDS,
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
        "Quick-Switch Prompt",
        "Quick-change second confirmation dialog",
    ),
    options=TOGGLE_OFF_ON,
)
DISPLAY_BATTLE_RESULT_CUTSCENE_ROW = GameSettingRowSpec(
    label_aliases=(
        "Display Battle Result Cutscene",
        "Battle Result Cutscene",
        "Show settlement characters",
    ),
    options=TOGGLE_OFF_ON,
)
CUSTOM_SHIP_NAMES_ROW = GameSettingRowSpec(
    label_aliases=("Custom Ship Names", "Change Oathed Ship Names"),
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
