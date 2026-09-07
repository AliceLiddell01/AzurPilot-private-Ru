"""Единая нормализация подтверждённого состояния выполнения runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol

from module.application.game_models import CurrentTaskSnapshot, CurrentTaskState
from module.application.runtime_state import RuntimeStateSnapshot


class WorkerIdentityStatus(StrEnum):
    """Результат проверки записи worker registry."""

    ABSENT = "absent"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkerIdentityEvidence:
    """Проверенная или явно неподтверждённая identity worker."""

    status: WorkerIdentityStatus
    pid: int | None = None
    created_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WorkerIdentityStatus):
            raise TypeError("status должен быть WorkerIdentityStatus")
        if self.status is WorkerIdentityStatus.VERIFIED:
            if (
                isinstance(self.pid, bool)
                or not isinstance(self.pid, int)
                or self.pid <= 0
                or isinstance(self.created_at, bool)
                or not isinstance(self.created_at, (int, float))
                or not isfinite(float(self.created_at))
                or self.created_at <= 0
            ):
                raise ValueError("Проверенная identity worker должна содержать PID и created_at")
        elif self.pid is not None or self.created_at is not None:
            raise ValueError("Неподтверждённая identity worker не должна содержать поля процесса")


class RuntimeStateReader(Protocol):
    """Узкий read-only порт существующего RuntimeStateStore."""

    def read(self, profile: str) -> RuntimeStateSnapshot | None: ...


class WorkerIdentityReader(Protocol):
    """Порт проверки точной identity worker владельцем runtime."""

    def read_worker_identity(self, profile: str) -> WorkerIdentityEvidence: ...


class RuntimeExecutionReader:
    """Преобразовать runtime snapshot в единую семантику execution."""

    def __init__(
        self,
        state_reader: RuntimeStateReader,
        worker_identity_reader: WorkerIdentityReader,
    ) -> None:
        self._state_reader = state_reader
        self._worker_identity_reader = worker_identity_reader

    def read_current_task(self, profile: str) -> CurrentTaskSnapshot:
        """Вернуть running/idle/stopped/unknown без fallback к queue или логам."""

        try:
            snapshot = self._state_reader.read(profile)
        except Exception:  # noqa: BLE001 - повреждённое состояние обязано стать unknown.
            return self._unknown(profile)

        try:
            identity = self._worker_identity_reader.read_worker_identity(profile)
        except Exception:  # noqa: BLE001 - read boundary обязан быть fail-closed.
            return self._unknown(profile)
        if not isinstance(identity, WorkerIdentityEvidence):
            return self._unknown(profile)

        if snapshot is None:
            if identity.status is WorkerIdentityStatus.ABSENT:
                return CurrentTaskSnapshot(profile, None, CurrentTaskState.STOPPED)
            return self._unknown(profile)
        if not isinstance(snapshot, RuntimeStateSnapshot) or snapshot.profile != profile:
            return self._unknown(profile)

        if snapshot.freshness != "fresh":
            return self._unknown(profile)

        if snapshot.worker_running:
            if (
                identity.status is not WorkerIdentityStatus.VERIFIED
                or identity.pid != snapshot.worker_pid
                or identity.created_at != snapshot.worker_created_at
            ):
                return self._unknown(profile)
            if snapshot.busy and snapshot.current_task is not None:
                return CurrentTaskSnapshot(
                    profile,
                    snapshot.current_task,
                    CurrentTaskState.RUNNING,
                )
            if not snapshot.busy and snapshot.current_task is None:
                return CurrentTaskSnapshot(profile, None, CurrentTaskState.IDLE)
            return self._unknown(profile)

        if identity.status is WorkerIdentityStatus.ABSENT:
            return CurrentTaskSnapshot(profile, None, CurrentTaskState.STOPPED)
        return self._unknown(profile)

    @staticmethod
    def _unknown(profile: str) -> CurrentTaskSnapshot:
        return CurrentTaskSnapshot(profile, None, CurrentTaskState.UNKNOWN)


__all__ = [
    "RuntimeExecutionReader",
    "RuntimeStateReader",
    "WorkerIdentityEvidence",
    "WorkerIdentityReader",
    "WorkerIdentityStatus",
]
