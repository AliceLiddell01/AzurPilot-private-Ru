"""Контроллер Dorm по состояниям для сканирования обоих этажей управления morale."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import cv2
import numpy as np

from module.base.button import Button
from module.base.decorator import cached_property
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormMoraleScanResult,
)
from module.dorm.morale_scanner import DormMoraleInputError, DormMoraleScanner
from module.ui.page import page_dorm
from module.ui.ui import UI


class DormMoraleControllerError(RuntimeError):
    """UI Train Dorm не достиг подтверждённого состояния."""


@dataclass(frozen=True, slots=True)
class DormTrainLayout:
    frame_width: int = 1280
    frame_height: int = 720
    # В live UI вкладка Train показывает персонажей 1F, Rest — персонажей 2F.
    floor_1_probe: tuple[int, int, int, int] = (145, 90, 330, 120)
    floor_2_probe: tuple[int, int, int, int] = (360, 90, 545, 120)
    train_modal_probe: tuple[int, int, int, int] = (555, 40, 900, 110)
    dorm_home_header_probe: tuple[int, int, int, int] = (830, 20, 1260, 130)
    floor_1_button: tuple[int, int, int, int] = (134, 85, 347, 137)
    floor_2_button: tuple[int, int, int, int] = (347, 85, 561, 137)
    train_button: tuple[int, int, int, int] = (20, 640, 230, 719)
    close_button: tuple[int, int, int, int] = (1110, 65, 1160, 120)
    # Реальные пять Train cards из EN UI. Клик выполняется только по уже
    # наблюдаемому occupant и лишь открывает replacement-selection modal.
    train_card_buttons: tuple[tuple[int, int, int, int], ...] = (
        (141, 205, 299, 535),
        (311, 205, 469, 535),
        (481, 205, 639, 535),
        (651, 205, 809, 535),
        (821, 205, 979, 535),
    )


@dataclass(frozen=True, slots=True)
class DormTrainStatePolicy:
    dorm_home_luma_min: float = 170.0
    dorm_home_light_ratio_min: float = 0.55
    train_modal_dark_luma_max: float = 100.0
    train_modal_dark_ratio_min: float = 0.5
    selected_luma_min: float = 180.0
    unselected_luma_max: float = 140.0
    selected_delta_min: float = 60.0


class DormTrainStateDetector:
    def __init__(
        self,
        layout: DormTrainLayout | None = None,
        policy: DormTrainStatePolicy | None = None,
    ) -> None:
        self.layout = DormTrainLayout() if layout is None else layout
        self.policy = DormTrainStatePolicy() if policy is None else policy

    def _normalize(self, frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
            raise DormMoraleInputError("Контроллер Dorm ожидает кадр 1280x720.")
        height, width = frame.shape[:2]
        if (
            width * self.layout.frame_height != height * self.layout.frame_width
            or width < self.layout.frame_width
            or height < self.layout.frame_height
        ):
            raise DormMoraleInputError(
                "Контроллер Dorm ожидает кадр 16:9 не меньше 1280x720."
            )
        if (height, width) == (self.layout.frame_height, self.layout.frame_width):
            return frame
        return cv2.resize(
            frame,
            (self.layout.frame_width, self.layout.frame_height),
            interpolation=cv2.INTER_AREA,
        )

    @staticmethod
    def _mean_luma(frame: np.ndarray, area: tuple[int, int, int, int]) -> float:
        x1, y1, x2, y2 = area
        return float(np.mean(cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)))

    @staticmethod
    def _light_ratio(frame: np.ndarray, area: tuple[int, int, int, int]) -> float:
        x1, y1, x2, y2 = area
        luma = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
        return float(np.mean(luma >= 180))

    def train_modal_visible(self, frame: np.ndarray) -> bool:
        frame = self._normalize(frame)
        x1, y1, x2, y2 = self.layout.train_modal_probe
        luma = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
        dark_ratio = float(np.mean(luma <= self.policy.train_modal_dark_luma_max))
        return dark_ratio >= self.policy.train_modal_dark_ratio_min

    def dorm_home_visible(self, frame: np.ndarray) -> bool:
        """Подтверждает Home Dorm по Train, а не по кнопке редактора Move."""
        frame = self._normalize(frame)
        if self.train_modal_visible(frame):
            return False
        for area in (self.layout.train_button, self.layout.dorm_home_header_probe):
            if (
                self._mean_luma(frame, area) < self.policy.dorm_home_luma_min
                or self._light_ratio(frame, area)
                < self.policy.dorm_home_light_ratio_min
            ):
                return False
        return True

    def selected_floor(self, frame: np.ndarray) -> DormFloor | None:
        frame = self._normalize(frame)
        if not self.train_modal_visible(frame):
            return None
        floor_1 = self._mean_luma(frame, self.layout.floor_1_probe)
        floor_2 = self._mean_luma(frame, self.layout.floor_2_probe)
        if (
            floor_1 >= self.policy.selected_luma_min
            and floor_2 <= self.policy.unselected_luma_max
            and floor_1 - floor_2 >= self.policy.selected_delta_min
        ):
            return DormFloor.FLOOR_1
        if (
            floor_2 >= self.policy.selected_luma_min
            and floor_1 <= self.policy.unselected_luma_max
            and floor_2 - floor_1 >= self.policy.selected_delta_min
        ):
            return DormFloor.FLOOR_2
        return None


class DormMoraleController(UI):
    """Открыть Train Dorm и выполнить ограниченный скан 1F -> 2F."""

    def __init__(
        self,
        config,
        device=None,
        *,
        scanner: DormMoraleScanner | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        super().__init__(config, device=device)
        self._scanner = scanner
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    @cached_property
    def dorm_train_layout(self) -> DormTrainLayout:
        return DormTrainLayout()

    @cached_property
    def dorm_train_state(self) -> DormTrainStateDetector:
        return DormTrainStateDetector(self.dorm_train_layout)

    @cached_property
    def dorm_morale_scanner(self) -> DormMoraleScanner:
        return self._scanner or DormMoraleScanner()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DormMoraleControllerError(
                "Часы контроллера должны возвращать datetime с часовым поясом."
            )
        return value

    def _current_frame(self) -> np.ndarray:
        frame = self.device.image
        if not isinstance(frame, np.ndarray):
            raise DormMoraleControllerError("Device не содержит снимок экрана Dorm.")
        return frame

    def _capture(self) -> np.ndarray:
        self.device.screenshot()
        return self._current_frame()

    @staticmethod
    def _button(area: tuple[int, int, int, int], name: str) -> Button:
        return Button(area=(), color=(), button=area, name=name)

    @staticmethod
    def _error_code(error: BaseException) -> str:
        value = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
        return value[:64] or "dorm_scan_failed"

    def _open_train(self) -> np.ndarray:
        frame = self._capture()
        if self.dorm_train_state.selected_floor(frame) is not None:
            return frame
        if not self.dorm_train_state.dorm_home_visible(frame) and not self.ui_page_appear(
            page_dorm,
            offset=(20, 20),
        ):
            self.ui_ensure(page_dorm)
            frame = self._current_frame()
        train_requested = False
        for attempt in range(20):
            if self.dorm_train_state.selected_floor(frame) is not None:
                return frame
            home_visible = self.dorm_train_state.dorm_home_visible(frame)
            if (
                (home_visible or self.ui_page_appear(page_dorm, offset=(20, 20)))
                and (not train_requested or (home_visible and attempt % 5 == 0))
            ):
                self.device.click(
                    self._button(
                        self.dorm_train_layout.train_button,
                        "DORM_MORALE_TRAIN",
                    )
                )
                train_requested = True
                frame = self._capture()
                continue
            if self.ui_additional(get_ship=False):
                frame = self._capture()
                continue
            if train_requested:
                frame = self._capture()
                continue
            raise DormMoraleControllerError(
                "Экран Train Dorm не открыт, текущее состояние UI не распознано."
            )
        raise DormMoraleControllerError(
            "Истёк лимит ожидания экрана Train Dorm."
        )

    def _select_floor(self, frame: np.ndarray, floor: DormFloor) -> np.ndarray:
        switch_requested = False
        for attempt in range(10):
            selected = self.dorm_train_state.selected_floor(frame)
            if selected is floor:
                return frame
            if selected is None:
                if self.ui_additional(get_ship=False):
                    frame = self._capture()
                    continue
                if switch_requested:
                    frame = self._capture()
                    continue
                raise DormMoraleControllerError(
                    "Состояние этажа Train Dorm не распознано."
                )
            if switch_requested and attempt % 4 != 0:
                frame = self._capture()
                continue
            area = (
                self.dorm_train_layout.floor_1_button
                if floor is DormFloor.FLOOR_1
                else self.dorm_train_layout.floor_2_button
            )
            self.device.click(self._button(area, f"DORM_MORALE_{floor.value}"))
            switch_requested = True
            frame = self._capture()
        raise DormMoraleControllerError(f"Не удалось выбрать этаж Dorm {floor.value}.")

    def close_train(self) -> np.ndarray:
        """Закрыть Train/Rest только по доказанному modal state."""

        frame = self._capture()
        close_requested = False
        for attempt in range(15):
            if self.dorm_train_state.dorm_home_visible(frame):
                return frame
            modal_visible = self.dorm_train_state.selected_floor(frame) is not None
            if modal_visible and (
                not close_requested or attempt % 5 == 0
            ):
                self.device.click(
                    self._button(
                        self.dorm_train_layout.close_button,
                        "DORM_MORALE_CLOSE",
                    )
                )
                close_requested = True
                frame = self._capture()
                continue
            if close_requested:
                frame = self._capture()
                continue
            raise DormMoraleControllerError(
                "Безопасный выход из Train Dorm не доказан."
            )
        raise DormMoraleControllerError(
            "Истёк лимит ожидания закрытия Train Dorm."
        )

    def open_candidate_selection(self, scan: DormMoraleScanResult) -> np.ndarray:
        """Открыть replacement-selection через уже наблюдаемого Train occupant.

        Метод намеренно не нажимает REMOVE, result card, Cancel или Confirm.
        Единственное действие после доказанного Train state — tap по существующему
        occupant, что в подтверждённом EN UI открывает read-only для нашей задачи
        candidate-selection до тех пор, пока Confirm не используется.
        """

        if not isinstance(scan, DormMoraleScanResult):
            raise TypeError("scan должен быть DormMoraleScanResult")
        floor_1 = next(
            (attempt for attempt in scan.attempts if attempt.floor is DormFloor.FLOOR_1),
            None,
        )
        if (
            floor_1 is None
            or floor_1.status is not DormFloorScanStatus.SUCCEEDED
            or floor_1.snapshot is None
            or not floor_1.snapshot.observations
        ):
            raise DormMoraleControllerError(
                "Targeted Search требует хотя бы одного доказанного Train occupant."
            )
        ordinal = floor_1.snapshot.observations[0].ordinal
        if type(ordinal) is not int or not 0 <= ordinal < len(self.dorm_train_layout.train_card_buttons):
            raise DormMoraleControllerError(
                "Ordinal Train occupant не соответствует подтверждённой геометрии 1F."
            )

        frame = self._open_train()
        frame = self._select_floor(frame, DormFloor.FLOOR_1)
        if self.dorm_train_state.selected_floor(frame) is not DormFloor.FLOOR_1:
            raise DormMoraleControllerError(
                "Train 1F не доказан перед открытием candidate-selection."
            )

        from module.retire.assets import DOCK_CHECK

        button = self._button(
            self.dorm_train_layout.train_card_buttons[ordinal],
            "DORM_MORALE_EXISTING_TRAIN_OCCUPANT",
        )
        for attempt in range(8):
            if self.appear(DOCK_CHECK, offset=(20, 20)):
                return frame
            if self.dorm_train_state.selected_floor(frame) is DormFloor.FLOOR_1:
                if attempt in {0, 4}:
                    self.device.click(button)
                frame = self._capture()
                continue
            raise DormMoraleControllerError(
                "После tap Train occupant получено неожиданное состояние UI."
            )
        raise DormMoraleControllerError(
            "Candidate-selection не открылся за ограниченное число попыток."
        )

    def _scan_floor(
        self,
        frame: np.ndarray,
        floor: DormFloor,
    ) -> tuple[np.ndarray, DormFloorScanAttempt]:
        frame = self._select_floor(frame, floor)
        fresh = self._capture()
        if self.dorm_train_state.selected_floor(fresh) is not floor:
            raise DormMoraleControllerError(
                f"Этаж Dorm {floor.value} не подтверждён на свежем снимке экрана."
            )
        observed_at = self._now()
        snapshot = self.dorm_morale_scanner.scan(fresh.copy(), floor=floor)
        return fresh, DormFloorScanAttempt(
            floor=floor,
            status=DormFloorScanStatus.SUCCEEDED,
            observed_at=observed_at,
            snapshot=snapshot,
        )

    def scan_both_floors(self, *, source: str) -> DormMoraleScanResult:
        if not isinstance(source, str) or not source.strip() or len(source) > 64:
            raise ValueError(
                "source должен быть непустой строкой длиной до 64 символов"
            )
        scan_id = self._id_factory()
        started_at = self._now()
        attempts: list[DormFloorScanAttempt] = []
        try:
            frame = self._open_train()
            frame, floor_1 = self._scan_floor(frame, DormFloor.FLOOR_1)
            attempts.append(floor_1)
        except Exception as error:  # noqa: BLE001 - результат хранит физический этап.
            attempts.extend(
                (
                    DormFloorScanAttempt(
                        floor=DormFloor.FLOOR_1,
                        status=DormFloorScanStatus.FAILED,
                        error_code=self._error_code(error),
                    ),
                    DormFloorScanAttempt(
                        floor=DormFloor.FLOOR_2,
                        status=DormFloorScanStatus.FAILED,
                        error_code="not_attempted_after_floor_1_failure",
                    ),
                )
            )
            finished_at = self._now()
            return DormMoraleScanResult(
                id=scan_id,
                started_at=started_at,
                finished_at=finished_at,
                attempts=tuple(attempts),
                source=source,
                idempotency_key=f"dorm-morale-scan-v1:{scan_id}",
            )

        try:
            _frame, floor_2 = self._scan_floor(frame, DormFloor.FLOOR_2)
            attempts.append(floor_2)
        except Exception as error:  # noqa: BLE001 - частичный результат хранится как явное доказательство.
            attempts.append(
                DormFloorScanAttempt(
                    floor=DormFloor.FLOOR_2,
                    status=DormFloorScanStatus.FAILED,
                    error_code=self._error_code(error),
                )
            )
        finished_at = self._now()
        return DormMoraleScanResult(
            id=scan_id,
            started_at=started_at,
            finished_at=finished_at,
            attempts=tuple(attempts),
            source=source,
            idempotency_key=f"dorm-morale-scan-v1:{scan_id}",
        )


__all__ = (
    "DormMoraleController",
    "DormMoraleControllerError",
    "DormTrainLayout",
    "DormTrainStateDetector",
    "DormTrainStatePolicy",
)
