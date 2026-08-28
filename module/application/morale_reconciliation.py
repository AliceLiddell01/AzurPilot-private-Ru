"""Сопоставление множества Dorm-фактов с актуальным физическим Fleet State."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    project_morale,
)
from module.application.storage_ports import StorageUnitOfWork
from module.dock_inventory.model import IdentityStatus
from module.dorm.morale_model import (
    DormFloor,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.formation.model import FleetSelection, FormationFleetSlotObservation


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
class MoraleReconciliationResult:
    dorm_scan_id: UUID
    complete_scan: bool
    exact_observations: int
    outside_dorm_observations: int
    ambiguous_observations: int
    unresolved_observations: int
    unmatched_observations: int
    stale_fleet_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _FleetCandidate:
    formation: FleetStateObservation
    slot: FormationFleetSlotObservation

    @property
    def key(self) -> tuple[int, object, int]:
        return self.formation.fleet_index, self.slot.side, self.slot.position


class MoraleReconciliationService:
    """Сохранить происхождение Dorm scan и консервативный morale-контекст по слотам."""

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
            previous_by_slot = {
                (item.fleet_index, item.side, item.position): item
                for item in uow.morale.latest(instance_id, selection)
            }
            observations = stored_scan.observations
            candidate_map: dict[int, tuple[_FleetCandidate, ...]] = {}
            identity_candidates: dict[int, tuple[_FleetCandidate, ...]] = {}
            unresolved_count = 0
            unmatched_count = 0
            for index, observation in enumerate(observations):
                if (
                    observation.identity_status is not IdentityStatus.MATCHED
                    or observation.canonical_identity is None
                ):
                    unresolved_count += 1
                    continue
                same_identity = tuple(
                    candidate
                    for candidate in candidates
                    if candidate.slot.canonical_identity
                    == observation.canonical_identity
                )
                identity_candidates[index] = same_identity
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
                observation = observations[index]
                uow.morale.append(
                    self._exact_observation(
                        instance_id,
                        stored_scan,
                        candidate,
                        observation,
                    )
                )
                exact_count += 1

            outside_count = 0
            if stored_scan.complete:
                blocked_keys = {
                    candidate.key
                    for compatible in identity_candidates.values()
                    for candidate in compatible
                }
                if unresolved_count:
                    blocked_keys.update(candidate.key for candidate in candidates)
                for candidate in candidates:
                    if candidate.key in assigned_keys or candidate.key in blocked_keys:
                        continue
                    uow.morale.append(
                        self._outside_observation(
                            instance_id,
                            stored_scan,
                            candidate,
                            previous_by_slot.get(candidate.key),
                        )
                    )
                    outside_count += 1

            uow.commit()
            return MoraleReconciliationResult(
                dorm_scan_id=stored_scan.id,
                complete_scan=stored_scan.complete,
                exact_observations=exact_count,
                outside_dorm_observations=outside_count,
                ambiguous_observations=ambiguous_count,
                unresolved_observations=unresolved_count,
                unmatched_observations=unmatched_count,
                stale_fleet_indices=tuple(sorted(stale_fleets)),
            )

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

    def _outside_observation(
        self,
        instance_id: UUID,
        scan: DormMoraleScanResult,
        candidate: _FleetCandidate,
        previous: MoraleObservation | None,
    ) -> MoraleObservation:
        slot = candidate.slot
        continuity = (
            previous is not None
            and previous.knowledge is MoraleKnowledge.EXACT
            and previous.baseline is not None
            and previous.observed_at.astimezone(UTC)
            <= scan.finished_at.astimezone(UTC)
            and previous.canonical_identity == slot.canonical_identity
            and previous.ship_form is slot.ship_form
        )
        baseline = (
            project_morale(previous, at=scan.finished_at).value
            if continuity and previous is not None
            else MoraleRecoveryProfile.outside_dorm_base().recovery_ceiling
        )
        return MoraleObservation(
            id=self._id_factory(),
            formation_snapshot_id=candidate.formation.id,
            instance_id=instance_id,
            fleet_index=candidate.formation.fleet_index,
            side=slot.side,
            position=slot.position,
            canonical_identity=slot.canonical_identity,
            ship_form=slot.ship_form,
            baseline=baseline,
            observed_at=scan.finished_at,
            recovery=MoraleRecoveryProfile.outside_dorm_base(),
            source=(
                "dorm_reconciliation:outside_continuity"
                if continuity
                else "dorm_reconciliation:outside_initial"
            ),
            idempotency_key=(
                f"dorm-reconcile-v1:{scan.id}:{candidate.formation.fleet_index}:"
                f"{slot.side.value}:{slot.position}"
            ),
            knowledge=MoraleKnowledge.EXACT,
            location=MoraleLocation.OUTSIDE_DORM,
            dorm_scan_id=scan.id,
        )


__all__ = (
    "DormMoraleRepository",
    "MoraleReconciliationResult",
    "MoraleReconciliationService",
    "MoraleReconciliationUnitOfWork",
)
