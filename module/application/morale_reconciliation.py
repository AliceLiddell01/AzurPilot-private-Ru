"""Сопоставление Dorm-фактов только с кораблями текущих рабочих флотов."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID, uuid4

from module.application.fleet_state import FleetStateObservation, FleetStateRepository
from module.application.instance_identity import resolve_runtime_instance
from module.application.morale import (
    MORALE_MAX,
    MoraleKnowledge,
    MoraleLocation,
    MoraleObservation,
    MoraleRecoveryProfile,
    MoraleRepository,
)
from module.application.storage_ports import StorageUnitOfWork
from module.dock_inventory.model import (
    CanonicalShipIdentity,
    IdentityStatus,
    ShipForm,
)
from module.dorm.morale_model import (
    DormFloor,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
)


class DormMoraleRepository(Protocol):
    def append_scan(
        self,
        instance_id: UUID,
        scan: DormMoraleScanResult,
    ) -> DormMoraleScanResult: ...

    def latest(self, instance_id: UUID) -> DormMoraleScanResult | None: ...


class MoraleReconciliationUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_state: FleetStateRepository
    morale: MoraleRepository
    dorm_morale: DormMoraleRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class TargetedMoraleLookupTarget:
    """Один доказанный Formation slot, которому ещё нужен exact morale baseline."""

    fleet_index: int
    side: FormationFleetSide
    position: int
    canonical_identity: CanonicalShipIdentity
    canonical_name: str
    ship_form: ShipForm

    def __post_init__(self) -> None:
        if type(self.fleet_index) is not int or not 1 <= self.fleet_index <= 6:
            raise ValueError("fleet_index должен быть int в диапазоне 1..6")
        if not isinstance(self.side, FormationFleetSide):
            raise TypeError("side должен быть FormationFleetSide")
        if type(self.position) is not int or not 1 <= self.position <= 3:
            raise ValueError("position должен быть int в диапазоне 1..3")
        if not isinstance(self.canonical_identity, CanonicalShipIdentity):
            raise TypeError("canonical_identity должен быть CanonicalShipIdentity")
        if not isinstance(self.canonical_name, str) or not self.canonical_name.strip():
            raise ValueError("canonical_name должен быть непустой строкой")
        if not isinstance(self.ship_form, ShipForm):
            raise TypeError("ship_form должен быть ShipForm")

    @property
    def key(self) -> tuple[int, FormationFleetSide, int]:
        return self.fleet_index, self.side, self.position

    @property
    def search_query(self) -> str:
        """Поиск EN-клиента использует canonical base name и не hardcode'ит roster."""

        return self.canonical_name


@dataclass(frozen=True, slots=True)
class MoraleReconciliationResult:
    dorm_scan_id: UUID
    complete_scan: bool
    exact_observations: int
    outside_dorm_observations: int
    ambiguous_observations: int
    unresolved_observations: int
    unmatched_observations: int
    stale_fleet_indices: tuple[int, ...]
    target_count: int = 0
    lookup_targets: tuple[TargetedMoraleLookupTarget, ...] = ()

    @property
    def missing_target_count(self) -> int:
        return len(self.lookup_targets)


@dataclass(frozen=True, slots=True)
class _FleetCandidate:
    formation: FleetStateObservation
    slot: FormationFleetSlotObservation

    @property
    def key(self) -> tuple[int, FormationFleetSide, int]:
        return self.formation.fleet_index, self.slot.side, self.slot.position

    def lookup_target(self) -> TargetedMoraleLookupTarget:
        if (
            self.slot.canonical_identity is None
            or self.slot.canonical_name is None
            or self.slot.ship_form is None
        ):
            raise ValueError("Targeted lookup требует MATCHED Formation slot")
        return TargetedMoraleLookupTarget(
            fleet_index=self.formation.fleet_index,
            side=self.slot.side,
            position=self.slot.position,
            canonical_identity=self.slot.canonical_identity,
            canonical_name=self.slot.canonical_name,
            ship_form=self.slot.ship_form,
        )


