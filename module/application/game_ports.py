"""Порты нейтральных read и control services game/runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from module.application.game_models import (
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
    DashboardResources,
    MediaFrame,
    SchedulerEntry,
)


class GameConfigReader(Protocol):
    def read_config(
        self,
        instance: str,
        task: str | None = None,
    ) -> Mapping[str, object]: ...

    def read_resources(self, instance: str) -> DashboardResources: ...

    def read_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: Sequence[str],
    ) -> Sequence[SchedulerEntry]: ...


class GameConfigWriter(Protocol):
    def update_config(self, request: ConfigUpdateRequest) -> None: ...

    def schedule_task(
        self,
        instance: str,
        task: str,
        scheduled_at: datetime,
    ) -> None: ...

    def clear_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: Sequence[str],
    ) -> Sequence[str]: ...


class GameConfigMetadata(Protocol):
    """Контракт metadata, необходимый legacy config adapter."""

    def read_argument_metadata(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> Mapping[str, object] | None: ...

    def redact_config(
        self,
        config_data: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def read_dashboard_resources(
        self,
        config_data: Mapping[str, object],
    ) -> DashboardResources: ...


class ConfigSchemaReader(Protocol):
    def read_argument_definition(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> ConfigArgumentDefinition | None: ...


class SchedulerTaskReader(Protocol):
    def list_schedulable_task_names(self) -> tuple[str, ...]: ...


class RuntimeLogReader(Protocol):
    def read_tail(self, instance: str, limit: int) -> Sequence[str]: ...

    def read_current_task(self, instance: str) -> str: ...


class ScreenshotReader(Protocol):
    def read_frame(self, instance: str) -> MediaFrame: ...


class InstanceLifecycleController(Protocol):
    def is_running(self, instance: str) -> bool: ...

    def start_instance(self, instance: str) -> bool: ...

    def stop_instance(self, instance: str) -> bool: ...


class EmulatorController(Protocol):
    def restart_emulator(self, instance: str) -> bool: ...


class AdbController(Protocol):
    def restart_adb(self, instance: str | None = None) -> bool: ...


__all__ = [
    "AdbController",
    "ConfigSchemaReader",
    "EmulatorController",
    "GameConfigMetadata",
    "GameConfigReader",
    "GameConfigWriter",
    "InstanceLifecycleController",
    "RuntimeLogReader",
    "SchedulerTaskReader",
    "ScreenshotReader",
]
