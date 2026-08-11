"""Stage 5 Dock level, raw-star, and progression observations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import ClassVar, Protocol

import cv2
import numpy as np

from module.combat.level import LevelOcr
from module.dock_inventory.card_grid import (
    DockCardGridScanner,
    DockCardPresence,
    DockCardSlotObservation,
    DockViewportCardScan,
)
from module.dock_inventory.catalog import (
    DockIdentityCatalog,
    load_dock_identity_catalog,
)
from module.dock_inventory.identity import (
    DockCardIdentityObservation,
    DockIdentityScanner,
    DockViewportIdentityScan,
)
from module.dock_inventory.model import StarObservation
from module.dock_inventory.navigation import (
    DockInventoryNavigator,
    DockPrerequisiteEvidence,
)
from module.dock_inventory.progression import (
    DockProgressionCatalog,
    DockProgressionObservation,
    derive_dock_progression,
    load_dock_progression_catalog,
)
from module.dock_inventory.traversal import DockTraversalResult, DockTraversalViewport
from module.logger import logger


class DockLevelStatus(Enum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class DockStarStatus(Enum):
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class DockStarGlyphState(Enum):
    FILLED = "filled"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class DockAttributeError(RuntimeError):
    """Base operational failure for Stage 5 Dock scanning."""


class DockAttributeInputError(DockAttributeError):
    """Stage 2/3/4 evidence does not describe the same detached frame."""


class DockAttributeIncompleteError(DockAttributeError):
    """Stage 3 UNKNOWN prevents a complete Stage 5 pass."""


class DockLevelOcrError(DockAttributeError):
    """The level OCR backend failed operationally."""


class DockStarCvError(DockAttributeError):
    """The star CV backend received invalid data or broke an invariant."""


def _validate_area(area: object, name: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(area, tuple)
        or len(area) != 4
        or any(type(value) is not int for value in area)
    ):
        raise TypeError(f"{name} должен быть tuple из четырёх int")
    x1, y1, x2, y2 = area
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError(f"{name} должен задавать положительный прямоугольник")
    return area


@dataclass(frozen=True, slots=True)
class DockLevelObservation:
    status: DockLevelStatus
    area: tuple[int, int, int, int]
    value: int | None = None
    reason: str | None = None
    raw_diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DockLevelStatus):
            raise TypeError("status должен быть DockLevelStatus")
        _validate_area(self.area, "level area")
        if self.status is DockLevelStatus.OBSERVED:
            if type(self.value) is not int or self.value < 1:
                raise ValueError("OBSERVED level требует положительный int")
            if self.reason is not None:
                raise ValueError("OBSERVED level не должен содержать reason")
        else:
            if self.value is not None:
                raise ValueError("UNKNOWN level не должен содержать guessed value")
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("UNKNOWN level требует reason")
        if self.raw_diagnostic is not None and not isinstance(self.raw_diagnostic, str):
            raise TypeError("raw_diagnostic должен быть string или None")


@dataclass(frozen=True, slots=True)
class DockStarGlyphObservation:
    index: int
    state: DockStarGlyphState
    area: tuple[int, int, int, int]
    shape_score: float
    fill_ratio: float
    upper_fill_ratio: float
    fill_match_score: float

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("star glyph index должен быть неотрицательным int")
        if not isinstance(self.state, DockStarGlyphState):
            raise TypeError("state должен быть DockStarGlyphState")
        _validate_area(self.area, "star glyph area")
        for name, value in (
            ("shape_score", self.shape_score),
            ("fill_ratio", self.fill_ratio),
            ("upper_fill_ratio", self.upper_fill_ratio),
            ("fill_match_score", self.fill_match_score),
        ):
            if (
                not isinstance(value, float)
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError(f"{name} должен быть конечным float в [0, 1]")


@dataclass(frozen=True, slots=True)
class DockStarScanObservation:
    status: DockStarStatus
    area: tuple[int, int, int, int]
    stars: StarObservation | None = None
    glyphs: tuple[DockStarGlyphObservation, ...] = ()
    reason: str | None = None
    detected_total: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DockStarStatus):
            raise TypeError("status должен быть DockStarStatus")
        _validate_area(self.area, "star area")
        if not isinstance(self.glyphs, tuple) or not all(
            isinstance(value, DockStarGlyphObservation) for value in self.glyphs
        ):
            raise TypeError("glyphs должен быть tuple DockStarGlyphObservation")
        if self.detected_total is not None and (
            type(self.detected_total) is not int or self.detected_total < 1
        ):
            raise ValueError("detected_total должен быть положительным int или None")
        if self.status is DockStarStatus.OBSERVED:
            if not isinstance(self.stars, StarObservation):
                raise ValueError("OBSERVED stars требует StarObservation")
            if self.reason is not None or self.detected_total != self.stars.total:
                raise ValueError("OBSERVED stars содержит несогласованный total/reason")
            if len(self.glyphs) != self.stars.total or any(
                glyph.state is DockStarGlyphState.UNKNOWN for glyph in self.glyphs
            ):
                raise ValueError("OBSERVED stars требует классификацию каждого glyph")
        else:
            if self.stars is not None:
                raise ValueError(
                    "UNKNOWN stars не должен содержать guessed StarObservation"
                )
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("UNKNOWN stars требует reason")


@dataclass(frozen=True, slots=True)
class DockCardAttributeObservation:
    identity: DockCardIdentityObservation
    level: DockLevelObservation
    stars: DockStarScanObservation
    progression: DockProgressionObservation

    def __post_init__(self) -> None:
        if not isinstance(self.identity, DockCardIdentityObservation):
            raise TypeError("identity должен быть DockCardIdentityObservation")
        if not isinstance(self.level, DockLevelObservation):
            raise TypeError("level должен быть DockLevelObservation")
        if not isinstance(self.stars, DockStarScanObservation):
            raise TypeError("stars должен быть DockStarScanObservation")
        if not isinstance(self.progression, DockProgressionObservation):
            raise TypeError("progression должен быть DockProgressionObservation")

    @property
    def viewport_index(self) -> int:
        return self.identity.viewport_index

    @property
    def slot_index(self) -> int:
        return self.identity.slot_index

    @property
    def row(self) -> int:
        return self.identity.row

    @property
    def column(self) -> int:
        return self.identity.column


@dataclass(frozen=True, slots=True)
class DockViewportAttributeScan:
    viewport_index: int
    scroll_position: float
    card_scan: DockViewportCardScan
    identity_scan: DockViewportIdentityScan
    observations: tuple[DockCardAttributeObservation, ...]
    level_attempts: int
    star_attempts: int

    def __post_init__(self) -> None:
        if type(self.viewport_index) is not int or self.viewport_index < 0:
            raise ValueError("viewport_index должен быть неотрицательным int")
        if not isinstance(self.scroll_position, float) or not math.isfinite(
            self.scroll_position
        ):
            raise ValueError("scroll_position должен быть конечным float")
        if not isinstance(self.card_scan, DockViewportCardScan):
            raise TypeError("card_scan имеет неверный тип")
        if not isinstance(self.identity_scan, DockViewportIdentityScan):
            raise TypeError("identity_scan имеет неверный тип")
        if not isinstance(self.observations, tuple) or not all(
            isinstance(value, DockCardAttributeObservation)
            for value in self.observations
        ):
            raise TypeError("observations имеет неверный тип")
        expected = self.card_scan.present_count
        if (
            self.card_scan.viewport_index != self.viewport_index
            or self.identity_scan.viewport_index != self.viewport_index
            or len(self.observations) != expected
            or self.level_attempts != expected
            or self.star_attempts != expected
        ):
            raise ValueError("Stage 5 attempts/observations не совпадают с PRESENT")

    @property
    def level_unknown_count(self) -> int:
        return sum(
            item.level.status is DockLevelStatus.UNKNOWN for item in self.observations
        )

    @property
    def star_unknown_count(self) -> int:
        return sum(
            item.stars.status is DockStarStatus.UNKNOWN for item in self.observations
        )


@dataclass(frozen=True, slots=True)
class DockAttributeScanResult:
    prerequisite: DockPrerequisiteEvidence
    traversal: DockTraversalResult
    viewports: tuple[DockViewportAttributeScan, ...]
    identity_catalog_fingerprint: str
    progression_catalog_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.prerequisite, DockPrerequisiteEvidence):
            raise TypeError("prerequisite имеет неверный тип")
        if not isinstance(self.traversal, DockTraversalResult):
            raise TypeError("traversal имеет неверный тип")
        if not isinstance(self.viewports, tuple) or not all(
            isinstance(value, DockViewportAttributeScan) for value in self.viewports
        ):
            raise TypeError("viewports имеет неверный тип")
        if len(self.viewports) != self.traversal.visited_viewports:
            raise ValueError("Число Stage 5 viewports не совпадает с traversal")
        for name, value in (
            ("identity_catalog_fingerprint", self.identity_catalog_fingerprint),
            ("progression_catalog_fingerprint", self.progression_catalog_fingerprint),
        ):
            if not re_full_sha256(value):
                raise ValueError(f"{name} должен быть SHA-256")


def re_full_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


class _DockLevelOcr(Protocol):
    def read_levels(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[object, ...]: ...


class DockLevelOcrAdapter:
    """Small adapter around the existing level OCR primitive."""

    def read_levels(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[object, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        result = LevelOcr(list(areas), name="DOCK_LEVEL_OCR", threshold=64).ocr(frame)
        values = result if isinstance(result, list) else [result]
        return tuple(values)


class DockLevelScanner:
    """Read levels from dynamic slot-relative ROIs without legacy clamping."""

    # The legacy EN CARD_LEVEL_GRIDS authority is (77, 5, 138, 27). Real
    # 1280x720 calibration extends its vertical anti-aliasing margin and drops
    # the rightmost decoration column which otherwise becomes a false "1".
    LEVEL_LEFT = 77
    LEVEL_TOP = 0
    LEVEL_RIGHT = 136
    LEVEL_BOTTOM = 31

    def __init__(
        self,
        maximum_observed_level: int,
        *,
        ocr: _DockLevelOcr | None = None,
    ) -> None:
        if type(maximum_observed_level) is not int or maximum_observed_level < 1:
            raise ValueError("maximum_observed_level должен быть положительным int")
        self.maximum_observed_level = maximum_observed_level
        self.ocr = DockLevelOcrAdapter() if ocr is None else ocr

    def level_area(
        self,
        slot_area: tuple[int, int, int, int],
        frame_shape: tuple[int, ...],
    ) -> tuple[int, int, int, int]:
        if len(frame_shape) < 2:
            raise DockAttributeInputError("Frame shape не содержит height/width.")
        x1, y1, _x2, _y2 = _validate_area(slot_area, "slot area")
        area = (
            x1 + self.LEVEL_LEFT,
            y1 + self.LEVEL_TOP,
            x1 + self.LEVEL_RIGHT,
            y1 + self.LEVEL_BOTTOM,
        )
        left, top, right, bottom = area
        height, width = frame_shape[:2]
        if left < 0 or top < 0 or right > width or bottom > height:
            raise DockAttributeInputError(
                f"Slot-relative level ROI выходит за frame: slot={slot_area}, roi={area}."
            )
        return area

    def scan(
        self,
        frame: np.ndarray,
        slots: Sequence[DockCardSlotObservation],
    ) -> tuple[DockLevelObservation, ...]:
        _validate_rgb_frame(frame)
        slots = tuple(slots)
        areas = tuple(self.level_area(slot.area, frame.shape) for slot in slots)
        try:
            values = self.ocr.read_levels(np.array(frame, copy=True), areas)
        except DockLevelOcrError:
            raise
        except Exception as exc:
            raise DockLevelOcrError(
                f"Операционный сбой Dock level OCR: {type(exc).__name__}: {exc}"
            ) from exc
        if len(values) != len(slots):
            raise DockLevelOcrError(
                "Число level OCR results не совпало с числом PRESENT slots."
            )
        return tuple(
            self._observation(area, value) for area, value in zip(areas, values)
        )

    def _observation(
        self, area: tuple[int, int, int, int], raw_value: object
    ) -> DockLevelObservation:
        diagnostic = repr(raw_value)
        if type(raw_value) is not int:
            return DockLevelObservation(
                status=DockLevelStatus.UNKNOWN,
                area=area,
                reason="ocr_result_not_integer",
                raw_diagnostic=diagnostic,
            )
        if raw_value < 1:
            return DockLevelObservation(
                status=DockLevelStatus.UNKNOWN,
                area=area,
                reason="blank_or_nonpositive_ocr",
                raw_diagnostic=diagnostic,
            )
        if raw_value > self.maximum_observed_level:
            return DockLevelObservation(
                status=DockLevelStatus.UNKNOWN,
                area=area,
                reason="outside_pinned_level_range",
                raw_diagnostic=diagnostic,
            )
        return DockLevelObservation(
            status=DockLevelStatus.OBSERVED,
            area=area,
            value=raw_value,
            raw_diagnostic=diagnostic,
        )


class DockStarScanner:
    """Universal calibrated star-row CV for supplied detached RGB frames."""

    STAR_LEFT = 0
    STAR_TOP = 177
    STAR_RIGHT = 138
    STAR_BOTTOM = 203

    YELLOW_HUE_MIN = 15
    YELLOW_HUE_MAX = 45
    YELLOW_SATURATION_MIN = 80
    YELLOW_VALUE_MIN = 100

    STAR_TEMPLATE_SIZE = 19
    STAR_SPACING = 14.5
    FIRST_MATCH_MIN = 0.25
    FIRST_RELATIVE_SCORE_MIN = 0.65
    FIRST_CENTER_TOLERANCE = 2.5
    # The UI centers rows of the three source-proven totals differently. The
    # total is selected from the visually matched first filled star, never
    # copied from identity metadata.
    SUPPORTED_TOTAL_FIRST_CENTERS: ClassVar[dict[int, float]] = {
        4: 48.5,
        5: 41.5,
        6: 34.5,
    }
    FILLED_RATIO_MIN = 0.35
    FILLED_UPPER_RATIO_MIN = 0.20
    FILLED_MATCH_MIN = 0.26
    FILLED_MATCH_RATIO_MIN = 0.32
    FILLED_WEAK_MATCH_MIN = 0.18
    FILLED_WEAK_RATIO_MIN = 0.25
    FILLED_WEAK_UPPER_RATIO_MIN = 0.23
    # Real outlined empty glyphs retain small yellow anti-aliased/highlight
    # fragments. The calibrated cutoff stays below the weakest continuous
    # filled-row evidence on the acceptance frame sets.
    EMPTY_RATIO_MAX = 0.245
    TRAILING_FILLED_RATIO_MIN = 0.26
    SHAPE_SCORE_MIN = 0.45
    GLYPH_ALIGNMENT_RADIUS = 2

    def __init__(self) -> None:
        self._fill_template, self._outline_template = self._build_templates()
        self._inner_template = cv2.erode(
            self._fill_template, np.ones((3, 3), dtype=np.uint8)
        )
        y_coordinates = np.indices(self._inner_template.shape)[0]
        self._upper_inner_template = np.where(
            (self._inner_template > 0) & (y_coordinates <= 10),
            255,
            0,
        ).astype(np.uint8)
        self._outline_distance = cv2.distanceTransform(
            255 - self._outline_template, cv2.DIST_L2, 3
        )

    @classmethod
    def _build_templates(cls) -> tuple[np.ndarray, np.ndarray]:
        center = cls.STAR_TEMPLATE_SIZE / 2
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            radius = 8.0 if index % 2 == 0 else 3.6
            points.append(
                (
                    round(center + radius * math.cos(angle)),
                    round(center + radius * math.sin(angle)),
                )
            )
        polygon = np.array(points, dtype=np.int32)
        fill = np.zeros(
            (cls.STAR_TEMPLATE_SIZE, cls.STAR_TEMPLATE_SIZE), dtype=np.uint8
        )
        outline = np.zeros_like(fill)
        cv2.fillPoly(fill, [polygon], 255)
        cv2.polylines(outline, [polygon], True, 255, 1, cv2.LINE_AA)
        outline = np.where(outline > 80, 255, 0).astype(np.uint8)
        return fill, outline

    def star_area(
        self, slot_area: tuple[int, int, int, int]
    ) -> tuple[int, int, int, int]:
        x1, y1, _x2, _y2 = _validate_area(slot_area, "slot area")
        return (
            x1 + self.STAR_LEFT,
            y1 + self.STAR_TOP,
            x1 + self.STAR_RIGHT,
            y1 + self.STAR_BOTTOM,
        )

    def scan(
        self,
        frame: np.ndarray,
        slots: Sequence[DockCardSlotObservation],
    ) -> tuple[DockStarScanObservation, ...]:
        _validate_rgb_frame(frame)
        height, width = frame.shape[:2]
        return tuple(
            self._scan_area(
                frame, self.star_area(slot.area), width=width, height=height
            )
            for slot in slots
        )

    def _scan_area(
        self,
        frame: np.ndarray,
        area: tuple[int, int, int, int],
        *,
        width: int,
        height: int,
    ) -> DockStarScanObservation:
        x1, y1, x2, y2 = area
        if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
            return DockStarScanObservation(
                status=DockStarStatus.UNKNOWN,
                area=area,
                reason="star_roi_clipped",
            )
        try:
            roi = frame[y1:y2, x1:x2]
            hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
            yellow = (
                (hsv[:, :, 0] >= self.YELLOW_HUE_MIN)
                & (hsv[:, :, 0] <= self.YELLOW_HUE_MAX)
                & (hsv[:, :, 1] >= self.YELLOW_SATURATION_MIN)
                & (hsv[:, :, 2] >= self.YELLOW_VALUE_MIN)
            ).astype(np.uint8) * 255
            matched = cv2.matchTemplate(
                yellow, self._fill_template, cv2.TM_CCOEFF_NORMED
            )
        except cv2.error as exc:
            raise DockStarCvError(f"Операционный сбой Dock star CV: {exc}") from exc

        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 40, 100)
        edge_distance = cv2.distanceTransform(255 - edges, cv2.DIST_L2, 3)
        outline_y, outline_x = np.where(self._outline_template > 0)
        first = self._first_filled_star(
            matched,
            edges,
            edge_distance,
            outline_x,
            outline_y,
        )
        if first is None:
            return DockStarScanObservation(
                status=DockStarStatus.UNKNOWN,
                area=area,
                reason="first_filled_star_not_proven",
            )
        first_center_x, first_center_y, total = first
        glyphs = []
        inner_y, inner_x = np.where(self._inner_template > 0)
        upper_inner_y, upper_inner_x = np.where(self._upper_inner_template > 0)
        half = self.STAR_TEMPLATE_SIZE // 2
        for index in range(total):
            center_x = round(first_center_x + index * self.STAR_SPACING)
            left = center_x - half
            top = first_center_y - half
            right = left + self.STAR_TEMPLATE_SIZE
            bottom = top + self.STAR_TEMPLATE_SIZE
            if left < 0 or top < 0 or right > roi.shape[1] or bottom > roi.shape[0]:
                return DockStarScanObservation(
                    status=DockStarStatus.UNKNOWN,
                    area=area,
                    reason="star_geometry_clipped",
                    detected_total=total,
                    glyphs=tuple(glyphs),
                )
            aligned = self._best_shape_alignment(
                edges,
                edge_distance,
                center_x,
                first_center_y,
                outline_x,
                outline_y,
            )
            if aligned is None:
                return DockStarScanObservation(
                    status=DockStarStatus.UNKNOWN,
                    area=area,
                    reason="star_geometry_clipped",
                    detected_total=total,
                    glyphs=tuple(glyphs),
                )
            left, top, shape_score = aligned
            right = left + self.STAR_TEMPLATE_SIZE
            bottom = top + self.STAR_TEMPLATE_SIZE
            fill_ratio = float(np.mean(yellow[top + inner_y, left + inner_x] > 0))
            upper_fill_ratio = float(
                np.mean(yellow[top + upper_inner_y, left + upper_inner_x] > 0)
            )
            match_neighborhood = matched[
                max(0, top - 2) : min(matched.shape[0], top + 3),
                max(0, left - 2) : min(matched.shape[1], left + 3),
            ]
            fill_match_score = float(max(0.0, np.max(match_neighborhood)))
            if shape_score < self.SHAPE_SCORE_MIN:
                state = DockStarGlyphState.UNKNOWN
            elif (
                (
                    fill_ratio >= self.FILLED_RATIO_MIN
                    and upper_fill_ratio >= self.FILLED_UPPER_RATIO_MIN
                )
                or (
                    fill_match_score >= self.FILLED_MATCH_MIN
                    and fill_ratio >= self.FILLED_MATCH_RATIO_MIN
                )
                or (
                    self.FILLED_WEAK_MATCH_MIN
                    <= fill_match_score
                    < self.FILLED_MATCH_MIN
                    and fill_ratio >= self.FILLED_WEAK_RATIO_MIN
                    and upper_fill_ratio >= self.FILLED_WEAK_UPPER_RATIO_MIN
                )
            ):
                state = DockStarGlyphState.FILLED
            elif fill_ratio <= self.EMPTY_RATIO_MAX:
                state = DockStarGlyphState.EMPTY
            else:
                state = DockStarGlyphState.UNKNOWN
            glyphs.append(
                DockStarGlyphObservation(
                    index=index,
                    state=state,
                    area=(x1 + left, y1 + top, x1 + right, y1 + bottom),
                    shape_score=shape_score,
                    fill_ratio=fill_ratio,
                    upper_fill_ratio=upper_fill_ratio,
                    fill_match_score=fill_match_score,
                )
            )
        for index, glyph in enumerate(glyphs):
            if glyph.state is not DockStarGlyphState.UNKNOWN:
                continue
            earlier = tuple(item.state for item in glyphs[:index])
            later = tuple(item.state for item in glyphs[index + 1 :])
            if (
                DockStarGlyphState.FILLED in later
                and DockStarGlyphState.EMPTY not in earlier
            ):
                glyphs[index] = replace(glyph, state=DockStarGlyphState.FILLED)
            elif (
                DockStarGlyphState.EMPTY in earlier
                and DockStarGlyphState.FILLED not in later
            ):
                glyphs[index] = replace(glyph, state=DockStarGlyphState.EMPTY)
        if DockStarGlyphState.EMPTY not in (item.state for item in glyphs):
            for index, glyph in enumerate(glyphs):
                if glyph.state is not DockStarGlyphState.UNKNOWN or index == 0:
                    continue
                if any(
                    item.state is not DockStarGlyphState.FILLED
                    for item in glyphs[:index]
                ):
                    break
                if glyph.fill_ratio >= self.TRAILING_FILLED_RATIO_MIN:
                    glyphs[index] = replace(glyph, state=DockStarGlyphState.FILLED)
                else:
                    break
        glyph_tuple = tuple(glyphs)
        if any(glyph.state is DockStarGlyphState.UNKNOWN for glyph in glyph_tuple):
            return DockStarScanObservation(
                status=DockStarStatus.UNKNOWN,
                area=area,
                reason="ambiguous_star_glyph",
                detected_total=total,
                glyphs=glyph_tuple,
            )
        filled = sum(glyph.state is DockStarGlyphState.FILLED for glyph in glyph_tuple)
        stars = StarObservation(filled=filled, empty=total - filled, total=total)
        return DockStarScanObservation(
            status=DockStarStatus.OBSERVED,
            area=area,
            stars=stars,
            detected_total=total,
            glyphs=glyph_tuple,
        )

    def _first_filled_star(
        self,
        matched: np.ndarray,
        edges: np.ndarray,
        edge_distance: np.ndarray,
        outline_x: np.ndarray,
        outline_y: np.ndarray,
    ) -> tuple[int, int, int] | None:
        if matched.ndim != 2 or not matched.size or not np.all(np.isfinite(matched)):
            raise DockStarCvError(
                "Star template response имеет неверную форму/значения."
            )
        profile = np.max(matched, axis=0)
        half = self.STAR_TEMPLATE_SIZE // 2
        candidates: list[tuple[int, int, int, float]] = []
        left_index = 22
        right_index = min(47, len(profile) - 1)
        for index in range(left_index, right_index + 1):
            score = float(profile[index])
            before = profile[max(0, index - 3) : index]
            after = profile[index + 1 : index + 4]
            if (
                score >= self.FIRST_MATCH_MIN
                and (not len(before) or score >= float(np.max(before)))
                and (not len(after) or score >= float(np.max(after)))
            ):
                center_x = index + half
                center_y = int(np.argmax(matched[:, index])) + half
                totals = tuple(
                    total
                    for total, expected_center in self.SUPPORTED_TOTAL_FIRST_CENTERS.items()
                    if abs(center_x - expected_center) <= self.FIRST_CENTER_TOLERANCE
                )
                if len(totals) != 1:
                    continue
                total = totals[0]
                row_is_proven = all(
                    (
                        aligned := self._best_shape_alignment(
                            edges,
                            edge_distance,
                            round(center_x + glyph_index * self.STAR_SPACING),
                            center_y,
                            outline_x,
                            outline_y,
                        )
                    )
                    is not None
                    and aligned[2] >= self.SHAPE_SCORE_MIN
                    for glyph_index in range(total)
                )
                if row_is_proven:
                    candidates.append((center_x, center_y, total, score))
        if not candidates:
            return None
        best_score = max(value[3] for value in candidates)
        candidates = [
            value
            for value in candidates
            if value[3] >= best_score * self.FIRST_RELATIVE_SCORE_MIN
        ]
        # A six-star row contains a four-star-aligned inner subsequence. Prefer
        # the longest fully proven row only while its first-glyph response is
        # competitive with the strongest candidate. This rejects weak portrait
        # peaks without consulting static identity metadata.
        center_x, center_y, total, _score = max(candidates, key=lambda value: value[2])
        return center_x, center_y, total

    def _best_shape_alignment(
        self,
        edges: np.ndarray,
        edge_distance: np.ndarray,
        center_x: int,
        center_y: int,
        outline_x: np.ndarray,
        outline_y: np.ndarray,
    ) -> tuple[int, int, float] | None:
        half = self.STAR_TEMPLATE_SIZE // 2
        candidates = []
        for offset_y in range(
            -self.GLYPH_ALIGNMENT_RADIUS, self.GLYPH_ALIGNMENT_RADIUS + 1
        ):
            for offset_x in range(
                -self.GLYPH_ALIGNMENT_RADIUS,
                self.GLYPH_ALIGNMENT_RADIUS + 1,
            ):
                left = center_x + offset_x - half
                top = center_y + offset_y - half
                right = left + self.STAR_TEMPLATE_SIZE
                bottom = top + self.STAR_TEMPLATE_SIZE
                if (
                    left < 0
                    or top < 0
                    or right > edges.shape[1]
                    or bottom > edges.shape[0]
                ):
                    continue
                score = self._shape_score(
                    edges[top:bottom, left:right],
                    edge_distance,
                    left,
                    top,
                    outline_x,
                    outline_y,
                )
                candidates.append((left, top, score))
        if not candidates:
            return None
        return max(candidates, key=lambda value: value[2])

    def _shape_score(
        self,
        edge_patch: np.ndarray,
        edge_distance: np.ndarray,
        left: int,
        top: int,
        outline_x: np.ndarray,
        outline_y: np.ndarray,
    ) -> float:
        template_to_image = float(
            np.exp(
                -(edge_distance[top + outline_y, left + outline_x] ** 2) / 4.0
            ).mean()
        )
        edge_y, edge_x = np.where(edge_patch > 0)
        if not len(edge_y):
            return 0.0
        image_to_template = float(
            np.exp(-(self._outline_distance[edge_y, edge_x] ** 2) / 4.0).mean()
        )
        denominator = template_to_image + image_to_template
        if denominator <= 0.0:
            return 0.0
        return float(
            max(
                0.0,
                min(
                    1.0,
                    2.0 * template_to_image * image_to_template / denominator,
                ),
            )
        )


class DockAttributeScanner:
    """Compose Stage 3/4 evidence with independent level/star observations."""

    def __init__(
        self,
        progression_catalog: DockProgressionCatalog,
        *,
        level_scanner: DockLevelScanner | None = None,
        star_scanner: DockStarScanner | None = None,
    ) -> None:
        if not isinstance(progression_catalog, DockProgressionCatalog):
            raise TypeError("progression_catalog имеет неверный тип")
        self.progression_catalog = progression_catalog
        self.level_scanner = (
            DockLevelScanner(progression_catalog.maximum_observed_level)
            if level_scanner is None
            else level_scanner
        )
        self.star_scanner = DockStarScanner() if star_scanner is None else star_scanner

    def scan_viewport(
        self,
        viewport: DockTraversalViewport,
        card_scan: DockViewportCardScan,
        identity_scan: DockViewportIdentityScan,
    ) -> DockViewportAttributeScan:
        present = self._validate_inputs(viewport, card_scan, identity_scan)
        frame_before = np.array(viewport.frame, copy=True)
        levels = self.level_scanner.scan(np.array(viewport.frame, copy=True), present)
        stars = self.star_scanner.scan(np.array(viewport.frame, copy=True), present)
        if not np.array_equal(viewport.frame, frame_before):
            raise DockAttributeInputError("Stage 5 scanner изменил caller-owned frame.")
        if len(levels) != len(present) or len(stars) != len(present):
            raise DockAttributeInputError(
                "Stage 5 scanner вернул неверное число результатов."
            )
        observations = []
        for identity, level, star in zip(identity_scan.observations, levels, stars):
            resolution = identity.resolution
            observations.append(
                DockCardAttributeObservation(
                    identity=identity,
                    level=level,
                    stars=star,
                    progression=derive_dock_progression(
                        identity_status=resolution.status,
                        canonical_identity=resolution.canonical_identity,
                        observed_stars=star.stars,
                        catalog=self.progression_catalog,
                    ),
                )
            )
        result = DockViewportAttributeScan(
            viewport_index=viewport.index,
            scroll_position=float(viewport.scroll_position),
            card_scan=card_scan,
            identity_scan=identity_scan,
            observations=tuple(observations),
            level_attempts=len(present),
            star_attempts=len(present),
        )
        logger.info(
            "[Инвентарь дока] attributes окно=%s present=%s level_unknown=%s "
            "star_unknown=%s",
            result.viewport_index,
            len(result.observations),
            result.level_unknown_count,
            result.star_unknown_count,
        )
        return result

    @staticmethod
    def _validate_inputs(
        viewport: DockTraversalViewport,
        card_scan: DockViewportCardScan,
        identity_scan: DockViewportIdentityScan,
    ) -> tuple[DockCardSlotObservation, ...]:
        if not isinstance(viewport, DockTraversalViewport):
            raise TypeError("viewport должен быть DockTraversalViewport")
        if not isinstance(card_scan, DockViewportCardScan):
            raise TypeError("card_scan должен быть DockViewportCardScan")
        if not isinstance(identity_scan, DockViewportIdentityScan):
            raise TypeError("identity_scan должен быть DockViewportIdentityScan")
        if (
            viewport.index != card_scan.viewport_index
            or viewport.index != identity_scan.viewport_index
        ):
            raise DockAttributeInputError("Viewport/card/identity index mismatch.")
        if not math.isclose(
            viewport.scroll_position,
            card_scan.scroll_position,
            rel_tol=0.0,
            abs_tol=1e-9,
        ) or not math.isclose(
            viewport.scroll_position,
            identity_scan.scroll_position,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise DockAttributeInputError(
                "Viewport/card/identity scroll-position mismatch."
            )
        if identity_scan.card_scan != card_scan:
            raise DockAttributeInputError(
                "Identity scan содержит другой Stage 3 card scan."
            )
        if card_scan.unknown_count:
            raise DockAttributeIncompleteError(
                "Stage 3 UNKNOWN не позволяет объявить Stage 5 pass полным: "
                f"viewport={viewport.index}, unknown={card_scan.unknown_count}."
            )
        present = tuple(
            slot
            for slot in card_scan.slots
            if slot.presence is DockCardPresence.PRESENT
        )
        if len(identity_scan.observations) != len(present):
            raise DockAttributeInputError("Identity/PRESENT count mismatch.")
        for slot, identity in zip(present, identity_scan.observations):
            if (
                identity.viewport_index != viewport.index
                or identity.slot_index != slot.slot_index
                or identity.row != slot.row
                or identity.column != slot.column
                or identity.area != slot.area
            ):
                raise DockAttributeInputError(
                    "Identity scan не сохраняет Stage 3 PRESENT order/geometry."
                )
        return present


@dataclass(slots=True)
class DockAttributeCollector:
    identity_scanner: DockIdentityScanner
    attribute_scanner: DockAttributeScanner
    card_scanner: DockCardGridScanner = field(default_factory=DockCardGridScanner)
    _viewports: list[DockViewportAttributeScan] = field(
        default_factory=list, init=False
    )

    def __call__(self, viewport: DockTraversalViewport) -> None:
        card_scan = self.card_scanner.scan_viewport(viewport)
        identity_scan = self.identity_scanner.scan_viewport(viewport, card_scan)
        self._viewports.append(
            self.attribute_scanner.scan_viewport(viewport, card_scan, identity_scan)
        )

    @property
    def viewports(self) -> tuple[DockViewportAttributeScan, ...]:
        return tuple(self._viewports)


def scan_dock_attributes(
    navigator: DockInventoryNavigator,
    *,
    identity_catalog: DockIdentityCatalog | None = None,
    progression_catalog: DockProgressionCatalog | None = None,
    identity_scanner: DockIdentityScanner | None = None,
    attribute_scanner: DockAttributeScanner | None = None,
    card_scanner: DockCardGridScanner | None = None,
    run_stage2_kwargs: dict[str, object] | None = None,
) -> DockAttributeScanResult:
    """Run the production Stage 2 -> 3 -> 4 -> 5 path without deduplication."""

    if not isinstance(navigator, DockInventoryNavigator):
        raise TypeError("navigator должен быть DockInventoryNavigator")
    if identity_catalog is None:
        identity_catalog = load_dock_identity_catalog()
    if progression_catalog is None:
        progression_catalog = load_dock_progression_catalog()
    if progression_catalog.identity_fingerprint != identity_catalog.fingerprint:
        raise DockAttributeInputError(
            "Identity/progression catalogs имеют разные semantic fingerprints."
        )
    if identity_scanner is None:
        identity_scanner = DockIdentityScanner(identity_catalog)
    elif identity_scanner.catalog.fingerprint != identity_catalog.fingerprint:
        raise DockAttributeInputError("identity_scanner использует другой catalog.")
    if attribute_scanner is None:
        attribute_scanner = DockAttributeScanner(progression_catalog)
    elif (
        attribute_scanner.progression_catalog.fingerprint
        != progression_catalog.fingerprint
    ):
        raise DockAttributeInputError("attribute_scanner использует другой catalog.")
    collector = DockAttributeCollector(
        identity_scanner=identity_scanner,
        attribute_scanner=attribute_scanner,
        card_scanner=DockCardGridScanner() if card_scanner is None else card_scanner,
    )
    stage2 = navigator.run_stage2(
        collector,
        **({} if run_stage2_kwargs is None else run_stage2_kwargs),
    )
    return DockAttributeScanResult(
        prerequisite=stage2.prerequisite,
        traversal=stage2.traversal,
        viewports=collector.viewports,
        identity_catalog_fingerprint=identity_catalog.fingerprint,
        progression_catalog_fingerprint=progression_catalog.fingerprint,
    )


def _validate_rgb_frame(frame: np.ndarray) -> None:
    if (
        not isinstance(frame, np.ndarray)
        or frame.dtype != np.uint8
        or frame.ndim != 3
        or frame.shape[2] != 3
        or not frame.size
    ):
        raise DockAttributeInputError(
            "Stage 5 требует непустой RGB uint8 frame формы HxWx3."
        )


__all__ = [
    "DockAttributeError",
    "DockAttributeIncompleteError",
    "DockAttributeInputError",
    "DockAttributeScanResult",
    "DockAttributeScanner",
    "DockCardAttributeObservation",
    "DockLevelObservation",
    "DockLevelOcrError",
    "DockLevelScanner",
    "DockLevelStatus",
    "DockStarCvError",
    "DockStarGlyphObservation",
    "DockStarGlyphState",
    "DockStarScanObservation",
    "DockStarScanner",
    "DockStarStatus",
    "DockViewportAttributeScan",
    "scan_dock_attributes",
]