class MoraleReconciliationService:
    """Сохранить Dorm evidence и выделить missing targets без выдуманного baseline."""

    def __init__(
        self,
        uow_factory: Callable[[], MoraleReconciliationUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock должен быть timezone-aware")
        return value

    @staticmethod
    def _candidates(
        formations: tuple[FleetStateObservation, ...],
    ) -> tuple[_FleetCandidate, ...]:
        return tuple(
            _FleetCandidate(formation, slot)
            for formation in formations
            for slot in formation.snapshot.slots
            if slot.occupied and slot.identity_status is IdentityStatus.MATCHED
        )

    @staticmethod
    def _location(floor: DormFloor) -> MoraleLocation:
        return (
            MoraleLocation.DORM_FLOOR_1
            if floor is DormFloor.FLOOR_1
            else MoraleLocation.DORM_FLOOR_2
        )

    @staticmethod
    def _floor_time(scan: DormMoraleScanResult, floor: DormFloor) -> datetime:
        attempt = next(item for item in scan.attempts if item.floor is floor)
        if attempt.observed_at is None:
            raise ValueError("Dorm observation не имеет floor timestamp")
        return attempt.observed_at

    def reconcile(
        self,
        instance: str,
        selection: FleetSelection,
        scan: DormMoraleScanResult,
    ) -> MoraleReconciliationResult:
        """Сопоставить Dorm только с target set текущих рабочих Formation fleets.

        Любой target без точного Train/Rest observation возвращается в
        ``lookup_targets``. Даже полный Dorm scan не превращает отсутствие в
        фиктивное ``119/EXACT``: exact current для такого target должен быть
        прочитан отдельным безопасным Search lookup.
        """

        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        if not isinstance(scan, DormMoraleScanResult):
            raise TypeError("scan должен быть DormMoraleScanResult")
        now = self._now()
        if scan.finished_at.astimezone(UTC) > now.astimezone(UTC):
            raise ValueError("Dorm scan не должен находиться в будущем")

        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            stored_scan = uow.dorm_morale.append_scan(instance_id, scan)
            formations = uow.fleet_state.latest(instance_id, selection)
            all_candidates = self._candidates(formations)
            stale_fleets = {
                item.fleet_index
                for item in formations
                if item.observed_at.astimezone(UTC) > scan.started_at.astimezone(UTC)
                or (
                    scan.catalog_fingerprint is not None
                    and item.snapshot.catalog_fingerprint != scan.catalog_fingerprint
                )
            }
            active_formations = tuple(
                item for item in formations if item.fleet_index not in stale_fleets
            )
            candidates = self._candidates(active_formations)
            observations = stored_scan.observations

            candidate_map: dict[int, tuple[_FleetCandidate, ...]] = {}
            unresolved_count = 0
            unmatched_count = 0
            for index, observation in enumerate(observations):
                if (
                    observation.identity_status is not IdentityStatus.MATCHED
                    or observation.canonical_identity is None
                ):
                    # Такой Dorm card может быть шумом или target, поэтому сам по себе
                    # не блокирует все targets. Нужные missing targets добирает Search.
                    unresolved_count += 1
                    continue
                same_identity = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.slot.canonical_identity == observation.canonical_identity
                )
                compatible = tuple(
                    candidate
                    for candidate in same_identity
                    if observation.ship_form is None
                    or candidate.slot.ship_form is observation.ship_form
                )
                candidate_map[index] = compatible
                if not compatible:
                    unmatched_count += 1

            demand = Counter(
                compatible[0].key
                for compatible in candidate_map.values()
                if len(compatible) == 1
            )
            matches: dict[int, _FleetCandidate] = {
                index: compatible[0]
                for index, compatible in candidate_map.items()
                if len(compatible) == 1 and demand[compatible[0].key] == 1
            }
            ambiguous_count = sum(
                bool(compatible)
                and not (len(compatible) == 1 and demand[compatible[0].key] == 1)
                for compatible in candidate_map.values()
            )

            exact_count = 0
            assigned_keys = {candidate.key for candidate in matches.values()}
            for index, candidate in matches.items():
                uow.morale.append(
                    self._exact_observation(
                        instance_id,
                        stored_scan,
                        candidate,
                        observations[index],
                    )
                )
                exact_count += 1

            lookup_targets = tuple(
                candidate.lookup_target()
                for candidate in candidates
                if candidate.key not in assigned_keys
            )
            uow.commit()
            return MoraleReconciliationResult(
                dorm_scan_id=stored_scan.id,
                complete_scan=stored_scan.complete,
                exact_observations=exact_count,
                # Reconciliation больше не объявляет outside без exact Search evidence.
                outside_dorm_observations=0,
                ambiguous_observations=ambiguous_count,
                unresolved_observations=unresolved_count,
                unmatched_observations=unmatched_count,
                stale_fleet_indices=tuple(sorted(stale_fleets)),
                target_count=len(all_candidates),
                lookup_targets=lookup_targets,
            )

    def record_targeted_outside(
        self,
        instance: str,
        target: TargetedMoraleLookupTarget,
        *,
        dorm_scan_id: UUID,
        morale: Decimal,
        observed_at: datetime,
    ) -> MoraleObservation:
        """Сохранить exact Search baseline корабля, доказанно находящегося вне Dorm."""

        if not isinstance(target, TargetedMoraleLookupTarget):
            raise TypeError("target должен быть TargetedMoraleLookupTarget")
        if not isinstance(dorm_scan_id, UUID):
            raise TypeError("dorm_scan_id должен быть UUID")
        if not isinstance(morale, Decimal) or not morale.is_finite():
            raise TypeError("morale должен быть конечным Decimal")
        if not Decimal(0) <= morale <= MORALE_MAX:
            raise ValueError("morale должен быть в диапазоне 0..150")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ValueError("observed_at должен быть timezone-aware")
        now = self._now()
        if observed_at.astimezone(UTC) > now.astimezone(UTC):
            raise ValueError("observed_at не должен находиться в будущем")

        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            latest_scan = uow.dorm_morale.latest(instance_id)
            if latest_scan is None or latest_scan.id != dorm_scan_id:
                raise ValueError("Targeted lookup должен ссылаться на последний Dorm scan")
            formations = uow.fleet_state.latest(
                instance_id,
                FleetSelection.one(target.fleet_index),
            )
            if len(formations) != 1:
                raise ValueError("Для targeted lookup не найден единственный Fleet State")
            formation = formations[0]
            if observed_at.astimezone(UTC) < formation.observed_at.astimezone(UTC):
                raise ValueError("Targeted lookup предшествует текущему Fleet State")
            slot = next(
                (
                    item
                    for item in formation.snapshot.slots
                    if item.side is target.side and item.position == target.position
                ),
                None,
            )
            if (
                slot is None
                or not slot.occupied
                or slot.identity_status is not IdentityStatus.MATCHED
                or slot.canonical_identity != target.canonical_identity
                or slot.ship_form is not target.ship_form
            ):
                raise ValueError("Formation continuity targeted lookup не доказана")

            observation = MoraleObservation(
                id=self._id_factory(),
                formation_snapshot_id=formation.id,
                instance_id=instance_id,
                fleet_index=target.fleet_index,
                side=target.side,
                position=target.position,
                canonical_identity=target.canonical_identity,
                ship_form=target.ship_form,
                baseline=morale,
                observed_at=observed_at,
                recovery=MoraleRecoveryProfile.outside_dorm_base(),
                source="targeted_search:exact",
                idempotency_key=(
                    f"targeted-morale-v1:{dorm_scan_id}:{target.fleet_index}:"
                    f"{target.side.value}:{target.position}"
                ),
                knowledge=MoraleKnowledge.EXACT,
                location=MoraleLocation.OUTSIDE_DORM,
                dorm_scan_id=dorm_scan_id,
            )
            stored = uow.morale.append(observation)
            uow.commit()
            return stored

    def _exact_observation(
        self,
        instance_id: UUID,
        scan: DormMoraleScanResult,
        candidate: _FleetCandidate,
        dorm: DormMoraleObservation,
    ) -> MoraleObservation:
        slot = candidate.slot
        return MoraleObservation(
            id=self._id_factory(),
            formation_snapshot_id=candidate.formation.id,
            instance_id=instance_id,
            fleet_index=candidate.formation.fleet_index,
            side=slot.side,
            position=slot.position,
            canonical_identity=slot.canonical_identity,
            ship_form=slot.ship_form,
            baseline=dorm.morale,
            observed_at=self._floor_time(scan, dorm.floor),
            recovery=MoraleRecoveryProfile(
                recovery_per_hour=dorm.recovery_per_hour,
                recovery_ceiling=MORALE_MAX,
                source=f"dorm_ui:{dorm.floor.value}",
            ),
            source="dorm_reconciliation:exact",
            idempotency_key=(
                f"dorm-reconcile-v1:{scan.id}:{candidate.formation.fleet_index}:"
                f"{slot.side.value}:{slot.position}"
            ),
            knowledge=MoraleKnowledge.EXACT,
            location=self._location(dorm.floor),
            dorm_scan_id=scan.id,
        )


__all__ = (
    "DormMoraleRepository",
    "MoraleReconciliationResult",
    "MoraleReconciliationService",
    "MoraleReconciliationUnitOfWork",
    "TargetedMoraleLookupTarget",
)
