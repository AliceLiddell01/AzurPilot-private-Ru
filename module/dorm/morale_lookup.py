"""Точечный read-only lookup morale через Train candidate-selection Search."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import cv2
import numpy as np

from module.application.morale_reconciliation import TargetedMoraleLookupTarget
from module.base.button import Button
from module.base.decorator import cached_property
from module.base.utils import extract_letters
from module.dock_inventory.catalog import (
    DockIdentityCatalog,
    load_dock_identity_catalog,
)
from module.dock_inventory.identity import DockShipIdentityResolver, DockShipNameOcr
from module.dock_inventory.model import IdentityStatus
from module.dorm.morale_scanner import DormMoraleValueOcr
from module.ocr.ocr import Ocr
from module.retire.dock import CARD_GRIDS
from module.ui.page import page_main
from module.ui.ui import UI


class TargetedMoraleLookupError(RuntimeError):
    """Search lookup не смог получить безопасное однозначное evidence."""

    def __init__(self, error_code: str, message: str) -> None:
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
            raise ValueError("error_code targeted lookup имеет неверный формат")
        super().__init__(message)
        self.error_code = error_code


class TargetedMoraleLocationHint(StrEnum):
    OUTSIDE_DORM = "outside_dorm"
    TRAIN = "train"
    REST = "rest"


@dataclass(frozen=True, slots=True)
class TargetedMoraleCardGeometry:
    row: int
    column: int
    area: tuple[int, int, int, int]
    morale_area: tuple[int, int, int, int]
    fleet_badge_area: tuple[int, int, int, int]
    state_area: tuple[int, int, int, int]
    presence_area: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class TargetedMoraleLookupLayout:
    """Геометрия подтверждённого EN candidate-selection UI в canonical 1280x720."""

    frame_width: int = 1280
    frame_height: int = 720
    search_button: tuple[int, int, int, int] = (648, 3, 704, 51)
    search_input: tuple[int, int, int, int] = (720, 8, 970, 46)
    home_button: tuple[int, int, int, int] = (1206, 0, 1279, 72)
    search_hue_min: int = 8
    search_hue_max: int = 38
    search_saturation_min: int = 70
    search_value_min: int = 100
    search_gold_ratio_min: float = 0.08

    def cards(self) -> tuple[TargetedMoraleCardGeometry, ...]:
        origin_x, origin_y = CARD_GRIDS.origin
        delta_x, delta_y = CARD_GRIDS.delta
        width, height = CARD_GRIDS.button_shape
        columns, rows = CARD_GRIDS.grid_shape
        result = []
        for row in range(int(rows)):
            for column in range(int(columns)):
                # Верхний левый slot selection UI занят безопасным REMOVE и не
                # является кораблём. Его никогда не рассматриваем и не нажимаем.
                if row == 0 and column == 0:
                    continue
                x1 = int(round(origin_x + column * delta_x))
                y1 = int(round(origin_y + row * delta_y))
                x2 = x1 + int(width)
                y2 = y1 + int(height)
                result.append(
                    TargetedMoraleCardGeometry(
                        row=row,
                        column=column,
                        area=(x1, y1, x2, y2),
                        # Совпадает с проверенной CARD_EMOTION_GRIDS geometry:
                        # только белые цифры, без зелёной иконки и светлых частей арта.
                        morale_area=(x1 + 23, y1 + 29, x1 + 48, y1 + 52),
                        fleet_badge_area=(x1 - 8, y1 + 108, x1 + 48, y1 + 160),
                        state_area=(x1 - 8, y1 + 62, x1 + 146, y1 + 139),
                        presence_area=(x1 - 8, y1 + 20, x1 + 27, y1 + 58),
                    )
                )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class TargetedMoraleLookupObservation:
    target: TargetedMoraleLookupTarget
    morale: object
    location_hint: TargetedMoraleLocationHint
    fleet_badge: int | None
    matched_result_count: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetedMoraleLookupTarget):
            raise TypeError("target имеет неверный тип")
        if not isinstance(self.location_hint, TargetedMoraleLocationHint):
            raise TypeError("location_hint имеет неверный тип")
        if self.fleet_badge is not None and (
            type(self.fleet_badge) is not int or not 1 <= self.fleet_badge <= 6
        ):
            raise ValueError("fleet_badge должен быть 1..6 или None")
        if type(self.matched_result_count) is not int or self.matched_result_count < 1:
            raise ValueError("matched_result_count должен быть положительным int")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at должен быть timezone-aware")


class _LookupTextOcr(Protocol):
    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]: ...


class _LookupWhiteTextModel(Ocr):
    def pre_process(self, image: np.ndarray) -> np.ndarray:
        return extract_letters(
            image,
            letter=(255, 255, 255),
            threshold=self.threshold,
        ).astype(np.uint8)


class TargetedLookupTextOcr:
    """Компактный OCR для FLEET/SELECTED/Resting overlays."""

    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _LookupWhiteTextModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            name="TARGETED_MORALE_CARD_STATE",
        )
        raw = model.ocr(frame)
        values = raw if isinstance(raw, list) else [raw]
        if len(values) != len(areas) or any(not isinstance(value, str) for value in values):
            raise TargetedMoraleLookupError(
                "state_ocr_failed",
                "OCR состояния Search card вернул неверное число значений.",
            )
        return tuple(values)


class TargetedMoraleLookupScanner:
    """Прочитать только видимые filtered Search cards без Device side effects."""

    GREEN_HUE_MIN = 35
    GREEN_HUE_MAX = 90
    GREEN_SATURATION_MIN = 75
    GREEN_VALUE_MIN = 90
    GREEN_RATIO_MIN = 0.035

    def __init__(
        self,
        catalog: DockIdentityCatalog | None = None,
        *,
        layout: TargetedMoraleLookupLayout | None = None,
        name_ocr=None,
        morale_ocr=None,
        text_ocr: _LookupTextOcr | None = None,
        resolver: DockShipIdentityResolver | None = None,
    ) -> None:
        self.catalog = load_dock_identity_catalog() if catalog is None else catalog
        self.layout = TargetedMoraleLookupLayout() if layout is None else layout
        self.name_ocr = DockShipNameOcr() if name_ocr is None else name_ocr
        self.morale_ocr = DormMoraleValueOcr() if morale_ocr is None else morale_ocr
        self.text_ocr = TargetedLookupTextOcr() if text_ocr is None else text_ocr
        self.resolver = (
            DockShipIdentityResolver(self.catalog) if resolver is None else resolver
        )

    def normalize_frame(self, frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
            raise TargetedMoraleLookupError(
                "invalid_frame",
                "Targeted lookup ожидает цветной np.ndarray.",
            )
        height, width = frame.shape[:2]
        if width * self.layout.frame_height != height * self.layout.frame_width:
            raise TargetedMoraleLookupError(
                "invalid_frame",
                "Targeted lookup получил неподдерживаемое соотношение сторон.",
            )
        if width < self.layout.frame_width or height < self.layout.frame_height:
            raise TargetedMoraleLookupError(
                "invalid_frame",
                "Targeted lookup получил слишком маленький кадр.",
            )
        if (height, width) == (self.layout.frame_height, self.layout.frame_width):
            return frame
        return cv2.resize(
            frame,
            (self.layout.frame_width, self.layout.frame_height),
            interpolation=cv2.INTER_AREA,
        )

    def _present(self, frame: np.ndarray, card: TargetedMoraleCardGeometry) -> bool:
        x1, y1, x2, y2 = card.presence_area
        hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)
        green = (
            (hsv[:, :, 0] >= self.GREEN_HUE_MIN)
            & (hsv[:, :, 0] <= self.GREEN_HUE_MAX)
            & (hsv[:, :, 1] >= self.GREEN_SATURATION_MIN)
            & (hsv[:, :, 2] >= self.GREEN_VALUE_MIN)
        )
        return float(np.mean(green)) >= self.GREEN_RATIO_MIN

    @staticmethod
    def _name_area(card: TargetedMoraleCardGeometry) -> tuple[int, int, int, int]:
        x1, y1, _x2, _y2 = card.area
        return x1 - 10, y1 + 160, x1 + 142, y1 + 190

    @staticmethod
    def _fleet_badge(raw: str) -> int | None:
        normalized = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
        match = re.search(r"FLEET([1-6])", normalized)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _location_hint(raw: str) -> TargetedMoraleLocationHint:
        normalized = re.sub(r"[^A-Z]", "", str(raw or "").upper())
        if "SELECTED" in normalized:
            return TargetedMoraleLocationHint.TRAIN
        if "RESTING" in normalized:
            return TargetedMoraleLocationHint.REST
        return TargetedMoraleLocationHint.OUTSIDE_DORM

    def scan(
        self,
        frame: np.ndarray,
        target: TargetedMoraleLookupTarget,
        *,
        observed_at: datetime | None = None,
    ) -> TargetedMoraleLookupObservation:
        if not isinstance(target, TargetedMoraleLookupTarget):
            raise TypeError("target должен быть TargetedMoraleLookupTarget")
        normalized = self.normalize_frame(frame)
        cards = tuple(card for card in self.layout.cards() if self._present(normalized, card))
        if not cards:
            raise TargetedMoraleLookupError(
                "no_result",
                f"Search не показал карточек для {target.canonical_name}.",
            )
        try:
            raw_names = self.name_ocr.read_names(
                normalized,
                tuple(self._name_area(card) for card in cards),
            )
        except TargetedMoraleLookupError:
            raise
        except Exception as exc:
            raise TargetedMoraleLookupError(
                "identity_ocr_failed",
                f"Не удалось прочитать identity Search cards: {type(exc).__name__}.",
            ) from exc
        if len(raw_names) != len(cards):
            raise TargetedMoraleLookupError(
                "identity_ocr_failed",
                "Identity OCR Search cards вернул неверное число значений.",
            )

        matched = []
        for card, raw_name in zip(cards, raw_names, strict=True):
            resolution = self.resolver.resolve(raw_name)
            if (
                resolution.status is IdentityStatus.MATCHED
                and resolution.canonical_identity == target.canonical_identity
                and resolution.ship_form is target.ship_form
            ):
                matched.append(card)
        if not matched:
            raise TargetedMoraleLookupError(
                "identity_not_proven",
                f"Search result identity не доказана для {target.canonical_name}.",
            )

        try:
            fleet_texts = self.text_ocr.read_values(
                normalized,
                tuple(card.fleet_badge_area for card in matched),
            )
            state_texts = self.text_ocr.read_values(
                normalized,
                tuple(card.state_area for card in matched),
            )
        except TargetedMoraleLookupError:
            raise
        except Exception as exc:
            raise TargetedMoraleLookupError(
                "state_ocr_failed",
                f"Не удалось прочитать overlay Search cards: {type(exc).__name__}.",
            ) from exc
        if len(fleet_texts) != len(matched) or len(state_texts) != len(matched):
            raise TargetedMoraleLookupError(
                "state_ocr_failed",
                "OCR overlay Search cards вернул неверное число значений.",
            )

        badges = tuple(self._fleet_badge(value) for value in fleet_texts)
        chosen_index: int | None = None
        if len(matched) == 1:
            if badges[0] == target.fleet_index:
                chosen_index = 0
        else:
            fleet_matches = tuple(
                index
                for index, badge in enumerate(badges)
                if badge == target.fleet_index
            )
            if len(fleet_matches) == 1:
                chosen_index = fleet_matches[0]
        if chosen_index is None:
            raise TargetedMoraleLookupError(
                "fleet_not_proven" if len(matched) == 1 else "duplicate_ambiguous",
                (
                    f"Физический Fleet {target.fleet_index} не доказан для "
                    f"{target.canonical_name}."
                    if len(matched) == 1
                    else f"Несколько физических copies {target.canonical_name} "
                    "остались неоднозначны."
                ),
            )

        chosen = matched[chosen_index]
        try:
            morale_values = self.morale_ocr.read_values(
                normalized,
                (chosen.morale_area,),
            )
        except TargetedMoraleLookupError:
            raise
        except Exception as exc:
            raise TargetedMoraleLookupError(
                "morale_ocr_failed",
                f"Не удалось прочитать exact morale {target.canonical_name}: {type(exc).__name__}.",
            ) from exc
        if len(morale_values) != 1:
            raise TargetedMoraleLookupError(
                "morale_ocr_failed",
                "Morale OCR Search card вернул неверное число значений.",
            )
        timestamp = observed_at or datetime.now(UTC)
        return TargetedMoraleLookupObservation(
            target=target,
            morale=morale_values[0],
            location_hint=self._location_hint(state_texts[chosen_index]),
            fleet_badge=badges[chosen_index],
            matched_result_count=len(matched),
            observed_at=timestamp,
        )


class TargetedMoraleLookupController(UI):
    """Управлять Search полем, не нажимая result card и Confirm."""

    def __init__(self, config, device=None, *, scanner=None) -> None:
        super().__init__(config, device=device)
        self._scanner = scanner

    @cached_property
    def targeted_morale_layout(self) -> TargetedMoraleLookupLayout:
        return TargetedMoraleLookupLayout()

    @cached_property
    def targeted_morale_scanner(self) -> TargetedMoraleLookupScanner:
        return self._scanner or TargetedMoraleLookupScanner()

    @staticmethod
    def _button(area: tuple[int, int, int, int], name: str) -> Button:
        return Button(area=(), color=(), button=area, name=name)

    def _capture(self) -> np.ndarray:
        self.device.screenshot()
        frame = self.device.image
        if not isinstance(frame, np.ndarray):
            raise TargetedMoraleLookupError(
                "invalid_frame",
                "Device не содержит Search screenshot.",
            )
        return frame

    def _search_active(self, frame: np.ndarray) -> bool:
        normalized = self.targeted_morale_scanner.normalize_frame(frame)
        x1, y1, x2, y2 = self.targeted_morale_layout.search_button
        hsv = cv2.cvtColor(normalized[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)
        gold = (
            (hsv[:, :, 0] >= self.targeted_morale_layout.search_hue_min)
            & (hsv[:, :, 0] <= self.targeted_morale_layout.search_hue_max)
            & (hsv[:, :, 1] >= self.targeted_morale_layout.search_saturation_min)
            & (hsv[:, :, 2] >= self.targeted_morale_layout.search_value_min)
        )
        return float(np.mean(gold)) >= self.targeted_morale_layout.search_gold_ratio_min

    def activate_search(self) -> np.ndarray:
        """Открыть Search только после доказанного candidate-selection state."""

        from module.retire.assets import DOCK_CHECK

        frame = self._capture()
        if not self.appear(DOCK_CHECK, offset=(20, 20)):
            raise TargetedMoraleLookupError(
                "selection_not_proven",
                "Candidate-selection UI не доказан перед Search.",
            )
        if self._search_active(frame):
            return frame
        self.device.click(
            self._button(self.targeted_morale_layout.search_button, "MORALE_LOOKUP_SEARCH")
        )
        for _ in range(5):
            frame = self._capture()
            if self._search_active(frame):
                return frame
        raise TargetedMoraleLookupError(
            "search_not_open",
            "Search field не открылся после ограниченного безопасного ожидания.",
        )

    def lookup(self, target: TargetedMoraleLookupTarget) -> TargetedMoraleLookupObservation:
        if not isinstance(target, TargetedMoraleLookupTarget):
            raise TypeError("target должен быть TargetedMoraleLookupTarget")
        self.activate_search()
        # Лупа только раскрывает Search. Для Android input поле нужно отдельно
        # сфокусировать безопасным кликом внутри подтверждённой текстовой области.
        self.device.click(
            self._button(
                self.targeted_morale_layout.search_input,
                "MORALE_LOOKUP_SEARCH_INPUT",
            )
        )
        self._capture()
        try:
            self.device.text_input_and_confirm(target.search_query, clear=True)
        except TargetedMoraleLookupError:
            raise
        except Exception as exc:
            raise TargetedMoraleLookupError(
                "search_input_failed",
                f"Не удалось ввести Search query: {type(exc).__name__}.",
            ) from exc
        last_error: TargetedMoraleLookupError | None = None
        for _ in range(5):
            frame = self._capture()
            try:
                return self.targeted_morale_scanner.scan(
                    frame,
                    target,
                    observed_at=datetime.now(UTC),
                )
            except TargetedMoraleLookupError as exc:
                last_error = exc
                if exc.error_code not in {
                    "no_result",
                    "identity_not_proven",
                    "identity_ocr_failed",
                    "morale_ocr_failed",
                    "state_ocr_failed",
                    "fleet_not_proven",
                }:
                    raise
        assert last_error is not None
        raise last_error

    def exit_to_main(self) -> None:
        """Discard selection modal через подтверждённый Home, не используя Confirm."""

        from module.retire.assets import DOCK_CHECK

        self._capture()
        if not self.appear(DOCK_CHECK, offset=(20, 20)):
            raise TargetedMoraleLookupError(
                "selection_not_proven",
                "Нельзя доказать безопасный Home exit из candidate-selection.",
            )
        self.device.click(
            self._button(self.targeted_morale_layout.home_button, "MORALE_LOOKUP_HOME")
        )
        self._capture()
        self.ui_ensure(page_main)


__all__ = (
    "TargetedMoraleLocationHint",
    "TargetedMoraleLookupController",
    "TargetedMoraleLookupError",
    "TargetedMoraleLookupLayout",
    "TargetedMoraleLookupObservation",
    "TargetedMoraleLookupScanner",
)
