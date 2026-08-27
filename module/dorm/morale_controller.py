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
from module.dorm.assets import OCR_DORM_SLOT
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
    floor_1_button: tuple[int, int, int, int] = (134, 85, 347, 137)
    floor_2_button: tuple[int, int, int, int] = (347, 85, 561, 137)


@dataclass(frozen=True, slots=True)
class DormTrainStatePolicy:
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

    def selected_floor(self, frame: np.ndarray) -> DormFloor | None:
        frame = self._normalize(frame)
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
        self.ui_ensure(page_dorm)
        frame = self._capture()
        train_requested = False
        for _ in range(20):
            if self.dorm_train_state.selected_floor(frame) is not None:
                return frame
            if not train_requested and self.ui_page_appear(
                page_dorm,
                offset=(20, 20),
            ):
                self.device.click(OCR_DORM_SLOT)
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
        for _ in range(10):
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
            if switch_requested:
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
    "DormTrainLayout",
    "DormTrainStateDetector",
    "DormTrainStatePolicy",
    "DormMoraleController",
    "DormMoraleControllerError",
)
