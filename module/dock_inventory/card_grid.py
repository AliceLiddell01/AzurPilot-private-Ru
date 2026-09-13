"""Pure stable-frame card-grid registration for Dock Inventory consumers."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from module.dock_inventory.navigation import (
    DockInventoryNavigator,
    DockInventoryStage2Result,
    DockPrerequisiteEvidence,
)
from module.dock_inventory.traversal import DockTraversalResult, DockTraversalViewport
from module.logger import logger
from module.retire.dock import CARD_GRIDS


class DockCardPresence(Enum):
    """Evidence-backed occupancy state of one registered Dock slot."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DockCardPresenceEvidence:
    """Generic structural measurements used by the tri-state classifier."""

    luma_std: float
    edge_density: float
    chroma_mean: float

    def __post_init__(self) -> None:
        for name, value in (
            ("luma_std", self.luma_std),
            ("edge_density", self.edge_density),
            ("chroma_mean", self.chroma_mean),
        ):
            if not isinstance(value, float) or not math.isfinite(value):
                raise TypeError(f"{name} должен быть конечным float")
            if value < 0.0:
                raise ValueError(f"{name} не может быть отрицательным")
        if self.edge_density > 1.0:
            raise ValueError("edge_density должен быть в диапазоне [0, 1]")


@dataclass(frozen=True, slots=True)
class DockCardSlotObservation:
    """One row-major card position in the current registered viewport."""

    slot_index: int
    column: int
    row: int
    area: tuple[int, int, int, int]
    presence: DockCardPresence
    evidence: DockCardPresenceEvidence

    def __post_init__(self) -> None:
        for name, value in (
            ("slot_index", self.slot_index),
            ("column", self.column),
            ("row", self.row),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} должен быть int")
            if value < 0:
                raise ValueError(f"{name} не может быть отрицательным")
        if (
            not isinstance(self.area, tuple)
            or len(self.area) != 4
            or any(type(value) is not int for value in self.area)
        ):
            raise TypeError("area должен быть tuple из четырёх int")
        x1, y1, x2, y2 = self.area
        if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("area должен задавать положительный прямоугольник")
        if not isinstance(self.presence, DockCardPresence):
            raise TypeError("presence должен быть DockCardPresence")
        if not isinstance(self.evidence, DockCardPresenceEvidence):
            raise TypeError("evidence должен быть DockCardPresenceEvidence")


@dataclass(frozen=True, slots=True)
class DockViewportCardScan:
    """Compact, frame-free card-layout result for one traversal viewport."""

    viewport_index: int
    scroll_position: float
    registered_row_origins: tuple[int, ...]
    slots: tuple[DockCardSlotObservation, ...]
    registration_method: str = "row_variance_gap_v1"

    def __post_init__(self) -> None:
        if type(self.viewport_index) is not int or self.viewport_index < 0:
            raise ValueError("viewport_index должен быть неотрицательным int")
        if (
            not isinstance(self.scroll_position, float)
            or not math.isfinite(self.scroll_position)
            or not 0.0 <= self.scroll_position <= 1.0
        ):
            raise ValueError("scroll_position должен быть конечным float в [0, 1]")
        if not isinstance(self.registered_row_origins, tuple) or any(
            type(value) is not int for value in self.registered_row_origins
        ):
            raise TypeError("registered_row_origins должен быть tuple из int")
        if any(value < 0 for value in self.registered_row_origins):
            raise ValueError("registered_row_origins не могут быть отрицательными")
        if any(
            current <= previous
            for previous, current in zip(
                self.registered_row_origins,
                self.registered_row_origins[1:],
            )
        ):
            raise ValueError("registered_row_origins должны строго возрастать")
        if not isinstance(self.slots, tuple) or not all(
            isinstance(slot, DockCardSlotObservation) for slot in self.slots
        ):
            raise TypeError("slots должен быть tuple из DockCardSlotObservation")
        column_count = int(CARD_GRIDS.grid_shape[0])
        expected_slot_count = len(self.registered_row_origins) * column_count
        if len(self.slots) != expected_slot_count:
            raise ValueError(
                "Число slots должно точно соответствовать зарегистрированным строкам"
            )
        for slot_index, slot in enumerate(self.slots):
            expected_row, expected_column = divmod(slot_index, column_count)
            if (
                slot.slot_index != slot_index
                or slot.row != expected_row
                or slot.column != expected_column
                or slot.area[1] != self.registered_row_origins[expected_row]
            ):
                raise ValueError("slots должны сохранять canonical row-major порядок")
        if (
            not isinstance(self.registration_method, str)
            or not self.registration_method
        ):
            raise ValueError("registration_method должен быть непустой строкой")

    @property
    def present_count(self) -> int:
        return sum(slot.presence is DockCardPresence.PRESENT for slot in self.slots)

    @property
    def absent_count(self) -> int:
        return sum(slot.presence is DockCardPresence.ABSENT for slot in self.slots)

    @property
    def unknown_count(self) -> int:
        return sum(slot.presence is DockCardPresence.UNKNOWN for slot in self.slots)


