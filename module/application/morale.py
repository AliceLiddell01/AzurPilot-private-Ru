"""Per-ship morale projection поверх актуального Formation Fleet State."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, TypeVar
from uuid import UUID, uuid4

from module.application.fleet_state import FleetStateObservation, FleetStateRepository
from module.application.instance_identity import resolve_runtime_instance
from module.application.storage_ports import StorageUnitOfWork
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    validate_surface_fleet_index,
)

_T = TypeVar("_T")
MORALE_MIN = Decimal(0)
MORALE_MAX = Decimal(150)
OUTSIDE_DORM_RECOVERY_PER_HOUR = Decimal(20)
OUTSIDE_DORM_RECOVERY_CEILING = Decimal(119)
_RECOVERY_TICK_MICROSECONDS = 6 * 60 * 1_000_000
_TICKS_PER_HOUR = Decimal(10)
_SLOT_ORDER = (
    (FormationFleetSide.MAIN, 1),
    (FormationFleetSide.MAIN, 2),
    (FormationFleetSide.MAIN, 3),
    (FormationFleetSide.VANGUARD, 1),
    (FormationFleetSide.VANGUARD, 2),
    (FormationFleetSide.VANGUARD, 3),
)


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


def _decimal(
    value: Decimal,
    *,
    field: str,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{field} должен быть конечным Decimal")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} должен быть в диапазоне {minimum}..{maximum}")
    return value


class MoraleKnowledge(StrEnum):
    EXACT = "exact"
    PROJECTED = "projected"
    UNKNOWN = "unknown"


class MoraleLocation(StrEnum):
    UNKNOWN = "unknown"
    DORM_FLOOR_1 = "dorm_floor_1"
    DORM_FLOOR_2 = "dorm_floor_2"
    OUTSIDE_DORM = "outside_dorm"


class MoraleContinuityError(ValueError):
    """Fleet State не доказывает occupant, к которому относится observation."""


class MoraleEventKind(StrEnum):
    """Тип изменения morale, зафиксированного после подтверждённого UI-события."""

    BATTLE = "battle"
    SHIPWRECK = "shipwreck"
    ADDITIONAL = "additional"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class MoraleRecoveryProfile:
    recovery_per_hour: Decimal
    recovery_ceiling: Decimal
    source: str

    def __post_init__(self) -> None:
        _decimal(
            self.recovery_per_hour,
            field="recovery_per_hour",
            minimum=MORALE_MIN,
            maximum=Decimal(1500),
        )
        _decimal(
            self.recovery_ceiling,
            field="recovery_ceiling",
            minimum=MORALE_MIN,
            maximum=MORALE_MAX,
        )
        _bounded(self.source, field="recovery source", maximum=64)

    @classmethod
    def outside_dorm_base(cls) -> MoraleRecoveryProfile:
        return cls(
            recovery_per_hour=OUTSIDE_DORM_RECOVERY_PER_HOUR,
            recovery_ceiling=OUTSIDE_DORM_RECOVERY_CEILING,
            source="outside_dorm:base",
        )


@dataclass(frozen=True, slots=True)
class RecordMoraleObservation:
    fleet_index: int
    side: FormationFleetSide
    position: int
    canonical_identity: CanonicalShipIdentity
    ship_form: ShipForm
    baseline: Decimal
    recovery: MoraleRecoveryProfile
    source: str
    idempotency_key: str
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.side, FormationFleetSide):
            raise TypeError("side должен быть FormationFleetSide")
        if type(self.position) is not int or not 1 <= self.position <= 3:
            raise ValueError("position должен быть int в диапазоне 1..3")
        if not isinstance(self.canonical_identity, CanonicalShipIdentity):
            raise TypeError("canonical_identity должен быть CanonicalShipIdentity")
        _bounded(
            self.canonical_identity.key,
            field="canonical_identity",
            maximum=128,
        )
        if not isinstance(self.ship_form, ShipForm):
            raise TypeError("ship_form должен быть ShipForm")
        _decimal(
            self.baseline,
            field="baseline",
            minimum=MORALE_MIN,
            maximum=MORALE_MAX,
        )
        if not isinstance(self.recovery, MoraleRecoveryProfile):
            raise TypeError("recovery должен быть MoraleRecoveryProfile")
        _bounded(self.source, field="source", maximum=64)
        _bounded(self.idempotency_key, field="idempotency_key", maximum=128)
        if self.observed_at is not None:
            _aware(self.observed_at, field="observed_at")


@dataclass(frozen=True, slots=True)
class RecordMoraleEvent:
    """Команда идемпотентного combat/warning event для одного физического флота."""

    fleet_index: int
    kind: MoraleEventKind
    cost: Decimal
    source: str
    event_key: str
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.kind, MoraleEventKind):
            raise TypeError("kind должен быть MoraleEventKind")
        _decimal(self.cost, field="cost", minimum=MORALE_MIN, maximum=MORALE_MAX)
        if self.kind is MoraleEventKind.WARNING and self.cost != MORALE_MIN:
            raise ValueError("Warning event не должен списывать morale")
        _bounded(self.source, field="source", maximum=64)
        _bounded(self.event_key, field="event_key", maximum=96)
        if self.observed_at is not None:
            _aware(self.observed_at, field="observed_at")


@dataclass(frozen=True, slots=True)
class MoraleEventResult:
    """Результат применения event без раскрытия внутренних storage-деталей."""

    event_key: str
    kind: MoraleEventKind
    fleet_index: int
    observed_at: datetime
    formation_observation_id: UUID
    applied_slots: int
    exact_slots: int
    unknown_slots: int
    skipped_slots: int

    @property
    def applied(self) -> bool:
        return self.applied_slots > 0


@dataclass(frozen=True, slots=True)
class MoraleObservation:
    id: UUID
    formation_snapshot_id: UUID
    instance_id: UUID
    fleet_index: int
    side: FormationFleetSide
    position: int
    canonical_identity: CanonicalShipIdentity
    ship_form: ShipForm
    baseline: Decimal | None
    observed_at: datetime
    recovery: MoraleRecoveryProfile
    source: str
    idempotency_key: str
    knowledge: MoraleKnowledge = MoraleKnowledge.EXACT
    location: MoraleLocation = MoraleLocation.UNKNOWN
    dorm_scan_id: UUID | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (self.id, self.formation_snapshot_id, self.instance_id)
        ):
            raise TypeError("Morale identity и provenance должны быть UUID")
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.side, FormationFleetSide):
            raise TypeError("side должен быть FormationFleetSide")
        if type(self.position) is not int or not 1 <= self.position <= 3:
            raise ValueError("position должен быть int в диапазоне 1..3")
        if not isinstance(self.canonical_identity, CanonicalShipIdentity):
            raise TypeError("canonical_identity должен быть CanonicalShipIdentity")
        _bounded(
            self.canonical_identity.key,
            field="canonical_identity",
            maximum=128,
        )
        if not isinstance(self.ship_form, ShipForm):
            raise TypeError("ship_form должен быть ShipForm")
        if self.knowledge is MoraleKnowledge.EXACT:
            _decimal(
                self.baseline,
                field="baseline",
                minimum=MORALE_MIN,
                maximum=MORALE_MAX,
            )
        elif self.knowledge is MoraleKnowledge.UNKNOWN:
            if self.baseline is not None:
                raise ValueError("UNKNOWN observation не должен содержать baseline")
        else:
            raise ValueError("Сохранённое observation должно быть exact или unknown")
        _aware(self.observed_at, field="observed_at")
        if not isinstance(self.recovery, MoraleRecoveryProfile):
            raise TypeError("recovery должен быть MoraleRecoveryProfile")
        _bounded(self.source, field="source", maximum=64)
        _bounded(self.idempotency_key, field="idempotency_key", maximum=128)
        if not isinstance(self.location, MoraleLocation):
            raise TypeError("location должен быть MoraleLocation")
        if self.location is MoraleLocation.UNKNOWN:
            if self.dorm_scan_id is not None:
                raise ValueError("Unknown location не должна ссылаться на Dorm scan")
        elif not isinstance(self.dorm_scan_id, UUID):
            raise ValueError("Наблюдаемая location требует Dorm scan provenance")


@dataclass(frozen=True, slots=True)
class MoraleProjection:
    value: Decimal
    knowledge: MoraleKnowledge
    elapsed_ticks: int


def project_morale(observation: MoraleObservation, *, at: datetime) -> MoraleProjection:
    """Спроецировать morale по завершённым серверным интервалам в шесть минут."""

    if not isinstance(observation, MoraleObservation):
        raise TypeError("observation должен быть MoraleObservation")
    if (
        observation.knowledge is not MoraleKnowledge.EXACT
        or observation.baseline is None
    ):
        raise ValueError("Проекция требует exact morale baseline")
    at = _aware(at, field="at")
    elapsed = at.astimezone(UTC) - observation.observed_at.astimezone(UTC)
    if elapsed.days < 0:
        raise ValueError("at не должен предшествовать observed_at")
    elapsed_microseconds = (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )
    elapsed_ticks = elapsed_microseconds // _RECOVERY_TICK_MICROSECONDS
    baseline = observation.baseline
    ceiling = observation.recovery.recovery_ceiling
    if baseline >= ceiling:
        value = baseline
    else:
        recovered = (
            observation.recovery.recovery_per_hour
            * Decimal(elapsed_ticks)
            / _TICKS_PER_HOUR
        )
        value = min(baseline + recovered, ceiling, MORALE_MAX)
    return MoraleProjection(
        value=value,
        knowledge=(
            MoraleKnowledge.EXACT if elapsed_ticks == 0 else MoraleKnowledge.PROJECTED
        ),
        elapsed_ticks=elapsed_ticks,
    )


def morale_ready_at(
    slot: MoraleSlotState,
    *,
    target: Decimal,
    at: datetime,
) -> datetime | None:
    """Вернуть первый подтверждённый tick, когда известный слот достигнет target.

    Для UNKNOWN нельзя вычислять время восстановления: ``None`` означает отсутствие
    доказательства, а не нулевую или предполагаемую мораль.
    """

    if not isinstance(slot, MoraleSlotState):
        raise TypeError("slot должен быть MoraleSlotState")
    at = _aware(at, field="at")
    _decimal(target, field="target", minimum=MORALE_MIN, maximum=MORALE_MAX)
    if (
        slot.knowledge is MoraleKnowledge.UNKNOWN
        or slot.baseline is None
        or slot.current is None
        or slot.recovery is None
        or slot.observed_at is None
    ):
        return None
    if slot.current >= target:
        return at
    if target > slot.recovery.recovery_ceiling:
        return None
    if slot.recovery.recovery_per_hour <= MORALE_MIN:
        return None
    required = (target - slot.baseline) * _TICKS_PER_HOUR
    ticks = int(
        (required / slot.recovery.recovery_per_hour).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return max(
        at,
        slot.observed_at
        + timedelta(minutes=6 * max(ticks, 0)),
    )


@dataclass(frozen=True, slots=True)
class MoraleSlotState:
    fleet_index: int
    side: FormationFleetSide
    position: int
    occupied: bool | None
    identity_status: IdentityStatus | None
    canonical_identity: CanonicalShipIdentity | None
    canonical_name: str | None
    ship_form: ShipForm | None
    knowledge: MoraleKnowledge
    baseline: Decimal | None = None
    current: Decimal | None = None
    recovery: MoraleRecoveryProfile | None = None
    observed_at: datetime | None = None
    source: str | None = None
    morale_observation_id: UUID | None = None
    location: MoraleLocation = MoraleLocation.UNKNOWN
    dorm_scan_id: UUID | None = None

    def __post_init__(self) -> None:
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.side, FormationFleetSide):
            raise TypeError("side должен быть FormationFleetSide")
        if type(self.position) is not int or not 1 <= self.position <= 3:
            raise ValueError("position должен быть int в диапазоне 1..3")
        if self.occupied is not None and type(self.occupied) is not bool:
            raise TypeError("occupied должен быть bool или None")
        if not isinstance(self.knowledge, MoraleKnowledge):
            raise TypeError("knowledge должен быть MoraleKnowledge")
        if not isinstance(self.location, MoraleLocation):
            raise TypeError("location должен быть MoraleLocation")
        exact_fields = (
            self.baseline,
            self.current,
        )
        evidence_fields = (
            self.recovery,
            self.observed_at,
            self.source,
            self.morale_observation_id,
        )
        if self.knowledge is MoraleKnowledge.UNKNOWN:
            if any(value is not None for value in exact_fields):
                raise ValueError("UNKNOWN slot не должен содержать exact morale")
            if self.recovery is None:
                if any(value is not None for value in evidence_fields[1:]):
                    raise ValueError("UNKNOWN slot содержит неполное recovery evidence")
                if (
                    self.location is not MoraleLocation.UNKNOWN
                    or self.dorm_scan_id is not None
                ):
                    raise ValueError(
                        "UNKNOWN без evidence должен иметь unknown location"
                    )
            elif any(value is None for value in evidence_fields):
                raise ValueError("Известный recovery context требует полное evidence")
        elif any(value is None for value in (*exact_fields, *evidence_fields)):
            raise ValueError("Known slot должен содержать полное morale state")
        if self.location is MoraleLocation.UNKNOWN:
            if self.dorm_scan_id is not None:
                raise ValueError("Unknown location не должна ссылаться на Dorm scan")
        elif not isinstance(self.dorm_scan_id, UUID):
            raise ValueError("Наблюдаемая location требует Dorm scan provenance")


@dataclass(frozen=True, slots=True)
class MoraleFleetState:
    fleet_index: int
    formation_observation_id: UUID | None
    formation_observed_at: datetime | None
    slots: tuple[MoraleSlotState, ...]

    def __post_init__(self) -> None:
        validate_surface_fleet_index(self.fleet_index)
        if not isinstance(self.slots, tuple) or len(self.slots) != 6:
            raise ValueError("Morale fleet state должен содержать шесть слотов")
        if tuple((slot.side, slot.position) for slot in self.slots) != _SLOT_ORDER:
            raise ValueError("Morale fleet state содержит неверный порядок слотов")
        if (self.formation_observation_id is None) != (
            self.formation_observed_at is None
        ):
            raise ValueError(
                "Formation provenance должен быть полным или отсутствовать"
            )


@dataclass(frozen=True, slots=True)
class MoraleSelectionState:
    selection: FleetSelection
    fleets: tuple[MoraleFleetState, ...]
    projected_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        if (
            tuple(item.fleet_index for item in self.fleets)
            != self.selection.fleet_indices
        ):
            raise ValueError("Morale fleets не соответствуют selection")
        _aware(self.projected_at, field="projected_at")


class MoraleRepository(Protocol):
    def append(self, observation: MoraleObservation) -> MoraleObservation: ...

    def latest(
        self,
        instance_id: UUID,
        selection: FleetSelection,
    ) -> tuple[MoraleObservation, ...]: ...

    def contains_idempotency(
        self,
        instance_id: UUID,
        keys: tuple[str, ...],
    ) -> frozenset[str]: ...


class MoraleUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_state: FleetStateRepository
    morale: MoraleRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class MoraleService:
    """Transport-neutral API записи baseline и чтения проекции по Fleet State."""

    def __init__(
        self,
        uow_factory: Callable[[], MoraleUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def _now(self) -> datetime:
        return _aware(self._clock(), field="clock")

    def now(self) -> datetime:
        """Вернуть единые часы application projection для потребителей чтения."""

        return self._now()

    def _transaction(
        self,
        instance: str,
        operation: Callable[[MoraleUnitOfWork, UUID], _T],
    ) -> _T:
        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            result = operation(uow, instance_id)
            uow.commit()
            return result

    def record(
        self,
        instance: str,
        command: RecordMoraleObservation,
    ) -> MoraleObservation:
        if not isinstance(command, RecordMoraleObservation):
            raise TypeError("command должен быть RecordMoraleObservation")
        now = self._now()
        observed_at = command.observed_at or now
        if observed_at.astimezone(UTC) > now.astimezone(UTC):
            raise ValueError("observed_at не должен находиться в будущем")

        def operation(uow: MoraleUnitOfWork, instance_id: UUID) -> MoraleObservation:
            formations = uow.fleet_state.latest(
                instance_id,
                FleetSelection.one(command.fleet_index),
            )
            if not formations:
                raise MoraleContinuityError(
                    "Для флота отсутствует сохранённый Fleet State"
                )
            formation = formations[0]
            if observed_at.astimezone(UTC) < formation.observed_at.astimezone(UTC):
                raise MoraleContinuityError(
                    "Morale observation предшествует доказанному Fleet State"
                )
            slot = formation.snapshot.slots[
                _SLOT_ORDER.index((command.side, command.position))
            ]
            self._require_current_occupant(slot, command)
            observation = MoraleObservation(
                id=self._id_factory(),
                formation_snapshot_id=formation.id,
                instance_id=instance_id,
                fleet_index=command.fleet_index,
                side=command.side,
                position=command.position,
                canonical_identity=command.canonical_identity,
                ship_form=command.ship_form,
                baseline=command.baseline,
                observed_at=observed_at,
                recovery=command.recovery,
                source=command.source,
                idempotency_key=command.idempotency_key,
            )
            return uow.morale.append(observation)

        return self._transaction(instance, operation)

    def apply_event(
        self,
        instance: str,
        command: RecordMoraleEvent,
    ) -> MoraleEventResult:
        """Применить одно подтверждённое событие к каждому текущему ship slot.

        Observation ledger одновременно хранит доказательство события и новое
        состояние. Повторная доставка того же ``event_key`` не создаёт новую
        deduction: уникальный slot-scoped idempotency key проверяется в той же
        транзакции до append.
        """

        if not isinstance(command, RecordMoraleEvent):
            raise TypeError("command должен быть RecordMoraleEvent")
        now = self._now()
        observed_at = command.observed_at or now
        if observed_at.astimezone(UTC) > now.astimezone(UTC):
            raise ValueError("observed_at не должен находиться в будущем")
        selection = FleetSelection.one(command.fleet_index)

        def operation(uow: MoraleUnitOfWork, instance_id: UUID) -> MoraleEventResult:
            formations = uow.fleet_state.latest(instance_id, selection)
            if not formations:
                raise MoraleContinuityError(
                    "Для morale event отсутствует сохранённый Fleet State"
                )
            formation = formations[0]
            if observed_at.astimezone(UTC) < formation.observed_at.astimezone(UTC):
                raise MoraleContinuityError(
                    "Morale event предшествует доказанному Fleet State"
                )

            latest = uow.morale.latest(instance_id, selection)
            latest_by_slot = {
                (item.fleet_index, item.side, item.position): item
                for item in latest
            }
            current_slots = tuple(
                slot
                for slot in formation.snapshot.slots
                if slot.occupied and slot.identity_status is IdentityStatus.MATCHED
            )
            slot_keys = tuple(
                self._event_idempotency_key(command, slot)
                for slot in current_slots
            )
            existing_keys = self._contains_idempotency(
                uow.morale,
                instance_id,
                slot_keys,
            )
            applied_slots = 0
            exact_slots = 0
            unknown_slots = 0
            skipped_slots = 0
            for slot, slot_key in zip(current_slots, slot_keys, strict=True):
                previous = latest_by_slot.get(
                    (command.fleet_index, slot.side, slot.position)
                )
                if slot_key in existing_keys or (
                    previous is not None and previous.idempotency_key == slot_key
                ):
                    skipped_slots += 1
                    continue
                observation = self._event_observation(
                    command,
                    formation,
                    slot,
                    previous,
                    instance_id=instance_id,
                    observed_at=observed_at,
                    idempotency_key=slot_key,
                )
                stored = uow.morale.append(observation)
                if stored.id != observation.id:
                    skipped_slots += 1
                    continue
                applied_slots += 1
                if observation.knowledge is MoraleKnowledge.EXACT:
                    exact_slots += 1
                else:
                    unknown_slots += 1

            return MoraleEventResult(
                event_key=command.event_key,
                kind=command.kind,
                fleet_index=command.fleet_index,
                observed_at=observed_at,
                formation_observation_id=formation.id,
                applied_slots=applied_slots,
                exact_slots=exact_slots,
                unknown_slots=unknown_slots,
                skipped_slots=skipped_slots,
            )

        return self._transaction(instance, operation)

    def record_warning(
        self,
        instance: str,
        *,
        fleet_index: int,
        event_key: str,
        observed_at: datetime | None = None,
    ) -> MoraleEventResult:
        """Зафиксировать low-morale evidence и инвалидировать exact projection."""

        return self.apply_event(
            instance,
            RecordMoraleEvent(
                fleet_index=fleet_index,
                kind=MoraleEventKind.WARNING,
                cost=MORALE_MIN,
                source="combat:low_morale_warning",
                event_key=event_key,
                observed_at=observed_at,
            ),
        )

    @staticmethod
    def _event_idempotency_key(
        command: RecordMoraleEvent,
        slot: FormationFleetSlotObservation,
    ) -> str:
        return (
            f"morale-event-v1:{command.event_key}:{command.fleet_index}:"
            f"{slot.side.value}:{slot.position}"
        )

    @staticmethod
    def _contains_idempotency(
        repository: MoraleRepository,
        instance_id: UUID,
        keys: tuple[str, ...],
    ) -> frozenset[str]:
        if not keys:
            return frozenset()
        contains = getattr(repository, "contains_idempotency", None)
        if callable(contains):
            return frozenset(contains(instance_id, keys))
        # Совместимость с минимальными in-memory repository doubles старых тестов.
        return frozenset()

    def _event_observation(
        self,
        command: RecordMoraleEvent,
        formation: FleetStateObservation,
        slot: FormationFleetSlotObservation,
        previous: MoraleObservation | None,
        *,
        instance_id: UUID,
        observed_at: datetime,
        idempotency_key: str,
    ) -> MoraleObservation:
        recovery = MoraleRecoveryProfile.outside_dorm_base()
        baseline: Decimal | None = None
        knowledge = MoraleKnowledge.UNKNOWN
        if (
            command.kind is not MoraleEventKind.WARNING
            and previous is not None
            and previous.knowledge is MoraleKnowledge.EXACT
            and previous.baseline is not None
            and previous.observed_at.astimezone(UTC)
            <= observed_at.astimezone(UTC)
            and previous.canonical_identity == slot.canonical_identity
            and previous.ship_form is slot.ship_form
        ):
            projection = project_morale(previous, at=observed_at)
            baseline = max(MORALE_MIN, projection.value - command.cost)
            knowledge = MoraleKnowledge.EXACT

        return MoraleObservation(
            id=self._id_factory(),
            formation_snapshot_id=formation.id,
            instance_id=instance_id,
            fleet_index=formation.fleet_index,
            side=slot.side,
            position=slot.position,
            canonical_identity=slot.canonical_identity,
            ship_form=slot.ship_form,
            baseline=baseline,
            observed_at=observed_at,
            recovery=recovery,
            source=command.source,
            idempotency_key=idempotency_key,
            knowledge=knowledge,
            location=MoraleLocation.UNKNOWN,
        )

    @staticmethod
    def _require_current_occupant(
        slot: FormationFleetSlotObservation,
        command: RecordMoraleObservation,
    ) -> None:
        if not slot.occupied or slot.identity_status is not IdentityStatus.MATCHED:
            raise MoraleContinuityError(
                "Morale observation требует однозначно распознанный занятый слот"
            )
        if (
            slot.canonical_identity != command.canonical_identity
            or slot.ship_form is not command.ship_form
        ):
            raise MoraleContinuityError(
                "Morale observation не соответствует текущему occupant Fleet slot"
            )

    def fleet(
        self,
        instance: str,
        fleet_index: int,
        *,
        at: datetime | None = None,
    ) -> MoraleFleetState:
        state = self.state(instance, FleetSelection.one(fleet_index), at=at)
        return state.fleets[0]

    def state(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        at: datetime | None = None,
    ) -> MoraleSelectionState:
        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        projected_at = self._now() if at is None else _aware(at, field="at")

        def operation(uow: MoraleUnitOfWork, instance_id: UUID) -> MoraleSelectionState:
            return self.state_in_uow(
                uow,
                instance_id,
                selection,
                at=projected_at,
            )

        return self._transaction(instance, operation)

    def state_in_uow(
        self,
        uow: MoraleUnitOfWork,
        instance_id: UUID,
        selection: FleetSelection,
        *,
        at: datetime | None = None,
    ) -> MoraleSelectionState:
        """Прочитать projection в уже открытой транзакции приложения."""

        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        if not isinstance(instance_id, UUID):
            raise TypeError("instance_id должен быть UUID")
        projected_at = self._now() if at is None else _aware(at, field="at")
        formations = uow.fleet_state.latest(instance_id, selection)
        morale = uow.morale.latest(instance_id, selection)
        return self.state_from_observations(
            selection,
            formations,
            morale,
            at=projected_at,
        )

    def state_from_observations(
        self,
        selection: FleetSelection,
        formations: tuple[FleetStateObservation, ...],
        morale: tuple[MoraleObservation, ...],
        *,
        at: datetime | None = None,
    ) -> MoraleSelectionState:
        """Собрать projection из уже выполненных set-based repository reads."""

        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        projected_at = self._now() if at is None else _aware(at, field="at")
        formation_by_fleet = {item.fleet_index: item for item in formations}
        morale_by_slot = {
            (item.fleet_index, item.side, item.position): item for item in morale
        }
        fleets = tuple(
            self._fleet_state(
                fleet_index,
                formation_by_fleet.get(fleet_index),
                morale_by_slot,
                projected_at,
            )
            for fleet_index in selection.fleet_indices
        )
        return MoraleSelectionState(selection, fleets, projected_at)

    @staticmethod
    def _fleet_state(
        fleet_index: int,
        formation: FleetStateObservation | None,
        morale_by_slot: dict[tuple[int, FormationFleetSide, int], MoraleObservation],
        projected_at: datetime,
    ) -> MoraleFleetState:
        if formation is None:
            slots = tuple(
                MoraleSlotState(
                    fleet_index=fleet_index,
                    side=side,
                    position=position,
                    occupied=None,
                    identity_status=None,
                    canonical_identity=None,
                    canonical_name=None,
                    ship_form=None,
                    knowledge=MoraleKnowledge.UNKNOWN,
                )
                for side, position in _SLOT_ORDER
            )
            return MoraleFleetState(fleet_index, None, None, slots)

        slots = tuple(
            MoraleService._slot_state(
                fleet_index,
                slot,
                morale_by_slot.get((fleet_index, slot.side, slot.position)),
                projected_at,
            )
            for slot in formation.snapshot.slots
        )
        return MoraleFleetState(
            fleet_index,
            formation.id,
            formation.observed_at,
            slots,
        )

    @staticmethod
    def _slot_state(
        fleet_index: int,
        slot: FormationFleetSlotObservation,
        observation: MoraleObservation | None,
        projected_at: datetime,
    ) -> MoraleSlotState:
        common = {
            "fleet_index": fleet_index,
            "side": slot.side,
            "position": slot.position,
            "occupied": slot.occupied,
            "identity_status": slot.identity_status,
            "canonical_identity": slot.canonical_identity,
            "canonical_name": slot.canonical_name,
            "ship_form": slot.ship_form,
        }
        if (
            slot.identity_status is not IdentityStatus.MATCHED
            or observation is None
            or observation.canonical_identity != slot.canonical_identity
            or observation.ship_form is not slot.ship_form
        ):
            return MoraleSlotState(**common, knowledge=MoraleKnowledge.UNKNOWN)
        if observation.knowledge is MoraleKnowledge.UNKNOWN:
            return MoraleSlotState(
                **common,
                knowledge=MoraleKnowledge.UNKNOWN,
                recovery=observation.recovery,
                observed_at=observation.observed_at,
                source=observation.source,
                morale_observation_id=observation.id,
                location=observation.location,
                dorm_scan_id=observation.dorm_scan_id,
            )
        projection = project_morale(observation, at=projected_at)
        return MoraleSlotState(
            **common,
            knowledge=projection.knowledge,
            baseline=observation.baseline,
            current=projection.value,
            recovery=observation.recovery,
            observed_at=observation.observed_at,
            source=observation.source,
            morale_observation_id=observation.id,
            location=observation.location,
            dorm_scan_id=observation.dorm_scan_id,
        )


__all__ = (
    "MORALE_MAX",
    "MORALE_MIN",
    "OUTSIDE_DORM_RECOVERY_CEILING",
    "OUTSIDE_DORM_RECOVERY_PER_HOUR",
    "MoraleContinuityError",
    "MoraleEventKind",
    "MoraleEventResult",
    "MoraleFleetState",
    "MoraleKnowledge",
    "MoraleLocation",
    "MoraleObservation",
    "MoraleProjection",
    "MoraleRecoveryProfile",
    "MoraleSelectionState",
    "MoraleService",
    "MoraleSlotState",
    "RecordMoraleEvent",
    "RecordMoraleObservation",
    "morale_ready_at",
    "project_morale",
)
