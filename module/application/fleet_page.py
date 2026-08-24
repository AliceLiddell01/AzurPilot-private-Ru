"""Нейтральная к представлению query-модель для WebUI-страницы «Флоты»."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from module.application.fleet_manual_scan import FleetManualScanCommand
from module.application.fleet_state import FleetStateObservation, FleetStateRepository
from module.application.instance_identity import resolve_runtime_instance
from module.application.storage_ports import StorageUnitOfWork
from module.dock_inventory.model import IdentityStatus
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    SUPPORTED_SURFACE_FLEET_INDICES,
)


class FleetSlotState(StrEnum):
    EMPTY = "empty"
    MATCHED = "matched"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class FleetSlotViewModel:
    side: FormationFleetSide
    position: int
    state: FleetSlotState
    canonical_identity: str | None
    canonical_name: str | None
    displayed_name: str | None


@dataclass(frozen=True, slots=True)
class FleetRowViewModel:
    fleet_index: int
    slots: tuple[FleetSlotViewModel, ...]
    observed_at: datetime | None
    complete: bool | None


@dataclass(frozen=True, slots=True)
class FleetPageViewModel:
    instance: str
    rows: tuple[FleetRowViewModel, ...]
    manual_command: FleetManualScanCommand | None


class FleetPageCommandRepository(Protocol):
    def latest(self, instance_id: UUID) -> FleetManualScanCommand | None: ...


class FleetPageUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_state: FleetStateRepository
    fleet_scan_commands: FleetPageCommandRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


def _slot_view(slot: FormationFleetSlotObservation) -> FleetSlotViewModel:
    if not slot.occupied:
        state = FleetSlotState.EMPTY
    elif slot.identity_status is IdentityStatus.MATCHED:
        state = FleetSlotState.MATCHED
    elif slot.identity_status is IdentityStatus.UNRESOLVED:
        state = FleetSlotState.UNRESOLVED
    elif slot.identity_status is IdentityStatus.AMBIGUOUS:
        state = FleetSlotState.AMBIGUOUS
    else:  # Domain-валидация должна делать эту ветку недостижимой.
        raise ValueError("Занятый Fleet slot содержит неизвестное identity state")
    return FleetSlotViewModel(
        side=slot.side,
        position=slot.position,
        state=state,
        canonical_identity=(
            slot.canonical_identity.key
            if slot.canonical_identity is not None
            else None
        ),
        canonical_name=slot.canonical_name,
        displayed_name=slot.displayed_name,
    )


def _row_view(
    fleet_index: int,
    observation: FleetStateObservation | None,
) -> FleetRowViewModel:
    if observation is None:
        return FleetRowViewModel(
            fleet_index=fleet_index,
            slots=(),
            observed_at=None,
            complete=None,
        )
    return FleetRowViewModel(
        fleet_index=fleet_index,
        slots=tuple(_slot_view(slot) for slot in observation.snapshot.slots),
        observed_at=observation.observed_at,
        complete=observation.snapshot.complete,
    )


class FleetPageQueryService:
    """Загружает все шесть флотов и последнюю команду одной application-транзакцией."""

    def __init__(self, uow_factory: Callable[[], FleetPageUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def view(self, instance: str) -> FleetPageViewModel:
        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            observations = uow.fleet_state.latest(instance_id, FleetSelection.all())
            command = uow.fleet_scan_commands.latest(instance_id)
            uow.commit()
        by_index = {item.fleet_index: item for item in observations}
        return FleetPageViewModel(
            instance=instance,
            rows=tuple(
                _row_view(fleet_index, by_index.get(fleet_index))
                for fleet_index in SUPPORTED_SURFACE_FLEET_INDICES
            ),
            manual_command=command,
        )


__all__ = [
    "FleetPageQueryService",
    "FleetPageViewModel",
    "FleetRowViewModel",
    "FleetSlotState",
    "FleetSlotViewModel",
]
