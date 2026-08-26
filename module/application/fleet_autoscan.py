"""Исполнение автоматического сканирования Formation-флотов как Scheduler-задачи."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from module.application.fleet_state import FleetScanBatchResult
from module.formation.model import FleetSelection

FLEET_AUTOSCAN_SOURCE = "autoscan:scheduler"


@dataclass(frozen=True, slots=True)
class FleetAutoScanConfig:
    selection: FleetSelection

    def __post_init__(self) -> None:
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")

    @classmethod
    def from_raw(cls, fleet_indices: object) -> FleetAutoScanConfig:
        if not isinstance(fleet_indices, (list, tuple)):
            raise TypeError("FleetAutoScan.Fleets должен быть списком индексов")
        return cls(FleetSelection(tuple(fleet_indices)))


@dataclass(frozen=True, slots=True)
class FleetAutoScanExecution:
    source: str
    selection: FleetSelection
    batch_result: FleetScanBatchResult
    complete_fleet_indices: tuple[int, ...]
    incomplete_fleet_indices: tuple[int, ...]


class FleetAutoScanStateService(Protocol):
    def scan(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> FleetScanBatchResult: ...


class FleetAutoScanCoordinator:
    """Выполнить один scan выбранных флотов без собственного due-engine."""

    def __init__(self, state_service: FleetAutoScanStateService) -> None:
        self._state_service = state_service

    def run(
        self,
        instance: str,
        config: FleetAutoScanConfig,
    ) -> FleetAutoScanExecution:
        if not isinstance(config, FleetAutoScanConfig):
            raise TypeError("config должен быть FleetAutoScanConfig")

        selection = config.selection
        batch = self._state_service.scan(
            instance,
            selection,
            source=FLEET_AUTOSCAN_SOURCE,
        )
        complete = tuple(
            observation.fleet_index
            for observation in batch.observations
            if observation.snapshot.complete
        )
        complete_set = set(complete)
        incomplete = tuple(
            fleet_index
            for fleet_index in selection.fleet_indices
            if fleet_index not in complete_set
        )
        return FleetAutoScanExecution(
            source=FLEET_AUTOSCAN_SOURCE,
            selection=selection,
            batch_result=batch,
            complete_fleet_indices=complete,
            incomplete_fleet_indices=incomplete,
        )


__all__ = [
    "FLEET_AUTOSCAN_SOURCE",
    "FleetAutoScanConfig",
    "FleetAutoScanCoordinator",
    "FleetAutoScanExecution",
]