@dataclass(frozen=True, slots=True)
class DockCardLayoutScanResult:
    """High-level Stage 2 traversal evidence plus per-viewport card layouts."""

    prerequisite: DockPrerequisiteEvidence
    traversal: DockTraversalResult
    viewports: tuple[DockViewportCardScan, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.prerequisite, DockPrerequisiteEvidence):
            raise TypeError("prerequisite должен быть DockPrerequisiteEvidence")
        if not isinstance(self.traversal, DockTraversalResult):
            raise TypeError("traversal должен быть DockTraversalResult")
        if not isinstance(self.viewports, tuple) or not all(
            isinstance(viewport, DockViewportCardScan) for viewport in self.viewports
        ):
            raise TypeError("viewports должен быть tuple из DockViewportCardScan")
        if len(self.viewports) != self.traversal.visited_viewports:
            raise ValueError(
                "Число card-layout результатов должно совпадать с traversal viewports"
            )


class DockCardGridError(RuntimeError):
    """Base operational error for stable-frame card-grid scanning."""


class DockCardGridFrameError(DockCardGridError):
    """The supplied frame cannot satisfy the supported geometry contract."""


class DockCardGridRegistrationError(DockCardGridError):
    """Dynamic row registration could not be proven from frame structure."""


class DockCardGridScanner:
    """Register full rows and classify card presence on one detached RGB frame.

    The scanner is stateless relative to viewports and has no device/UI owner.
    It deliberately keeps Stage 2's no-scroll behavior fail-closed: absence of
    a scrollbar is never reinterpreted here as small-Dock success.
    """

    COLUMN_COUNT = int(CARD_GRIDS.grid_shape[0])
    X_ORIGIN = int(CARD_GRIDS.origin[0])
    X_DELTA = float(CARD_GRIDS.delta[0])
    CARD_WIDTH = int(CARD_GRIDS.button_shape[0])
    CARD_HEIGHT = int(CARD_GRIDS.button_shape[1])
    ROW_DELTA = int(round(float(CARD_GRIDS.delta[1])))
    EXPECTED_GAP = ROW_DELTA - CARD_HEIGHT

    SAFE_SCAN_TOP = max(0, int(CARD_GRIDS.origin[1]) - EXPECTED_GAP)
    SAFE_BOTTOM_MARGIN = 1
    PROFILE_SMOOTHING_ROWS = max(3, EXPECTED_GAP // 4)
    PROFILE_WINDOW_CENTER_OFFSET = (PROFILE_SMOOTHING_ROWS - 1) / 2
    LOW_VARIANCE_THRESHOLD = 20.0
    GAP_RUN_MIN = max(3, EXPECTED_GAP // 3)
    GAP_RUN_MAX = int(round(EXPECTED_GAP * 1.5))
    GAP_CENTER_TO_ROW_ORIGIN = int(round(EXPECTED_GAP * 2 / 3))
    ROW_SPACING_TOLERANCE = max(2, EXPECTED_GAP // 5)
    CANDIDATE_GRID_TOLERANCE = ROW_SPACING_TOLERANCE + PROFILE_SMOOTHING_ROWS

    PRESENCE_INSET = 6
    PRESENT_LUMA_STD = 32.0
    PRESENT_EDGE_DENSITY = 0.12
    ABSENT_LUMA_STD = 18.0
    ABSENT_EDGE_DENSITY = 0.04
    EDGE_MAGNITUDE_THRESHOLD = 80.0

    def _validate_frame(self, frame: np.ndarray) -> tuple[int, int]:
        if (
            not isinstance(frame, np.ndarray)
            or frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
            or not frame.size
        ):
            raise DockCardGridFrameError(
                "Dock card-grid требует непустой RGB uint8 frame формы HxWx3."
            )
        height, width = frame.shape[:2]
        right = self._column_area(self.COLUMN_COUNT - 1, self.SAFE_SCAN_TOP)[2]
        if width < right or height <= self.SAFE_SCAN_TOP + self.CARD_HEIGHT:
            raise DockCardGridFrameError(
                f"Frame {width}x{height} меньше canonical Dock geometry."
            )
        return height, width

    def _column_area(self, column: int, row_origin: int) -> tuple[int, int, int, int]:
        x1 = int(np.rint(self.X_ORIGIN + column * self.X_DELTA))
        return x1, row_origin, x1 + self.CARD_WIDTH, row_origin + self.CARD_HEIGHT

    @staticmethod
    def _contiguous_runs(mask: np.ndarray) -> tuple[tuple[int, int], ...]:
        indexes = np.flatnonzero(mask)
        if not len(indexes):
            return ()
        runs: list[tuple[int, int]] = []
        start = previous = int(indexes[0])
        for raw_value in indexes[1:]:
            value = int(raw_value)
            if value != previous + 1:
                runs.append((start, previous))
                start = value
            previous = value
        runs.append((start, previous))
        return tuple(runs)

    def _candidate_row_origins(self, frame: np.ndarray) -> tuple[int, ...]:
        height, _ = self._validate_frame(frame)
        safe_bottom = height - self.SAFE_BOTTOM_MARGIN
        left = self.X_ORIGIN
        right = self._column_area(self.COLUMN_COUNT - 1, self.SAFE_SCAN_TOP)[2]
        gray = cv2.cvtColor(
            frame[self.SAFE_SCAN_TOP : safe_bottom, left:right],
            cv2.COLOR_RGB2GRAY,
        )
        row_variance = np.std(gray, axis=1)
        kernel = np.ones(self.PROFILE_SMOOTHING_ROWS, dtype=np.float64)
        kernel /= self.PROFILE_SMOOTHING_ROWS
        smoothed = np.convolve(row_variance, kernel, mode="valid")
        if not np.all(np.isfinite(smoothed)):
            raise DockCardGridRegistrationError(
                "Профиль строк Dock содержит нечисловые значения."
            )

        runs = self._contiguous_runs(smoothed < self.LOW_VARIANCE_THRESHOLD)
        origins = []
        for start, end in runs:
            run_width = end - start + 1
            if not self.GAP_RUN_MIN <= run_width <= self.GAP_RUN_MAX:
                continue
            center = (
                self.SAFE_SCAN_TOP
                + self.PROFILE_WINDOW_CENTER_OFFSET
                + (start + end) / 2
            )
            origins.append(int(np.rint(center + self.GAP_CENTER_TO_ROW_ORIGIN)))
        return tuple(origins)

    def _snap_candidate_origins(
        self,
        candidates: Sequence[int],
    ) -> tuple[int, ...]:
        """Fit structural candidates to the canonical vertical grid phase."""
        values = tuple(candidates)
        if not values:
            return ()
        if any(type(value) is not int for value in values):
            raise DockCardGridRegistrationError(
                "Candidate row origins должны быть int."
            )
        if any(current <= previous for previous, current in zip(values, values[1:])):
            raise DockCardGridRegistrationError(
                "Candidate row origins должны строго возрастать без дубликатов."
            )

        grid_indexes = [0]
        for previous, current in zip(values, values[1:]):
            spacing = current - previous
            grid_steps = max(1, int(np.rint(spacing / self.ROW_DELTA)))
            if (
                abs(spacing - grid_steps * self.ROW_DELTA)
                > self.CANDIDATE_GRID_TOLERANCE
            ):
                raise DockCardGridRegistrationError(
                    "Недопустимое расстояние между candidate rows Dock: "
                    f"{spacing}, canonical step={self.ROW_DELTA}."
                )
            grid_indexes.append(grid_indexes[-1] + grid_steps)

        phase_samples = tuple(
            value - grid_index * self.ROW_DELTA
            for value, grid_index in zip(values, grid_indexes)
        )
        phase = int(np.rint(np.median(phase_samples)))
        snapped = tuple(
            phase + grid_index * self.ROW_DELTA for grid_index in grid_indexes
        )
        if any(
            abs(raw_origin - snapped_origin) > self.CANDIDATE_GRID_TOLERANCE
            for raw_origin, snapped_origin in zip(values, snapped)
        ):
            raise DockCardGridRegistrationError(
                "Candidate rows не образуют доказуемую canonical Dock grid."
            )
        return snapped

    def measure_presence(
        self,
        frame: np.ndarray,
        area: tuple[int, int, int, int],
    ) -> DockCardPresenceEvidence:
        """Measure generic structure without mutating the supplied frame."""
        height, width = self._validate_frame(frame)
        x1, y1, x2, y2 = area
        if x1 < 0 or y1 < 0 or x2 > width or y2 > height or x2 <= x1 or y2 <= y1:
            raise DockCardGridFrameError(f"Slot area выходит за frame: {area!r}.")
        crop = frame[y1:y2, x1:x2]
        inset = self.PRESENCE_INSET
        if crop.shape[0] <= inset * 2 or crop.shape[1] <= inset * 2:
            raise DockCardGridFrameError(
                "Slot area слишком мала для presence evidence."
            )
        inner = crop[inset:-inset, inset:-inset]
        gray = cv2.cvtColor(inner, cv2.COLOR_RGB2GRAY)
        gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        magnitude = cv2.magnitude(gradient_x, gradient_y)
        chroma = np.max(inner, axis=2).astype(np.int16) - np.min(inner, axis=2).astype(
            np.int16
        )
        return DockCardPresenceEvidence(
            luma_std=float(np.std(gray)),
            edge_density=float(np.mean(magnitude > self.EDGE_MAGNITUDE_THRESHOLD)),
            chroma_mean=float(np.mean(chroma)),
        )

    def classify_presence(
        self,
        evidence: DockCardPresenceEvidence,
    ) -> DockCardPresence:
        """Require two independent strong signals for PRESENT or ABSENT."""
        if (
            evidence.luma_std >= self.PRESENT_LUMA_STD
            and evidence.edge_density >= self.PRESENT_EDGE_DENSITY
        ):
            return DockCardPresence.PRESENT
        if (
            evidence.luma_std <= self.ABSENT_LUMA_STD
            and evidence.edge_density <= self.ABSENT_EDGE_DENSITY
        ):
            return DockCardPresence.ABSENT
        return DockCardPresence.UNKNOWN

    def _scan_row(
        self,
        frame: np.ndarray,
        row_index: int,
        row_origin: int,
    ) -> tuple[DockCardSlotObservation, ...]:
        slots = []
        for column in range(self.COLUMN_COUNT):
            area = self._column_area(column, row_origin)
            evidence = self.measure_presence(frame, area)
            slots.append(
                DockCardSlotObservation(
                    slot_index=row_index * self.COLUMN_COUNT + column,
                    column=column,
                    row=row_index,
                    area=area,
                    presence=self.classify_presence(evidence),
                    evidence=evidence,
                )
            )
        return tuple(slots)

    def _validate_row_origins(
        self,
        origins: Sequence[int],
        *,
        height: int,
    ) -> tuple[int, ...]:
        values = tuple(origins)
        if not values:
            raise DockCardGridRegistrationError(
                "Не найдено ни одной доказанной полностью видимой строки Dock."
            )
        if any(type(value) is not int for value in values):
            raise DockCardGridRegistrationError("Row origins должны быть int.")
        if any(current <= previous for previous, current in zip(values, values[1:])):
            raise DockCardGridRegistrationError(
                "Row origins должны строго возрастать без дубликатов."
            )
        max_rows = (
            height - self.SAFE_BOTTOM_MARGIN - self.SAFE_SCAN_TOP - self.CARD_HEIGHT
        ) // self.ROW_DELTA + 1
        if len(values) > max_rows:
            raise DockCardGridRegistrationError(
                f"Число зарегистрированных строк превышает geometry maximum: {max_rows}."
            )
        for previous, current in zip(values, values[1:]):
            spacing = current - previous
            if abs(spacing - self.ROW_DELTA) > self.ROW_SPACING_TOLERANCE:
                raise DockCardGridRegistrationError(
                    "Недопустимое расстояние между строками Dock: "
                    f"{spacing}, ожидалось {self.ROW_DELTA}±{self.ROW_SPACING_TOLERANCE}."
                )
        safe_bottom = height - self.SAFE_BOTTOM_MARGIN
        if any(
            origin < self.SAFE_SCAN_TOP or origin + self.CARD_HEIGHT > safe_bottom
            for origin in values
        ):
            raise DockCardGridRegistrationError(
                "Зарегистрированная строка выходит за supported card scan area."
            )
        return values

    def _recover_preceding_visible_row(
        self,
        frame: np.ndarray,
        accepted: Sequence[int],
        *,
        height: int,
    ) -> tuple[int, ...]:
        """Recover one top row only from proven grid phase plus presence evidence."""
        values = tuple(accepted)
        if len(values) < 2:
            return values
        if abs((values[1] - values[0]) - self.ROW_DELTA) > self.ROW_SPACING_TOLERANCE:
            return values

        inferred = values[0] - self.ROW_DELTA
        safe_bottom = height - self.SAFE_BOTTOM_MARGIN
        if inferred < self.SAFE_SCAN_TOP or inferred + self.CARD_HEIGHT > safe_bottom:
            return values

        states = tuple(
            slot.presence for slot in self._scan_row(frame, 0, inferred)
        )
        if DockCardPresence.PRESENT not in states:
            return values
        return (inferred, *values)

    def register_rows(self, frame: np.ndarray) -> tuple[int, ...]:
        """Return structurally proven, fully visible card-row origins."""
        height, _ = self._validate_frame(frame)
        safe_bottom = height - self.SAFE_BOTTOM_MARGIN
        candidates = self._snap_candidate_origins(self._candidate_row_origins(frame))
        full_candidates = tuple(
            origin
            for origin in candidates
            if origin >= self.SAFE_SCAN_TOP and origin + self.CARD_HEIGHT <= safe_bottom
        )
        accepted: list[int] = []
        for origin in full_candidates:
            row_slots = self._scan_row(frame, 0, origin)
            states = tuple(slot.presence for slot in row_slots)
            if DockCardPresence.PRESENT in states:
                accepted.append(origin)
                continue
            if DockCardPresence.UNKNOWN in states:
                raise DockCardGridRegistrationError(
                    f"Структура candidate row неоднозначна: origin={origin}."
                )
            # An all-ABSENT candidate is background, not a registered card row.
        recovered = self._recover_preceding_visible_row(
            frame,
            accepted,
            height=height,
        )
        return self._validate_row_origins(recovered, height=height)

    def scan_viewport(self, viewport: DockTraversalViewport) -> DockViewportCardScan:
        """Scan only ``viewport.frame``; no screenshot, click, or scroll occurs."""
        if not isinstance(viewport, DockTraversalViewport):
            raise TypeError("viewport должен быть DockTraversalViewport")
        before = viewport.frame.copy()
        origins = self.register_rows(viewport.frame)
        slots = tuple(
            slot
            for row_index, origin in enumerate(origins)
            for slot in self._scan_row(viewport.frame, row_index, origin)
        )
        if not np.array_equal(viewport.frame, before):
            raise DockCardGridFrameError("Card-grid scanner мутировал входной frame.")
        result = DockViewportCardScan(
            viewport_index=viewport.index,
            scroll_position=float(viewport.scroll_position),
            registered_row_origins=origins,
            slots=slots,
        )
        logger.info(
            "[Инвентарь дока] окно=%s позиция=%.6f строки=%s "
            "занято=%s пусто=%s неизвестно=%s",
            result.viewport_index,
            result.scroll_position,
            result.registered_row_origins,
            result.present_count,
            result.absent_count,
            result.unknown_count,
        )
        return result


class DockCardGridCollector:
    """Stage 2 visitor that preserves every per-viewport Stage 3 result."""

    def __init__(self, scanner: DockCardGridScanner | None = None) -> None:
        self.scanner = DockCardGridScanner() if scanner is None else scanner
        self._viewports: list[DockViewportCardScan] = []

    def __call__(self, viewport: DockTraversalViewport) -> None:
        self._viewports.append(self.scanner.scan_viewport(viewport))

    @property
    def viewports(self) -> tuple[DockViewportCardScan, ...]:
        return tuple(self._viewports)


def scan_dock_card_layouts(
    navigator: DockInventoryNavigator,
    *,
    scanner: DockCardGridScanner | None = None,
    run_stage2_kwargs: dict[str, object] | None = None,
) -> DockCardLayoutScanResult:
    """Reuse the Stage 2 prerequisite/navigation/traversal/cleanup workflow."""
    if not isinstance(navigator, DockInventoryNavigator):
        raise TypeError("navigator должен быть DockInventoryNavigator")
    collector = DockCardGridCollector(scanner)
    stage2: DockInventoryStage2Result = navigator.run_stage2(
        collector,
        **({} if run_stage2_kwargs is None else run_stage2_kwargs),
    )
    return DockCardLayoutScanResult(
        prerequisite=stage2.prerequisite,
        traversal=stage2.traversal,
        viewports=collector.viewports,
    )