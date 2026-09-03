"""Нейтральная к представлению модель запросов для WebUI-страницы «Флоты»."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from module.application.fleet_manual_scan import FleetManualScanCommand
from module.application.fleet_mapping import WorkingFleetBinding
from module.application.fleet_state import FleetStateObservation, FleetStateRepository
from module.application.instance_identity import resolve_runtime_instance
from module.application.morale import (
    MoraleKnowledge,
    MoraleLocation,
    MoraleRepository,
    MoraleService,
    MoraleSlotState,
)
from module.application.storage_ports import StorageUnitOfWork
from module.dock_inventory.model import IdentityStatus, ShipForm
from module.formation.model import (
    SUPPORTED_SURFACE_FLEET_INDICES,
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
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
    ship_form: ShipForm | None

    def __post_init__(self) -> None:
        if self.state is FleetSlotState.MATCHED:
            if self.canonical_identity is None:
                raise ValueError("MATCHED Fleet view slot требует canonical identity")
            if self.canonical_name is None or not self.canonical_name.strip():
                raise ValueError("MATCHED Fleet view slot требует canonical name")
            if not isinstance(self.ship_form, ShipForm):
                raise ValueError("MATCHED Fleet view slot требует ship form")
        elif any(
            value is not None
            for value in (self.canonical_identity, self.canonical_name, self.ship_form)
        ):
            raise ValueError(
                "Только MATCHED Fleet view slot может содержать canonical identity/name/form"
            )

    @property
    def canonical_display_name(self) -> str | None:
        if self.state is not FleetSlotState.MATCHED:
            return None
        if self.ship_form is ShipForm.RETROFIT:
            return f"{self.canonical_name} (Retrofit)"
        return self.canonical_name


@dataclass(frozen=True, slots=True)
class FleetRowViewModel:
    fleet_index: int
    slots: tuple[FleetSlotViewModel, ...]
    observed_at: datetime | None
    complete: bool | None


@dataclass(frozen=True, slots=True)
class MoraleRowViewModel:
    """Строка только для чтения с моралью одного физического Fleet slot."""

    task: str
    role: str
    logical_fleet_index: int
    physical_fleet_index: int
    side: FormationFleetSide
    position: int
    canonical_identity: str
    ship_name: str
    ship_form: ShipForm
    knowledge: MoraleKnowledge
    current: Decimal | None
    recovery_per_hour: Decimal | None
    location: MoraleLocation
    source: str | None
    last_sync: datetime | None
    last_known: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_identity, str):
            raise TypeError("Morale row canonical identity должен быть строкой")
        if not isinstance(self.ship_name, str):
            raise TypeError("Morale row ship name должен быть строкой")
        if not self.canonical_identity.strip():
            raise ValueError("Morale row требует canonical identity")
        if not self.ship_name.strip():
            raise ValueError("Morale row требует ship name")
        if not isinstance(self.ship_form, ShipForm):
            raise TypeError("Morale row требует ShipForm")
        if not isinstance(self.knowledge, MoraleKnowledge):
            raise TypeError("Morale row требует MoraleKnowledge")
        if self.knowledge is MoraleKnowledge.UNKNOWN and self.current is not None:
            raise ValueError("UNKNOWN morale row не должен содержать current")
        if self.knowledge is not MoraleKnowledge.UNKNOWN and self.current is None:
            raise ValueError("Known morale row требует current")
        if type(self.last_known) is not bool:
            raise TypeError("last_known должен быть bool")


@dataclass(frozen=True, slots=True)
class FleetPageViewModel:
    instance: str
    rows: tuple[FleetRowViewModel, ...]
    manual_command: FleetManualScanCommand | None
    morale_rows: tuple[MoraleRowViewModel, ...] = ()


class FleetPageCommandRepository(Protocol):
    def latest(self, instance_id: UUID) -> FleetManualScanCommand | None: ...


class FleetPageUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_state: FleetStateRepository
    morale: MoraleRepository
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
    else:
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
        ship_form=slot.ship_form,
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


def _morale_row(
    binding: WorkingFleetBinding,
    slot: MoraleSlotState,
) -> MoraleRowViewModel:
    if (
        slot.canonical_identity is None
        or slot.canonical_name is None
        or slot.ship_form is None
    ):
        raise ValueError("Morale row может быть построен только для MATCHED slot")
    return MoraleRowViewModel(
        task=binding.task,
        role=binding.role,
        logical_fleet_index=binding.logical_fleet_index,
        physical_fleet_index=binding.physical_fleet_index,
        side=slot.side,
        position=slot.position,
        canonical_identity=slot.canonical_identity.key,
        ship_name=slot.canonical_name,
        ship_form=slot.ship_form,
        knowledge=slot.knowledge,
        current=slot.current,
        recovery_per_hour=(
            slot.recovery.recovery_per_hour if slot.recovery is not None else None
        ),
        location=slot.location,
        source=slot.source,
        last_sync=slot.observed_at,
    )


def _last_known_morale_row(slot: MoraleSlotState) -> MoraleRowViewModel:
    if (
        slot.canonical_identity is None
        or slot.canonical_name is None
        or slot.ship_form is None
    ):
        raise ValueError("Last-known morale row требует MATCHED slot")
    return MoraleRowViewModel(
        task="",
        role="",
        logical_fleet_index=0,
        physical_fleet_index=slot.fleet_index,
        side=slot.side,
        position=slot.position,
        canonical_identity=slot.canonical_identity.key,
        ship_name=slot.canonical_name,
        ship_form=slot.ship_form,
        knowledge=slot.knowledge,
        current=slot.current,
        recovery_per_hour=(
            slot.recovery.recovery_per_hour if slot.recovery is not None else None
        ),
        location=slot.location,
        source=slot.source,
        last_sync=slot.observed_at,
        last_known=True,
    )


class FleetPageQueryService:
    """Загружает состав и morale одним set-based чтением на страницу."""

    def __init__(
        self,
        uow_factory: Callable[[], FleetPageUnitOfWork],
        *,
        morale_service: MoraleService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._morale_service = morale_service or MoraleService(uow_factory)

    def view(
        self,
        instance: str,
        working_fleets: tuple[WorkingFleetBinding, ...] = (),
    ) -> FleetPageViewModel:
        if not isinstance(working_fleets, tuple):
            raise TypeError("working_fleets должен быть tuple")
        physical_indices = tuple(
            binding.physical_fleet_index for binding in working_fleets
        )
        if len(set(physical_indices)) != len(physical_indices):
            raise ValueError("working_fleets не должен содержать дублирующий physical fleet")
        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            observations = uow.fleet_state.latest(instance_id, FleetSelection.all())
            command = uow.fleet_scan_commands.latest(instance_id)
            selection = (
                FleetSelection.several(*physical_indices)
                if physical_indices
                else FleetSelection.all()
            )
            morale_observations = uow.morale.latest(instance_id, selection)
            morale_state = None
            if morale_observations or physical_indices:
                morale_state = self._morale_service.state_from_observations(
                    selection,
                    observations,
                    morale_observations,
                    at=self._morale_service.now(),
                )
            uow.commit()

        by_index = {item.fleet_index: item for item in observations}
        morale_by_index = (
            {item.fleet_index: item for item in morale_state.fleets}
            if morale_state is not None
            else {}
        )
        if working_fleets:
            morale_rows = tuple(
                _morale_row(binding, slot)
                for binding in working_fleets
                for fleet in (morale_by_index.get(binding.physical_fleet_index),)
                if fleet is not None
                for slot in fleet.slots
                if slot.occupied and slot.identity_status is IdentityStatus.MATCHED
            )
        else:
            persisted_keys = {
                (item.fleet_index, item.side, item.position)
                for item in morale_observations
            }
            if morale_state is None:
                morale_rows = ()
            else:
                morale_rows = tuple(
                    _last_known_morale_row(slot)
                    for fleet in morale_state.fleets
                    for slot in fleet.slots
                    if (
                        slot.occupied
                        and slot.identity_status is IdentityStatus.MATCHED
                        and (fleet.fleet_index, slot.side, slot.position)
                        in persisted_keys
                    )
                )

        return FleetPageViewModel(
            instance=instance,
            rows=tuple(
                _row_view(fleet_index, by_index.get(fleet_index))
                for fleet_index in SUPPORTED_SURFACE_FLEET_INDICES
            ),
            manual_command=command,
            morale_rows=morale_rows,
        )


__all__ = [
    "FleetPageQueryService",
    "FleetPageViewModel",
    "FleetRowViewModel",
    "FleetSlotState",
    "FleetSlotViewModel",
    "MoraleRowViewModel",
    "WorkingFleetBinding",
]
