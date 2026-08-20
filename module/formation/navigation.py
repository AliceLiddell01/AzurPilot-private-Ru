"""Навигация по Formation и выбор игрового флота для сканирования."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from module.base.button import Button
from module.base.decorator import cached_property
from module.exception import GameStuckError
from module.formation.model import FormationFleetSnapshot
from module.formation.scanner import FormationFleetInfoScanner, FormationFleetInputError
from module.logger import logger
from module.ocr.ocr import Digit
from module.ui.page import page_fleet
from module.ui.ui import UI


@dataclass(frozen=True, slots=True)
class FormationNavigationLayout:
    """Геометрия интерактивных элементов Formation в кадре 1280x720."""

    frame_width: int = 1280
    frame_height: int = 720
    surface_fleet_select: tuple[int, int, int, int] = (128, 648, 355, 706)
    fleet_rows_top_to_bottom: tuple[tuple[int, int, int, int], ...] = (
        (151, 317, 354, 359),
        (151, 371, 354, 413),
        (151, 425, 354, 467),
        (151, 478, 354, 520),
        (151, 532, 354, 574),
        (151, 586, 354, 628),
    )
    fleet_menu_probes: tuple[tuple[int, int, int, int], ...] = (
        (300, 322, 345, 352),
        (300, 376, 345, 406),
        (300, 430, 345, 460),
        (300, 483, 345, 513),
        (300, 537, 345, 567),
        (300, 591, 345, 621),
    )
    fleet_index_area: tuple[int, int, int, int] = (950, 115, 990, 165)
    info_button: tuple[int, int, int, int] = (896, 639, 1015, 708)
    info_state_probe: tuple[int, int, int, int] = (920, 650, 980, 690)
    formation_button: tuple[int, int, int, int] = (1036, 639, 1245, 708)

    def __post_init__(self) -> None:
        if len(self.fleet_rows_top_to_bottom) != 6:
            raise ValueError("Formation fleet menu должен содержать шесть строк")
        if len(self.fleet_menu_probes) != 6:
            raise ValueError("Formation fleet menu должен содержать шесть probe-областей")
        for area in (
            self.surface_fleet_select,
            *self.fleet_rows_top_to_bottom,
            *self.fleet_menu_probes,
            self.fleet_index_area,
            self.info_button,
            self.info_state_probe,
            self.formation_button,
        ):
            x1, y1, x2, y2 = area
            if not (0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height):
                raise ValueError(f"Formation navigation area выходит за frame: {area!r}")

    def fleet_row(self, fleet_index: int) -> tuple[int, int, int, int]:
        if type(fleet_index) is not int or not 1 <= fleet_index <= 6:
            raise ValueError("fleet_index должен быть int в диапазоне 1..6")
        return self.fleet_rows_top_to_bottom[6 - fleet_index]


GLOBAL_FORMATION_NAVIGATION_LAYOUT_1280_720 = FormationNavigationLayout()


@dataclass(frozen=True, slots=True)
class FormationStatePolicy:
    """Пороговые правила распознавания menu/info без template assets."""

    menu_gray_saturation_max: float = 55.0
    menu_gray_value_min: float = 175.0
    menu_required_gray_rows: int = 4
    info_orange_hue_min: int = 5
    info_orange_hue_max: int = 25
    info_orange_saturation_min: int = 70
    info_orange_value_min: int = 170
    info_orange_ratio_min: float = 0.50


class FormationUiStateDetector:
    """Распознаёт устойчивые состояния Formation по нескольким независимым ROI."""

    def __init__(
        self,
        layout: FormationNavigationLayout = GLOBAL_FORMATION_NAVIGATION_LAYOUT_1280_720,
        policy: FormationStatePolicy = FormationStatePolicy(),
    ) -> None:
        self.layout = layout
        self.policy = policy

    def _validate_frame(self, frame: np.ndarray) -> None:
        if not isinstance(frame, np.ndarray) or frame.shape != (
            self.layout.frame_height,
            self.layout.frame_width,
            3,
        ):
            raise FormationFleetInputError(
                "Formation navigation ожидает цветной frame 1280x720."
            )

    @staticmethod
    def _crop(frame: np.ndarray, area: tuple[int, int, int, int]) -> np.ndarray:
        x1, y1, x2, y2 = area
        return frame[y1:y2, x1:x2]

    def fleet_menu_opened(self, frame: np.ndarray) -> bool:
        self._validate_frame(frame)
        gray_rows = 0
        for area in self.layout.fleet_menu_probes:
            hsv = cv2.cvtColor(self._crop(frame, area), cv2.COLOR_BGR2HSV)
            saturation = float(np.mean(hsv[:, :, 1]))
            value = float(np.mean(hsv[:, :, 2]))
            if (
                saturation <= self.policy.menu_gray_saturation_max
                and value >= self.policy.menu_gray_value_min
            ):
                gray_rows += 1
        return gray_rows >= self.policy.menu_required_gray_rows

    def info_opened(self, frame: np.ndarray) -> bool:
        self._validate_frame(frame)
        hsv = cv2.cvtColor(
            self._crop(frame, self.layout.info_state_probe),
            cv2.COLOR_BGR2HSV,
        )
        orange = (
            (hsv[:, :, 0] >= self.policy.info_orange_hue_min)
            & (hsv[:, :, 0] <= self.policy.info_orange_hue_max)
            & (hsv[:, :, 1] >= self.policy.info_orange_saturation_min)
            & (hsv[:, :, 2] >= self.policy.info_orange_value_min)
        )
        return float(np.mean(orange)) >= self.policy.info_orange_ratio_min


class _FleetIndexModel(Protocol):
    def ocr(self, frame: np.ndarray): ...


class FormationFleetIndexOcr:
    """OCR синего номера текущего Surface Fleet с domain validation 1..6."""

    def __init__(
        self,
        layout: FormationNavigationLayout = GLOBAL_FORMATION_NAVIGATION_LAYOUT_1280_720,
        *,
        model: _FleetIndexModel | None = None,
    ) -> None:
        self.layout = layout
        self.model = (
            Digit(
                layout.fleet_index_area,
                lang="azur_lane",
                letter=(94, 155, 255),
                threshold=96,
                name="FORMATION_FLEET_INDEX",
            )
            if model is None
            else model
        )

    def read(self, frame: np.ndarray) -> int | None:
        value = self.model.ocr(frame)
        if type(value) is int and 1 <= value <= 6:
            return value
        return None


class FormationFleetController(UI):
    """State-machine `Main -> Formation -> Fleet N -> Info -> snapshot`."""

    @cached_property
    def formation_navigation_layout(self) -> FormationNavigationLayout:
        return GLOBAL_FORMATION_NAVIGATION_LAYOUT_1280_720

    @cached_property
    def formation_state(self) -> FormationUiStateDetector:
        return FormationUiStateDetector(self.formation_navigation_layout)

    @cached_property
    def formation_fleet_index_ocr(self) -> FormationFleetIndexOcr:
        return FormationFleetIndexOcr(self.formation_navigation_layout)

    @cached_property
    def formation_fleet_scanner(self) -> FormationFleetInfoScanner:
        return FormationFleetInfoScanner()

    @staticmethod
    def _click_button(
        area: tuple[int, int, int, int],
        name: str,
    ) -> Button:
        return Button(area=(), color=(), button=area, name=name)

    def _current_frame(self) -> np.ndarray:
        frame = self.device.image
        if not isinstance(frame, np.ndarray):
            raise FormationFleetInputError("Device не содержит Formation screenshot.")
        return frame

    def _close_info(self) -> None:
        for _ in self.loop(timeout=20):
            frame = self._current_frame()
            if not self.formation_state.info_opened(frame):
                if self.ui_page_appear(page_fleet, offset=(20, 20)):
                    return
                continue
            self.device.click(
                self._click_button(
                    self.formation_navigation_layout.formation_button,
                    "FORMATION_CLOSE_INFO",
                )
            )
            continue
        raise GameStuckError("[Построение — сканер] Не удалось закрыть Formation Info")

    def ensure_formation_page(self) -> None:
        self.device.screenshot()
        if self.formation_state.info_opened(self._current_frame()):
            self._close_info()
        self.ui_ensure(page_fleet, skip_first_screenshot=True)

    def ensure_surface_fleet(self, fleet_index: int) -> None:
        if type(fleet_index) is not int or not 1 <= fleet_index <= 6:
            raise FormationFleetInputError("fleet_index должен быть int в диапазоне 1..6")

        for _ in self.loop(skip_first=False, timeout=20):
            frame = self._current_frame()
            if self.formation_state.info_opened(frame):
                raise FormationFleetInputError(
                    "Нельзя выбирать Surface Fleet при открытом Formation Info."
                )

            if self.formation_state.fleet_menu_opened(frame):
                self.device.click(
                    self._click_button(
                        self.formation_navigation_layout.fleet_row(fleet_index),
                        f"FORMATION_SELECT_FLEET_{fleet_index}",
                    )
                )
                continue

            current = self.formation_fleet_index_ocr.read(frame)
            if current == fleet_index:
                logger.info(
                    f"[Построение — сканер] Выбран Surface Fleet {fleet_index}"
                )
                return
            if current is None:
                continue

            self.device.click(
                self._click_button(
                    self.formation_navigation_layout.surface_fleet_select,
                    "FORMATION_OPEN_FLEET_MENU",
                )
            )
            continue

        raise GameStuckError(
            f"[Построение — сканер] Не удалось выбрать Surface Fleet {fleet_index}"
        )

    def _open_info(self) -> None:
        for _ in self.loop(skip_first=False, timeout=20):
            frame = self._current_frame()
            if self.formation_state.info_opened(frame):
                return
            if self.formation_state.fleet_menu_opened(frame):
                continue
            if not self.ui_page_appear(page_fleet, offset=(20, 20)):
                continue

            self.device.click(
                self._click_button(
                    self.formation_navigation_layout.info_button,
                    "FORMATION_OPEN_INFO",
                )
            )
            continue

        raise GameStuckError("[Построение — сканер] Не удалось открыть Formation Info")

    def scan_surface_fleet(
        self,
        fleet_index: int,
        *,
        close_info: bool = True,
    ) -> FormationFleetSnapshot:
        """Получить фактический состав одного Surface Fleet 1..6."""

        self.ensure_formation_page()
        self.ensure_surface_fleet(fleet_index)
        self._open_info()

        try:
            self.device.screenshot()
            frame = self._current_frame()
            if not self.formation_state.info_opened(frame):
                raise FormationFleetInputError(
                    "Formation Info исчез до начала сканирования состава."
                )
            result = self.formation_fleet_scanner.scan(
                frame.copy(),
                fleet_index=fleet_index,
            )
            logger.info(
                f"[Построение — сканер] Fleet {fleet_index}: "
                f"занято {result.occupied_count}/6, "
                f"полное сопоставление: {result.complete}"
            )
            return result
        finally:
            if close_info:
                self._close_info()
