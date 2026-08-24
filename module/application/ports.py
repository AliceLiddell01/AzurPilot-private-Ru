"""Минимальные read-only порты для прикладных сервисов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from module.application.models import TaskMetadata, TaskSummary


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Снимок legacy runtime без передачи ProcessManager через границу."""

    running: bool
    state_code: int


class InstanceRuntimeReader(Protocol):
    def list_instance_names(self) -> tuple[str, ...]: ...

    def read_instance_status(self, name: str) -> RuntimeSnapshot: ...


class TaskCatalogReader(Protocol):
    def list_task_summaries(self) -> tuple[TaskSummary, ...]: ...

    def read_task_metadata(self, name: str) -> TaskMetadata | None: ...
