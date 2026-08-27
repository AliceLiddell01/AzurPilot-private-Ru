"""Контроллер Dorm по состояниям для сканирования обоих этажей управления morale."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import cv2
import numpy as np

from module.base.button import Button
from module.config.config import AzurLaneConfig
from module.device.device import Device
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormMoraleScanResult,
)
from module.dorm.morale_scanner import DormMoraleInputError, DormMoraleScanner
from module.dorm.morale_ui_layout import DormManageLayout
from module.ui.ui import UI


class DormMoraleControllerError(RuntimeError):
    """UI управления Dorm не достиг подтверждённого состояния."""


@dataclass(frozen=True, slots=True)
class DormManageStateDetector:
    """Определить открытое управление Dorm и выбранный этаж по состоянию кнопок."""

    layout: DormManageLayout = DormManageLayout()

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
    def _mean_bgr(frame: np.ndarray, area: tuple[int, int, int, int]) -> np.ndarray:
        x1, y1, x2, y2 = area
        return frame[y1:y2, x1:x2].mean(axis=(0, 1))

    def _selected(self, frame: np.ndarray, area: tuple[int, int, int, int]) -> bool:
        mean = self._mean_bgr(frame, area)
        # Выбранная кнопка этажа использует яркий голубой акцент, а невыбранная — серый.
        return bool(mean[0] > 125 and mean[1] > 105 and mean[0] - mean[2] > 20)

    def manage_opened(self, frame: np.ndarray) -> bool:
        normalized = self._normalize(frame)
        first = self._mean_bgr(normalized, self.layout.floor_1_state_probe)
        second = self._mean_bgr(normalized, self.layout.floor_2_state_probe)
        return bool(
            first.mean() > 60
            and second.mean() > 60
            and abs(float(first.mean()) - float(second.mean())) < 90
        )

    def selected_floor(self, frame: np.ndarray) -> DormFloor | None:
        normalized = self._normalize(frame)
        floor_1 = self._selected(normalized, self.layout.floor_1_state_probe)
        floor_2 = self._selected(normalized, self.layout.floor_2_state_probe)
        if floor_1 == floor_2:
            return None
        return DormFloor.FLOOR_1 if floor_1 else DormFloor.FLOOR_2


class DormMoraleController(UI):
    """Открыть управление Dorm и выполнить ограниченный скан 1F -> 2F."""

    def __init__(
        self,
        config: AzurLaneConfig,
        device: Device | None = None,
        *,
        scanner: DormMoraleScanner | None = None,
        state_detector: DormManageStateDetector | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(config, device=device)
        self.dorm_morale_scanner = scanner or DormMoraleScanner()
        self.dorm_manage_state = state_detector or DormManageStateDetector()
        self.dorm_manage_layout = self.dorm_manage_state.layout
        self._clock = clock or (lambda: datetime.now(UTC))

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
        return Button(area=area, color=(), button=area, name=name)

    def _open_manage(self) -> np.ndarray:
        frame = self._capture()
        for _ in range(10):
            if self.dorm_manage_state.manage_opened(frame):
                return frame
            # Состояние кнопки DORM_MANAGE подтверждается на текущем снимке перед кликом.
            if self.appear(self.dorm_manage_layout.manage_button, offset=(20, 20)):
                self.device.click(self.dorm_manage_layout.manage_button)
                frame = self._capture()
                continue
            raise DormMoraleControllerError(
                "Управление Dorm не открыто, текущее состояние UI не распознано."
            )
        raise DormMoraleControllerError("Истёк лимит открытия управления Dorm.")

    def _select_floor(self, frame: np.ndarray, floor: DormFloor) -> np.ndarray:
        for _ in range(10):
            selected = self.dorm_manage_state.selected_floor(frame)
            if selected is floor:
                return frame
            if selected is None:
                # При неопределённом состоянии сначала запрашиваем новый снимок без клика.
                frame = self._capture()
                if self.dorm_manage_state.selected_floor(frame) is None:
                    raise DormMoraleControllerError(
                        "Состояние этажа управления Dorm не распознано."
                    )
                continue
            area = (
                self.dorm_manage_layout.floor_1_button
                if floor is DormFloor.FLOOR_1
                else self.dorm_manage_layout.floor_2_button
            )
            self.device.click(self._button(area, f"DORM_MORALE_{floor.value}"))
            frame = self._capture()
        raise DormMoraleControllerError(f"Не удалось выбрать этаж Dorm {floor.value}.")

    def _scan_floor(
        self,
        frame: np.ndarray,
        floor: DormFloor,
    ) -> tuple[np.ndarray, DormFloorScanAttempt]:
        frame = self._select_floor(frame, floor)
        fresh = self._capture()
        if self.dorm_manage_state.selected_floor(fresh) is not floor:
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

    @staticmethod
    def _error_code(error: BaseException) -> str:
        name = type(error).__name__
        if len(name) > 64:
            return name[:64]
        return name

    def scan_both_floors(self, *, source: str) -> DormMoraleScanResult:
        started_at = self._now()
        scan_id = uuid4()
        attempts: list[DormFloorScanAttempt] = []
        try:
            frame = self._open_manage()
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
                        status=DormFloorScanStatus.SKIPPED,
                        error_code="previous_floor_failed",
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


__all__ = [
    "DormManageStateDetector",
    "DormMoraleController",
    "DormMoraleControllerError",
]
