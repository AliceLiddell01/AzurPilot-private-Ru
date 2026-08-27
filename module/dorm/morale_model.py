"""Transport-neutral наблюдения из UI управления morale в Dorm."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm


def _aware(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} должен содержать timezone-aware datetime")
    return value


def _bounded(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"{field} должен быть непустой строкой длиной до {maximum} символов"
        )
    return value


class DormFloor(StrEnum):
    FLOOR_1 = "1F"
    FLOOR_2 = "2F"


class DormFloorScanStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DormMoraleScanStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DormMoraleObservation:
    floor: DormFloor
    ordinal: int
    raw_name_ocr: str
    displayed_name: str
    identity_status: IdentityStatus
    morale: Decimal
    recovery_per_hour: Decimal
    canonical_identity: CanonicalShipIdentity | None = None
    canonical_name: str | None = None
    ship_form: ShipForm | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.floor, DormFloor):
            raise TypeError("floor должен быть DormFloor")
        if type(self.ordinal) is not int or not 1 <= self.ordinal <= 5:
            raise ValueError("ordinal должен быть int в диапазоне 1..5")
        for field_name, value in (
            ("raw_name_ocr", self.raw_name_ocr),
            ("displayed_name", self.displayed_name),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{field_name} должен быть строкой")
        if not isinstance(self.identity_status, IdentityStatus):
            raise TypeError("identity_status должен быть IdentityStatus")
        for field_name, value, minimum, maximum in (
            ("morale", self.morale, Decimal(0), Decimal(150)),
            (
                "recovery_per_hour",
                self.recovery_per_hour,
                Decimal(0),
                Decimal(1500),
            ),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise TypeError(f"{field_name} должен быть конечным Decimal")
            if not minimum <= value <= maximum:
                raise ValueError(
                    f"{field_name} должен быть в диапазоне {minimum}..{maximum}"
                )
        if self.identity_status is IdentityStatus.MATCHED:
            if not isinstance(self.canonical_identity, CanonicalShipIdentity):
                raise ValueError("MATCHED требует canonical identity")
            if (
                not isinstance(self.canonical_name, str)
                or not self.canonical_name.strip()
            ):
                raise ValueError("MATCHED требует canonical name")
            if self.ship_form is not None and not isinstance(self.ship_form, ShipForm):
                raise TypeError("ship_form должен быть ShipForm или None")
        elif any(
            value is not None
            for value in (self.canonical_identity, self.canonical_name, self.ship_form)
        ):
            raise ValueError(
                "Только MATCHED может содержать canonical identity/name/form"
            )


@dataclass(frozen=True, slots=True)
class DormFloorSnapshot:
    floor: DormFloor
    observations: tuple[DormMoraleObservation, ...]
    catalog_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.floor, DormFloor):
            raise TypeError("floor должен быть DormFloor")
        if not isinstance(self.observations, tuple) or any(
            not isinstance(item, DormMoraleObservation) for item in self.observations
        ):
            raise TypeError("observations должен быть tuple DormMoraleObservation")
        if any(item.floor is not self.floor for item in self.observations):
            raise ValueError("Dorm observations должны относиться к одному floor")
        ordinals = tuple(item.ordinal for item in self.observations)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError(
                "Dorm observation ordinals должны быть уникальны и упорядочены"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.catalog_fingerprint):
            raise ValueError("catalog_fingerprint должен быть SHA-256")


@dataclass(frozen=True, slots=True)
class DormFloorScanAttempt:
    floor: DormFloor
    status: DormFloorScanStatus
    observed_at: datetime | None = None
    snapshot: DormFloorSnapshot | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.floor, DormFloor):
            raise TypeError("floor должен быть DormFloor")
        if not isinstance(self.status, DormFloorScanStatus):
            raise TypeError("status должен быть DormFloorScanStatus")
        if self.status is DormFloorScanStatus.SUCCEEDED:
            _aware(self.observed_at, field="observed_at")
            if not isinstance(self.snapshot, DormFloorSnapshot):
                raise ValueError("Успешный floor scan требует snapshot")
            if self.snapshot.floor is not self.floor:
                raise ValueError("Floor scan содержит snapshot другого этажа")
            if self.error_code is not None:
                raise ValueError("Успешный floor scan не содержит error_code")
        else:
            if self.observed_at is not None or self.snapshot is not None:
                raise ValueError("Неуспешный floor scan не содержит observation")
            _bounded(self.error_code, field="error_code", maximum=64)


@dataclass(frozen=True, slots=True)
class DormMoraleScanResult:
    id: UUID
    started_at: datetime
    finished_at: datetime
    attempts: tuple[DormFloorScanAttempt, DormFloorScanAttempt]
    source: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise TypeError("Dorm scan id должен быть UUID")
        _aware(self.started_at, field="started_at")
        _aware(self.finished_at, field="finished_at")
        if self.finished_at.astimezone(UTC) < self.started_at.astimezone(UTC):
            raise ValueError("finished_at не должен предшествовать started_at")
        if (
            not isinstance(self.attempts, tuple)
            or len(self.attempts) != 2
            or tuple(item.floor for item in self.attempts)
            != (DormFloor.FLOOR_1, DormFloor.FLOOR_2)
        ):
            raise ValueError("Dorm scan должен содержать попытки 1F и 2F")
        _bounded(self.source, field="source", maximum=64)
        _bounded(self.idempotency_key, field="idempotency_key", maximum=128)
        fingerprints = {
            item.snapshot.catalog_fingerprint
            for item in self.attempts
            if item.snapshot is not None
        }
        if len(fingerprints) > 1:
            raise ValueError("Dorm floor snapshots используют разные каталоги")

    @property
    def status(self) -> DormMoraleScanStatus:
        success_count = sum(
            item.status is DormFloorScanStatus.SUCCEEDED for item in self.attempts
        )
        if success_count == 2:
            return DormMoraleScanStatus.SUCCEEDED
        if success_count == 1:
            return DormMoraleScanStatus.PARTIAL
        return DormMoraleScanStatus.FAILED

    @property
    def complete(self) -> bool:
        return self.status is DormMoraleScanStatus.SUCCEEDED

    @property
    def observations(self) -> tuple[DormMoraleObservation, ...]:
        return tuple(
            observation
            for attempt in self.attempts
            if attempt.snapshot is not None
            for observation in attempt.snapshot.observations
        )

    @property
    def catalog_fingerprint(self) -> str | None:
        return next(
            (
                item.snapshot.catalog_fingerprint
                for item in self.attempts
                if item.snapshot is not None
            ),
            None,
        )


__all__ = (
    "DormFloor",
    "DormFloorScanAttempt",
    "DormFloorScanStatus",
    "DormFloorSnapshot",
    "DormMoraleObservation",
    "DormMoraleScanResult",
    "DormMoraleScanStatus",
)
