"""Доменная модель снимка состава игрового флота."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm

SUPPORTED_SURFACE_FLEET_INDICES = (1, 2, 3, 4, 5, 6)


def validate_surface_fleet_index(value: object) -> int:
    """Вернуть допустимый индекс Formation Surface Fleet."""

    if type(value) is not int or value not in SUPPORTED_SURFACE_FLEET_INDICES:
        raise ValueError("fleet_index должен быть int в диапазоне 1..6")
    return value


@dataclass(frozen=True, slots=True)
class FleetSelection:
    """Декларативный неизменяемый выбор одного или нескольких флотов."""

    fleet_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.fleet_indices, tuple):
            raise TypeError("fleet_indices должен быть tuple")
        if not self.fleet_indices:
            raise ValueError("Fleet selection не должен быть пустым")
        normalized = tuple(
            sorted({validate_surface_fleet_index(value) for value in self.fleet_indices})
        )
        object.__setattr__(self, "fleet_indices", normalized)

    @classmethod
    def one(cls, fleet_index: int) -> FleetSelection:
        return cls((fleet_index,))

    @classmethod
    def several(cls, *fleet_indices: int) -> FleetSelection:
        return cls(tuple(fleet_indices))

    @classmethod
    def all(cls) -> FleetSelection:
        return cls(SUPPORTED_SURFACE_FLEET_INDICES)


class FormationFleetSide(Enum):
    """Сторона обычного надводного флота."""

    MAIN = "main"
    VANGUARD = "vanguard"


@dataclass(frozen=True, slots=True)
class FormationFleetSlotObservation:
    """Наблюдение одного из шести фиксированных слотов Formation Info."""

    side: FormationFleetSide
    position: int
    occupied: bool
    identity_status: IdentityStatus | None = None
    raw_name_ocr: str | None = None
    displayed_name: str | None = None
    canonical_identity: CanonicalShipIdentity | None = None
    canonical_name: str | None = None
    ship_form: ShipForm | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.side, FormationFleetSide):
            raise TypeError("side должен быть FormationFleetSide")
        if type(self.position) is not int or not 1 <= self.position <= 3:
            raise ValueError("position должен быть int в диапазоне 1..3")
        if type(self.occupied) is not bool:
            raise TypeError("occupied должен быть bool")

        if not self.occupied:
            if any(
                value is not None
                for value in (
                    self.identity_status,
                    self.raw_name_ocr,
                    self.displayed_name,
                    self.canonical_identity,
                    self.canonical_name,
                    self.ship_form,
                )
            ):
                raise ValueError("Пустой слот не должен содержать identity-данные")
            return

        if not isinstance(self.identity_status, IdentityStatus):
            raise TypeError("Занятый слот должен содержать IdentityStatus")
        for field_name, value in (
            ("raw_name_ocr", self.raw_name_ocr),
            ("displayed_name", self.displayed_name),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{field_name} занятого слота должен быть строкой")

        if self.identity_status is IdentityStatus.MATCHED:
            if not isinstance(self.canonical_identity, CanonicalShipIdentity):
                raise ValueError("MATCHED требует canonical identity")
            if not isinstance(self.canonical_name, str) or not self.canonical_name.strip():
                raise ValueError("MATCHED требует canonical name")
            if not isinstance(self.ship_form, ShipForm):
                raise ValueError("MATCHED требует ship form")
        elif any(
            value is not None
            for value in (self.canonical_identity, self.canonical_name, self.ship_form)
        ):
            raise ValueError("Только MATCHED может содержать canonical identity/name/form")


@dataclass(frozen=True, slots=True)
class FormationFleetSnapshot:
    """Неизменяемый снимок состава одного игрового флота 1..6."""

    fleet_index: int
    slots: tuple[FormationFleetSlotObservation, ...]
    catalog_fingerprint: str

    def __post_init__(self) -> None:
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.slots, tuple) or len(self.slots) != 6:
            raise ValueError("Formation snapshot должен содержать ровно шесть слотов")
        if not all(isinstance(slot, FormationFleetSlotObservation) for slot in self.slots):
            raise TypeError("slots должен содержать FormationFleetSlotObservation")
        expected = (
            (FormationFleetSide.MAIN, 1),
            (FormationFleetSide.MAIN, 2),
            (FormationFleetSide.MAIN, 3),
            (FormationFleetSide.VANGUARD, 1),
            (FormationFleetSide.VANGUARD, 2),
            (FormationFleetSide.VANGUARD, 3),
        )
        actual = tuple((slot.side, slot.position) for slot in self.slots)
        if actual != expected:
            raise ValueError("Formation snapshot содержит неверный порядок слотов")
        if not re.fullmatch(r"[0-9a-f]{64}", self.catalog_fingerprint):
            raise ValueError("catalog_fingerprint должен быть SHA-256")

    @property
    def complete(self) -> bool:
        """Истина, если каждый занятый слот однозначно сопоставлен с кораблём."""

        return all(
            not slot.occupied or slot.identity_status is IdentityStatus.MATCHED
            for slot in self.slots
        )

    @property
    def occupied_count(self) -> int:
        return sum(slot.occupied for slot in self.slots)

    @property
    def ships(self) -> tuple[CanonicalShipIdentity, ...]:
        return tuple(
            slot.canonical_identity
            for slot in self.slots
            if slot.canonical_identity is not None
        )
