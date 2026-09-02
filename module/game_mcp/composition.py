"""Ленивая сборка Game MCP поверх нейтральных application services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import cast

from module.application.errors import ServiceUnavailableError
from module.application.game_read_service import GameReadService
from module.application.legacy_adapters import (
    GeneratedTaskCatalogAdapter,
    LegacyInstanceRuntimeAdapter,
)
from module.application.legacy_game_adapters import (
    LegacyConfigAdapter,
    LegacyRuntimeLogAdapter,
    LegacyScreenshotAdapter,
)
from module.application.morale import MoraleUnitOfWork
from module.application.services import InstanceQueryService, TaskCatalogService

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class GameMcpEnvironment:
    """Минимальный environment contract для Game MCP persistence builder."""

    repository_root: Path


class GameMcpBackend:
    """Собрать read services и лениво подключить control owners."""

    def __init__(
        self,
        *,
        instance_reader: object | None = None,
        task_catalog: object | None = None,
        config_reader: object | None = None,
        log_reader: object | None = None,
        screenshot_reader: object | None = None,
        fleet_state_reader: object | None = None,
        morale_reader: object | None = None,
        persistence_factory: Callable[[GameMcpEnvironment], object] | None = None,
        repository_root: Path | None = None,
    ) -> None:
        if instance_reader is None:
            instance_reader = LegacyInstanceRuntimeAdapter()
        if task_catalog is None:
            task_catalog = GeneratedTaskCatalogAdapter.from_generated_sources()
        if config_reader is None:
            config_reader = LegacyConfigAdapter(task_catalog)  # type: ignore[arg-type]
        if log_reader is None:
            log_reader = LegacyRuntimeLogAdapter(
                (repository_root or _REPOSITORY_ROOT) / "log"
            )
        if screenshot_reader is None:
            screenshot_reader = LegacyScreenshotAdapter()

        self._instance_reader = instance_reader
        self._task_catalog = task_catalog
        self._config_reader = config_reader
        self.instances = InstanceQueryService(instance_reader)  # type: ignore[arg-type]
        self.tasks = TaskCatalogService(task_catalog)  # type: ignore[arg-type]
        self.read = GameReadService(
            instance_reader=instance_reader,  # type: ignore[arg-type]
            config_reader=config_reader,  # type: ignore[arg-type]
            log_reader=log_reader,  # type: ignore[arg-type]
            screenshot_reader=screenshot_reader,  # type: ignore[arg-type]
            scheduler_tasks=task_catalog,  # type: ignore[arg-type]
        )
        self._fleet_state = fleet_state_reader
        self._morale = morale_reader
        self._control: object | None = None
        self._persistence_factory = persistence_factory or _default_persistence
        self._repository_root = (repository_root or _REPOSITORY_ROOT).resolve()
        self._persistence: object | None = None
        self._closed = False
        self._service_lock = Lock()
        self._persistence_lock = Lock()

    @property
    def fleet_state(self) -> object:
        """Лениво вернуть transport-neutral Fleet State read service."""

        with self._service_lock:
            self._ensure_open()
            if self._fleet_state is None:
                from module.application.fleet_state import FleetStateReadService

                self._fleet_state = FleetStateReadService(self._uow_factory)
            return self._fleet_state

    @property
    def morale(self) -> object:
        """Лениво вернуть transport-neutral Morale read service."""

        with self._service_lock:
            self._ensure_open()
            if self._morale is None:
                from module.application.morale import MoraleService

                self._morale = MoraleService(self._uow_factory)
            return self._morale

    @property
    def control(self) -> object:
        """Лениво вернуть нейтральный Game control service."""

        with self._service_lock:
            self._ensure_open()
            if self._control is None:
                from module.application.game_control_service import GameControlService
                from module.application.legacy_game_adapters import (
                    LegacyAdbAdapter,
                    LegacyEmulatorAdapter,
                    LegacyProcessManagerAdapter,
                    legacy_current_time,
                )

                self._control = GameControlService(
                    instance_reader=self._instance_reader,  # type: ignore[arg-type]
                    config_schema=self._task_catalog,  # type: ignore[arg-type]
                    config_writer=self._config_reader,  # type: ignore[arg-type]
                    scheduler_tasks=self._task_catalog,  # type: ignore[arg-type]
                    lifecycle=LegacyProcessManagerAdapter(),
                    emulator=LegacyEmulatorAdapter(typed_failures=True),
                    adb=LegacyAdbAdapter(typed_failures=True),
                    clock=legacy_current_time,
                    config_reader=self._config_reader,
                    mutation_lock_root=self._repository_root,
                )
            return self._control

    @property
    def mutation_lock_root(self) -> Path:
        """Repository-scoped runtime root для общей profile mutation lock."""

        return self._repository_root

    def _uow_factory(self) -> MoraleUnitOfWork:
        composition = self._get_persistence()
        factory = getattr(composition, "uow_factory", None)
        if not callable(factory):
            raise TypeError(
                "Read-only persistence composition не предоставила uow_factory"
            )
        return cast(MoraleUnitOfWork, factory())

    def _get_persistence(self) -> object:
        with self._persistence_lock:
            self._ensure_open()
            if self._persistence is None:
                self._persistence = self._persistence_factory(
                    GameMcpEnvironment(self._repository_root)
                )
            return self._persistence

    def dispose(self) -> None:
        """Освободить engine, если domain read действительно его создавал."""

        with self._service_lock:
            with self._persistence_lock:
                if self._closed:
                    return
                self._closed = True
                self._fleet_state = None
                self._morale = None
                self._control = None
                persistence = self._persistence
                self._persistence = None
                if persistence is None:
                    return
                dispose = getattr(persistence, "dispose", None)
                if callable(dispose):
                    dispose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ServiceUnavailableError("Game MCP backend закрыт.")


def _default_persistence(environment: GameMcpEnvironment) -> object:
    """Получить нейтральную persistence composition без Dev Runtime."""

    from module.persistence.runtime import build_read_only_persistence_composition

    return build_read_only_persistence_composition(environment)


__all__ = ("GameMcpBackend", "GameMcpEnvironment")
