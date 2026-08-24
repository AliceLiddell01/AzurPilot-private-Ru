"""Сканирование состава флота с открытого экрана Formation Info."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from module.base.utils import extract_letters
from module.dock_inventory.catalog import (
    DockIdentityCatalog,
    load_dock_identity_catalog,
)
from module.dock_inventory.identity import DockShipIdentityResolver
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
    validate_surface_fleet_index,
)
from module.ocr.ocr import Ocr


class FormationFleetScanError(RuntimeError):
    """Базовая ошибка сканера состава Formation."""


class FormationFleetInputError(FormationFleetScanError):
    """Кадр или геометрия не соответствуют поддерживаемому Formation Info."""


class FormationFleetOcrError(FormationFleetScanError):
    """OCR не смог вернуть согласованный результат для занятых слотов."""


@dataclass(frozen=True, slots=True)
class FormationSlotGeometry:
    """Геометрия одного слота в нормализованном кадре 1280x720."""

    side: FormationFleetSide
    position: int
    presence_area: tuple[int, int, int, int]
    name_area: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class FormationInfoLayout:
    """Инъецируемый контракт геометрии Formation Info."""

    frame_width: int
    frame_height: int
    slots: tuple[FormationSlotGeometry, ...]

    def __post_init__(self) -> None:
        if type(self.frame_width) is not int or self.frame_width <= 0:
            raise ValueError("frame_width должен быть положительным int")
        if type(self.frame_height) is not int or self.frame_height <= 0:
            raise ValueError("frame_height должен быть положительным int")
        if not isinstance(self.slots, tuple) or len(self.slots) != 6:
            raise ValueError("Formation Info должен содержать шесть слотов")
        expected = (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
        if tuple((slot.side, slot.position) for slot in self.slots) != expected:
            raise ValueError("Formation Info содержит неверный порядок слотов")
        for slot in self.slots:
            for area in (slot.presence_area, slot.name_area):
                if (
                    not isinstance(area, tuple)
                    or len(area) != 4
                    or any(type(value) is not int for value in area)
                ):
                    raise TypeError("Область Formation Info должна быть tuple из четырёх int")
                x1, y1, x2, y2 = area
                if not (0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height):
                    raise ValueError(f"Область Formation Info выходит за кадр: {area!r}")


GLOBAL_FORMATION_INFO_LAYOUT_1280_720 = FormationInfoLayout(
    frame_width=1280,
    frame_height=720,
    slots=(
        FormationSlotGeometry(FormationFleetSide.MAIN, 1, (67, 548, 213, 568), (67, 458, 215, 486)),
        FormationSlotGeometry(FormationFleetSide.MAIN, 2, (249, 548, 403, 568), (248, 458, 405, 486)),
        FormationSlotGeometry(FormationFleetSide.MAIN, 3, (435, 548, 589, 568), (435, 458, 591, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 1, (691, 548, 839, 568), (689, 458, 843, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 2, (875, 548, 1028, 568), (873, 458, 1029, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 3, (1060, 548, 1214, 568), (1059, 458, 1216, 486)),
    ),
)


@dataclass(frozen=True, slots=True)
class FormationPresencePolicy:
    """Пороговая политика структурного признака занятого слота."""

    stats_green_hue_min: int = 35
    stats_green_hue_max: int = 85
    stats_green_saturation_min: int = 90
    stats_green_value_min: int = 120
    stats_green_ratio_min: float = 0.03

    def __post_init__(self) -> None:
        for field_name, value in (
            ("stats_green_hue_min", self.stats_green_hue_min),
            ("stats_green_hue_max", self.stats_green_hue_max),
        ):
            if type(value) is not int or not 0 <= value <= 179:
                raise ValueError(f"{field_name} должен быть int в диапазоне 0..179")
        for field_name, value in (
            ("stats_green_saturation_min", self.stats_green_saturation_min),
            ("stats_green_value_min", self.stats_green_value_min),
        ):
            if type(value) is not int or not 0 <= value <= 255:
                raise ValueError(f"{field_name} должен быть int в диапазоне 0..255")
        if self.stats_green_hue_min > self.stats_green_hue_max:
            raise ValueError("stats_green_hue_min не должен превышать stats_green_hue_max")
        if not 0.0 <= self.stats_green_ratio_min <= 1.0:
            raise ValueError("stats_green_ratio_min должен быть в диапазоне 0..1")


@dataclass(frozen=True, slots=True)
class FormationPresenceEvidence:
    """Наблюдаемый зелёный признак строки Total Stats."""

    stats_green_ratio: float
    occupied: bool


class _FormationNameOcr(Protocol):
    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]: ...


class _FormationNameOcrModel(Ocr):
    """OCR белых имён кораблей в карточках Formation Info."""

    def pre_process(self, image: np.ndarray) -> np.ndarray:
        return extract_letters(
            image,
            letter=(255, 255, 255),
            threshold=self.threshold,
        ).astype(np.uint8)


class FormationShipNameOcr:
    """Адаптер общего EN OCR для фиксированных областей имён Formation Info."""

    def read_names(
        self,
        frame: np.ndarray,
        areas: Sequence[tuple[int, int, int, int]],
    ) -> tuple[str, ...]:
        areas = tuple(areas)
        if not areas:
            return ()
        model = _FormationNameOcrModel(
            list(areas),
            lang="azur_lane",
            threshold=128,
            name="FORMATION_SHIP_NAME",
        )
        result = model.ocr(frame)
        values = result if isinstance(result, list) else [result]
        if len(values) != len(areas) or any(not isinstance(value, str) for value in values):
            raise FormationFleetOcrError(
                "OCR вернул результат, не соответствующий числу занятых слотов Formation."
            )
        return tuple(values)


class FormationFleetInfoScanner:
    """Сканер шести слотов с уже открытого Formation Info."""

    def __init__(
        self,
        catalog: DockIdentityCatalog | None = None,
        *,
        layout: FormationInfoLayout = GLOBAL_FORMATION_INFO_LAYOUT_1280_720,
        presence_policy: FormationPresencePolicy = FormationPresencePolicy(),
        name_ocr: _FormationNameOcr | None = None,
        resolver: DockShipIdentityResolver | None = None,
    ) -> None:
        self.catalog = load_dock_identity_catalog() if catalog is None else catalog
        if not isinstance(self.catalog, DockIdentityCatalog):
            raise TypeError("catalog должен быть DockIdentityCatalog")
        if not isinstance(layout, FormationInfoLayout):
            raise TypeError("layout должен быть FormationInfoLayout")
        if not isinstance(presence_policy, FormationPresencePolicy):
            raise TypeError("presence_policy должен быть FormationPresencePolicy")
        self.layout = layout
        self.presence_policy = presence_policy
        self.name_ocr = FormationShipNameOcr() if name_ocr is None else name_ocr
        self.resolver = DockShipIdentityResolver(self.catalog) if resolver is None else resolver

    def _validate_frame(self, frame: np.ndarray) -> None:
        if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
            raise FormationFleetInputError("Кадр Formation должен быть цветным np.ndarray.")
        if frame.shape[:2] != (self.layout.frame_height, self.layout.frame_width):
            raise FormationFleetInputError(
                "Кадр Formation имеет неподдерживаемую геометрию: "
                f"{frame.shape[:2]}, ожидается "
                f"{(self.layout.frame_height, self.layout.frame_width)}."
            )

    def presence_evidence(
        self,
        frame: np.ndarray,
        geometry: FormationSlotGeometry,
    ) -> FormationPresenceEvidence:
        x1, y1, x2, y2 = geometry.presence_area
        image = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        green = (
            (hsv[:, :, 0] >= self.presence_policy.stats_green_hue_min)
            & (hsv[:, :, 0] <= self.presence_policy.stats_green_hue_max)
            & (hsv[:, :, 1] >= self.presence_policy.stats_green_saturation_min)
            & (hsv[:, :, 2] >= self.presence_policy.stats_green_value_min)
        )
        stats_green_ratio = float(np.mean(green))
        return FormationPresenceEvidence(
            stats_green_ratio=stats_green_ratio,
            occupied=stats_green_ratio >= self.presence_policy.stats_green_ratio_min,
        )

    def scan(self, frame: np.ndarray, *, fleet_index: int) -> FormationFleetSnapshot:
        try:
            fleet_index = validate_surface_fleet_index(fleet_index)
        except ValueError:
            raise FormationFleetInputError("fleet_index должен быть int в диапазоне 1..6")
        self._validate_frame(frame)

        evidence = tuple(
            self.presence_evidence(frame, geometry)
            for geometry in self.layout.slots
        )
        occupied_geometries = tuple(
            geometry
            for geometry, item in zip(self.layout.slots, evidence)
            if item.occupied
        )
        ocr_frame = frame.copy()
        try:
            raw_names = self.name_ocr.read_names(
                ocr_frame,
                tuple(geometry.name_area for geometry in occupied_geometries),
            )
        except FormationFleetOcrError:
            raise
        except Exception as error:
            raise FormationFleetOcrError(
                f"Не удалось распознать имена занятых слотов Formation: {error}"
            ) from error
        if len(raw_names) != len(occupied_geometries):
            raise FormationFleetOcrError(
                "OCR вернул неверное число имён для занятых слотов Formation."
            )

        name_iter = iter(raw_names)
        observations: list[FormationFleetSlotObservation] = []
        for geometry, item in zip(self.layout.slots, evidence):
            if not item.occupied:
                observations.append(
                    FormationFleetSlotObservation(
                        side=geometry.side,
                        position=geometry.position,
                        occupied=False,
                    )
                )
                continue

            raw_name = next(name_iter)
            resolution = self.resolver.resolve(raw_name)
            observations.append(
                FormationFleetSlotObservation(
                    side=geometry.side,
                    position=geometry.position,
                    occupied=True,
                    identity_status=resolution.status,
                    raw_name_ocr=resolution.raw_name_ocr,
                    displayed_name=resolution.displayed_name,
                    canonical_identity=resolution.canonical_identity,
                    canonical_name=resolution.canonical_name,
                )
            )

        return FormationFleetSnapshot(
            fleet_index=fleet_index,
            slots=tuple(observations),
            catalog_fingerprint=self.catalog.fingerprint,
        )
