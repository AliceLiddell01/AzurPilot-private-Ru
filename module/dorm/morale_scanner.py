"""Чистый сканер имени, morale и recovery на уже открытом этаже Dorm."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

import cv2
import numpy as np

from module.base.utils import extract_letters
from module.dock_inventory.catalog import (
    DockIdentityCatalog,
    load_dock_identity_catalog,
)
from module.dock_inventory.identity import DockShipIdentityResolver
from module.dock_inventory.model import IdentityStatus, ShipForm
from module.dorm.morale_model import DormFloor, DormFloorSnapshot, DormMoraleObservation
from module.logger import logger
from module.ocr.ocr import Ocr


class DormMoraleScanError(RuntimeError):
    """Базовая ошибка чистого сканера Dorm."""


class DormMoraleInputError(DormMoraleScanError):
    """Кадр или этаж не соответствует контракту сканера."""


class DormMoraleOcrError(DormMoraleScanError):
    """OCR не вернул полный и валидный набор фактов."""


@dataclass(frozen=True, slots=True)
class DormCardGeometry:
    ordinal: int
    presence_area: tuple[int, int, int, int]
    morale_area: tuple[int, int, int, int]
    recovery_area: tuple[int, int, int, int]
    floor_1_name_area: tuple[int, int, int, int]
    floor_2_name_area: tuple[int, int, int, int]

    def name_area(self, floor: DormFloor) -> tuple[int, int, int, int]:
        return (
            self.floor_1_name_area
            if floor is DormFloor.FLOOR_1
            else self.floor_2_name_area
        )


@dataclass(frozen=True, slots=True)
class DormMoraleLayout:
    frame_width: int
    frame_height: int
    cards: tuple[DormCardGeometry, ...]

    def __post_init__(self) -> None:
        if type(self.frame_width) is not int or self.frame_width <= 0:
            raise ValueError("frame_width должен быть положительным int")
        if type(self.frame_height) is not int or self.frame_height <= 0:
            raise ValueError("frame_height должен быть положительным int")
        if not isinstance(self.cards, tuple) or len(self.cards) != 5:
            raise ValueError("Dorm layout должен содержать пять card slots")
        if tuple(item.ordinal for item in self.cards) != (1, 2, 3, 4, 5):
            raise ValueError("Dorm card ordinals должны быть 1..5")
        for card in self.cards:
            for area in (
                card.presence_area,
                card.morale_area,
                card.recovery_area,
                card.floor_1_name_area,
                card.floor_2_name_area,
            ):
                if (
                    not isinstance(area, tuple)
                    or len(area) != 4
                    or any(type(value) is not int for value in area)
                ):
                    raise TypeError("Dorm ROI должен быть tuple из четырёх int")
                x1, y1, x2, y2 = area
                if not (
                    0 <= x1 < x2 <= self.frame_width
                    and 0 <= y1 < y2 <= self.frame_height
                ):
                    raise ValueError(f"Dorm ROI выходит за кадр: {area!r}")


_CARD_X = ((141, 299), (311, 469), (481, 639), (651, 809), (821, 979))
GLOBAL_DORM_MORALE_LAYOUT_1280_720 = DormMoraleLayout(
    frame_width=1280,
    frame_height=720,
    cards=tuple(
        DormCardGeometry(
            ordinal=index,
            presence_area=(x1 + 3, 590, x1 + 90, 650),
            morale_area=(x2 - 48, 565, x2 - 4, 605),
            recovery_area=(x2 - 64, 595, x2 - 2, 635),
            floor_1_name_area=(x1, 440, x2, 480),
            floor_2_name_area=(x1, 490, x2, 540),
        )
        for index, (x1, x2) in enumerate(_CARD_X, start=1)
    ),
)


@dataclass(frozen=True, slots=True)
class DormPresencePolicy:
    green_hue_min: int = 35
    green_hue_max: int = 85
    green_saturation_min: int = 80
    green_value_min: int = 100
    green_ratio_min: float = 0.05


class _DormNameOcr(Protocol):
    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]: ...


class _DormMoraleValueOcr(Protocol):
    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[Decimal, ...]: ...


class _DormRecoveryOcr(Protocol):
    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[Decimal, ...]: ...


class _DormWhiteTextOcrModel(Ocr):
    def pre_process(self, image: np.ndarray) -> np.ndarray:
        return extract_letters(
            image,
            letter=(255, 255, 255),
            threshold=self.threshold,
        ).astype(np.uint8)


class _DormMoraleNumericOcrModel(_DormWhiteTextOcrModel):
    def after_process(self, result):
        result = super().after_process(result)
        return (
            result.replace("I", "1")
            .replace("D", "0")
            .replace("S", "5")
            .replace("B", "8")
        )


def _result_tuple(result, expected: int, *, field: str) -> tuple[str, ...]:
    values = result if isinstance(result, list) else [result]
    if len(values) != expected or any(not isinstance(value, str) for value in values):
        raise DormMoraleOcrError(
            f"OCR {field} вернул результат, не соответствующий card count."
        )
    return tuple(values)


def parse_morale_value(raw: str) -> Decimal:
    normalized = str(raw or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d+)?", normalized):
        raise DormMoraleOcrError(f"Некорректный Morale OCR: {raw!r}")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        raise DormMoraleOcrError(f"Некорректный Morale OCR: {raw!r}") from None
    if not Decimal(0) <= value <= Decimal(150):
        raise DormMoraleOcrError(f"Morale вне диапазона 0..150: {raw!r}")
    return value


def parse_recovery_speed(raw: str) -> Decimal:
    normalized = str(raw or "").strip()
    match = re.fullmatch(r"(\d{1,4}(?:\.\d+)?)\s*/\s*[hH]", normalized)
    if match is None:
        raise DormMoraleOcrError(f"Некорректный Recovery Speed OCR: {raw!r}")
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        raise DormMoraleOcrError(f"Некорректный Recovery Speed OCR: {raw!r}") from None
    if not Decimal(0) <= value <= Decimal(1500):
        raise DormMoraleOcrError(f"Recovery Speed вне диапазона: {raw!r}")
    return value


class DormShipNameOcr:
    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _DormWhiteTextOcrModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            name="DORM_MORALE_SHIP_NAME",
        )
        return _result_tuple(model.ocr(frame), len(areas), field="ship name")


class DormMoraleValueOcr:
    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[Decimal, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _DormMoraleNumericOcrModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            alphabet="0123456789.IDSB",
            name="DORM_MORALE_VALUE",
        )
        raw = _result_tuple(model.ocr(frame), len(areas), field="Morale")
        return tuple(parse_morale_value(value) for value in raw)


class DormRecoverySpeedOcr:
    def read_values(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[Decimal, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _DormWhiteTextOcrModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            name="DORM_RECOVERY_SPEED",
        )
        raw = _result_tuple(model.ocr(frame), len(areas), field="Recovery Speed")
        return tuple(parse_recovery_speed(value) for value in raw)


class DormMoraleScanner:
    """Прочитать один уже открытый этаж без побочных эффектов Device или UI."""

    def __init__(
        self,
        catalog: DockIdentityCatalog | None = None,
        *,
        layout: DormMoraleLayout = GLOBAL_DORM_MORALE_LAYOUT_1280_720,
        presence_policy: DormPresencePolicy | None = None,
        name_ocr: _DormNameOcr | None = None,
        morale_ocr: _DormMoraleValueOcr | None = None,
        recovery_ocr: _DormRecoveryOcr | None = None,
        resolver: DockShipIdentityResolver | None = None,
    ) -> None:
        self.catalog = load_dock_identity_catalog() if catalog is None else catalog
        if not isinstance(self.catalog, DockIdentityCatalog):
            raise TypeError("catalog должен быть DockIdentityCatalog")
        if not isinstance(layout, DormMoraleLayout):
            raise TypeError("layout должен быть DormMoraleLayout")
        presence_policy = (
            DormPresencePolicy() if presence_policy is None else presence_policy
        )
        if not isinstance(presence_policy, DormPresencePolicy):
            raise TypeError("presence_policy должен быть DormPresencePolicy")
        self.layout = layout
        self.presence_policy = presence_policy
        self.name_ocr = DormShipNameOcr() if name_ocr is None else name_ocr
        self.morale_ocr = DormMoraleValueOcr() if morale_ocr is None else morale_ocr
        self.recovery_ocr = (
            DormRecoverySpeedOcr() if recovery_ocr is None else recovery_ocr
        )
        self.resolver = (
            DockShipIdentityResolver(self.catalog) if resolver is None else resolver
        )

    def normalize_frame(self, frame: np.ndarray) -> np.ndarray:
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
            raise DormMoraleInputError("Dorm scanner ожидает цветной np.ndarray.")
        height, width = frame.shape[:2]
        if width * self.layout.frame_height != height * self.layout.frame_width:
            raise DormMoraleInputError(
                f"Dorm frame имеет неподдерживаемое соотношение сторон: {(height, width)}."
            )
        if width < self.layout.frame_width or height < self.layout.frame_height:
            raise DormMoraleInputError(f"Dorm frame слишком мал: {(height, width)}.")
        if (height, width) == (self.layout.frame_height, self.layout.frame_width):
            return frame
        return cv2.resize(
            frame,
            (self.layout.frame_width, self.layout.frame_height),
            interpolation=cv2.INTER_AREA,
        )

    def _occupied(self, frame: np.ndarray, card: DormCardGeometry) -> bool:
        x1, y1, x2, y2 = card.presence_area
        hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)
        green = (
            (hsv[:, :, 0] >= self.presence_policy.green_hue_min)
            & (hsv[:, :, 0] <= self.presence_policy.green_hue_max)
            & (hsv[:, :, 1] >= self.presence_policy.green_saturation_min)
            & (hsv[:, :, 2] >= self.presence_policy.green_value_min)
        )
        return float(np.mean(green)) >= self.presence_policy.green_ratio_min

    def scan(self, frame: np.ndarray, *, floor: DormFloor) -> DormFloorSnapshot:
        if not isinstance(floor, DormFloor):
            raise DormMoraleInputError("floor должен быть DormFloor")
        normalized = self.normalize_frame(frame)
        occupied = tuple(
            card for card in self.layout.cards if self._occupied(normalized, card)
        )
        try:
            names = self.name_ocr.read_names(
                normalized,
                tuple(card.name_area(floor) for card in occupied),
            )
            morale_values = self.morale_ocr.read_values(
                normalized,
                tuple(card.morale_area for card in occupied),
            )
            recovery_values = self.recovery_ocr.read_values(
                normalized,
                tuple(card.recovery_area for card in occupied),
            )
        except DormMoraleOcrError:
            raise
        except Exception as error:
            raise DormMoraleOcrError(f"Dorm OCR завершился ошибкой: {error}") from error
        if not (
            len(names) == len(morale_values) == len(recovery_values) == len(occupied)
        ):
            raise DormMoraleOcrError("Dorm OCR вернул несогласованное число значений.")

        observations = []
        for card, raw_name, morale, recovery in zip(
            occupied,
            names,
            morale_values,
            recovery_values,
        ):
            resolution = self.resolver.resolve(raw_name)
            if resolution.status is not IdentityStatus.MATCHED:
                logger.warning(
                    "[Общежитие — сканер морали] Не удалось однозначно определить "
                    f"корабль: floor={floor.value} ordinal={card.ordinal} "
                    f"status={resolution.status.value} raw_name_ocr={raw_name!r} "
                    f"reason={resolution.reason!r}"
                )
            observed_form = (
                ShipForm.RETROFIT
                if resolution.status is IdentityStatus.MATCHED
                and resolution.ship_form is ShipForm.RETROFIT
                and re.search(
                    r"\(\s*Retrofit\s*\)\s*$", resolution.displayed_name, re.IGNORECASE
                )
                else None
            )
            observations.append(
                DormMoraleObservation(
                    floor=floor,
                    ordinal=card.ordinal,
                    raw_name_ocr=resolution.raw_name_ocr,
                    displayed_name=resolution.displayed_name,
                    identity_status=resolution.status,
                    canonical_identity=resolution.canonical_identity,
                    canonical_name=resolution.canonical_name,
                    ship_form=observed_form,
                    morale=morale,
                    recovery_per_hour=recovery,
                )
            )
        return DormFloorSnapshot(
            floor=floor,
            observations=tuple(observations),
            catalog_fingerprint=self.catalog.fingerprint,
        )


__all__ = (
    "GLOBAL_DORM_MORALE_LAYOUT_1280_720",
    "DormCardGeometry",
    "DormMoraleInputError",
    "DormMoraleLayout",
    "DormMoraleOcrError",
    "DormMoraleScanError",
    "DormMoraleScanner",
    "DormPresencePolicy",
    "DormRecoverySpeedOcr",
    "DormShipNameOcr",
    "parse_morale_value",
    "parse_recovery_speed",
)