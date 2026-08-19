"""Сканирование состава флота с открытого экрана Formation Info."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import cv2
import numpy as np

from module.base.utils import extract_letters
from module.dock_inventory.catalog import DockIdentityCatalog, load_dock_identity_catalog
from module.dock_inventory.identity import DockShipIdentityResolver
from module.formation.model import (
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
)
from module.ocr.ocr import Ocr


class FormationFleetScanError(RuntimeError):
    """Базовая ошибка Formation Fleet Scanner."""


class FormationFleetInputError(FormationFleetScanError):
    """Кадр или геометрия не соответствуют поддерживаемому Formation Info."""


class FormationFleetOcrError(FormationFleetScanError):
    """OCR не смог вернуть согласованный результат для занятых слотов."""


@dataclass(frozen=True, slots=True)
class FormationSlotGeometry:
    """Геометрия одного слота в нормализованном кадре 1280x720."""

    side: FormationFleetSide
    position: int
    portrait_area: tuple[int, int, int, int]
    name_area: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class FormationInfoLayout:
    """Инъецируемый layout-контракт Formation Info."""

    frame_width: int
    frame_height: int
    slots: tuple[FormationSlotGeometry, ...]

    def __post_init__(self) -> None:
        if type(self.frame_width) is not int or self.frame_width <= 0:
            raise ValueError("frame_width должен быть положительным int")
        if type(self.frame_height) is not int or self.frame_height <= 0:
            raise ValueError("frame_height должен быть положительным int")
        if not isinstance(self.slots, tuple) or len(self.slots) != 6:
            raise ValueError("Formation Info layout должен содержать шесть слотов")
        expected = (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
        if tuple((slot.side, slot.position) for slot in self.slots) != expected:
            raise ValueError("Formation Info layout содержит неверный порядок слотов")
        for slot in self.slots:
            for area in (slot.portrait_area, slot.name_area):
                if (
                    not isinstance(area, tuple)
                    or len(area) != 4
                    or any(type(value) is not int for value in area)
                ):
                    raise TypeError("Область Formation Info должна быть tuple из четырёх int")
                x1, y1, x2, y2 = area
                if not (0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height):
                    raise ValueError(f"Область Formation Info выходит за frame: {area!r}")


GLOBAL_FORMATION_INFO_LAYOUT_1280_720 = FormationInfoLayout(
    frame_width=1280,
    frame_height=720,
    slots=(
        FormationSlotGeometry(FormationFleetSide.MAIN, 1, (66, 153, 215, 448), (67, 458, 215, 486)),
        FormationSlotGeometry(FormationFleetSide.MAIN, 2, (248, 153, 405, 448), (248, 458, 405, 486)),
        FormationSlotGeometry(FormationFleetSide.MAIN, 3, (435, 153, 591, 448), (435, 458, 591, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 1, (690, 153, 841, 448), (689, 458, 843, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 2, (873, 153, 1029, 448), (873, 458, 1029, 486)),
        FormationSlotGeometry(FormationFleetSide.VANGUARD, 3, (1059, 153, 1216, 448), (1059, 458, 1216, 486)),
    ),
)


@dataclass(frozen=True, slots=True)
class FormationPresencePolicy:
    """Пороговая политика независимого occupied/empty evidence."""

    bright_luma: int = 150
    bright_ratio_min: float = 0.08
    luma_std_min: float = 43.0

    def __post_init__(self) -> None:
        if type(self.bright_luma) is not int or not 0 <= self.bright_luma <= 255:
            raise ValueError("bright_luma должен быть int в диапазоне 0..255")
        if not 0.0 <= self.bright_ratio_min <= 1.0:
            raise ValueError("bright_ratio_min должен быть в диапазоне 0..1")
        if not 0.0 <= self.luma_std_min <= 255.0:
            raise ValueError("luma_std_min должен быть в диапазоне 0..255")


@dataclass(frozen=True, slots=True)
class FormationPresenceEvidence:
    luma_std: float
    bright_ratio: float
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
    """Адаптер общего EN OCR для фиксированных name ROI Formation Info."""

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
                "OCR вернул результат, не соответствующий числу занятых Formation slots."
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
            raise FormationFleetInputError("Formation frame должен быть цветным np.ndarray.")
        if frame.shape[:2] != (self.layout.frame_height, self.layout.frame_width):
            raise FormationFleetInputError(
                "Formation frame имеет неподдерживаемую геометрию: "
                f"{frame.shape[:2]}, ожидается "
                f"{(self.layout.frame_height, self.layout.frame_width)}."
            )

    def presence_evidence(
        self,
        frame: np.ndarray,
        geometry: FormationSlotGeometry,
    ) -> FormationPresenceEvidence:
        x1, y1, x2, y2 = geometry.portrait_area
        image = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        luma_std = float(np.std(gray))
        bright_ratio = float(np.mean(gray >= self.presence_policy.bright_luma))
        occupied = (
            luma_std >= self.presence_policy.luma_std_min
            and bright_ratio >= self.presence_policy.bright_ratio_min
        )
        return FormationPresenceEvidence(
            luma_std=luma_std,
            bright_ratio=bright_ratio,
            occupied=occupied,
        )

    def scan(self, frame: np.ndarray, *, fleet_index: int) -> FormationFleetSnapshot:
        if type(fleet_index) is not int or not 1 <= fleet_index <= 6:
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
                f"Не удалось распознать имена Formation slots: {error}"
            ) from error
        if len(raw_names) != len(occupied_geometries):
            raise FormationFleetOcrError(
                "OCR вернул неверное число имён для занятых Formation slots."
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
