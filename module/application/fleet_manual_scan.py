"""Устойчивые команды ручного сканирования флотов и worker-оркестрация."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, TypeVar
from uuid import UUID, uuid4

from module.application.fleet_state import (
    FleetScanBatchResult,
    FleetScanRunStatus,
    FleetStateService,
)
from module.application.instance_identity import resolve_runtime_instance
from module.application.storage_ports import StorageUnitOfWork
from module.formation.model import FleetSelection

FLEET_MANUAL_SCAN_SOURCE = "manual:webui"
_T = TypeVar("_T")


def _aware(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} должен содержать timezone-aware datetime")
    return value


def _error_code(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("Неуспешная команда требует error_code")
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("error_code должен содержать от 1 до 64 символов")
    return value


class FleetManualScanStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FleetManualScanCommand:
    id: UUID
    instance_id: UUID
    selection: FleetSelection
    created_at: datetime
    status: FleetManualScanStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_run_id: UUID | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.instance_id, UUID):
            raise TypeError("Manual command identity должен быть UUID")
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        created_at = _aware(self.created_at, field="created_at")
        if not isinstance(self.status, FleetManualScanStatus):
            raise TypeError("status должен быть FleetManualScanStatus")
        if self.result_run_id is not None and not isinstance(self.result_run_id, UUID):
            raise TypeError("result_run_id должен быть UUID")
        if self.started_at is not None:
            started_at = _aware(self.started_at, field="started_at")
            if started_at.astimezone(UTC) < created_at.astimezone(UTC):
                raise ValueError("started_at не может быть раньше created_at")
        if self.finished_at is not None:
            finished_at = _aware(self.finished_at, field="finished_at")
            if self.started_at is None or (
                finished_at.astimezone(UTC) < self.started_at.astimezone(UTC)
            ):
                raise ValueError("finished_at требует корректный started_at")

        if self.status is FleetManualScanStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.finished_at,
                    self.result_run_id,
                    self.error_code,
                )
            ):
                raise ValueError("Pending command не содержит lifecycle result")
        elif self.status is FleetManualScanStatus.RUNNING:
            if self.started_at is None or any(
                value is not None
                for value in (
                    self.finished_at,
                    self.result_run_id,
                    self.error_code,
                )
            ):
                raise ValueError("Running command содержит некорректный lifecycle")
        elif self.status is FleetManualScanStatus.SUCCEEDED:
            if (
                self.started_at is None
                or self.finished_at is None
                or self.result_run_id is None
            ):
                raise ValueError("Succeeded command требует timestamps и result_run_id")
            _error_code(self.error_code, required=False)
            if self.error_code is not None:
                raise ValueError("Succeeded command не содержит error_code")
        elif self.status is FleetManualScanStatus.PARTIAL:
            if (
                self.started_at is None
                or self.finished_at is None
                or self.result_run_id is None
            ):
                raise ValueError("Partial command требует timestamps и result_run_id")
            _error_code(self.error_code, required=True)
        else:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("Failed command требует timestamps")
            _error_code(self.error_code, required=True)


@dataclass(frozen=True, slots=True)
class FleetManualScanSubmission:
    command: FleetManualScanCommand
    created: bool


@dataclass(frozen=True, slots=True)
class FleetManualScanExecution:
    command: FleetManualScanCommand
    batch_result: FleetScanBatchResult


class FleetManualScanCommandRepository(Protocol):
    def create_pending(
        self,
        instance_id: UUID,
        command_id: UUID,
        selection: FleetSelection,
        *,
        created_at: datetime,
    ) -> FleetManualScanSubmission: ...

    def latest(self, instance_id: UUID) -> FleetManualScanCommand | None: ...

    def pending_exists(self, instance_id: UUID) -> bool: ...

    def claim_next(
        self,
        instance_id: UUID,
        *,
        started_at: datetime,
    ) -> FleetManualScanCommand | None: ...

    def finish(
        self,
        command_id: UUID,
        instance_id: UUID,
        *,
        status: FleetManualScanStatus,
        finished_at: datetime,
        result_run_id: UUID | None,
        error_code: str | None,
    ) -> FleetManualScanCommand: ...

    def fail_running(
        self,
        instance_id: UUID,
        *,
        finished_at: datetime,
        error_code: str,
    ) -> int: ...


class FleetManualScanUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_scan_commands: FleetManualScanCommandRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class FleetManualScanCommandService:
    """Транзакционный lifecycle команды, общий для WebUI и worker-процесса."""

    def __init__(
        self,
        uow_factory: Callable[[], FleetManualScanUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def _now(self) -> datetime:
        return _aware(self._clock(), field="clock")

    def _transaction(
        self,
        instance: str,
        operation: Callable[[FleetManualScanUnitOfWork, UUID], _T],
    ) -> _T:
        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            result = operation(uow, instance_id)
            uow.commit()
            return result

    def submit(
        self,
        instance: str,
        selection: FleetSelection,
    ) -> FleetManualScanSubmission:
        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        command_id = self._id_factory()
        created_at = self._now()
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.create_pending(
                instance_id,
                command_id,
                selection,
                created_at=created_at,
            ),
        )

    def latest(self, instance: str) -> FleetManualScanCommand | None:
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.latest(instance_id),
        )

    def pending_exists(self, instance: str) -> bool:
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.pending_exists(
                instance_id
            ),
        )

    def claim_next(self, instance: str) -> FleetManualScanCommand | None:
        started_at = self._now()
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.claim_next(
                instance_id,
                started_at=started_at,
            ),
        )

    def finish(
        self,
        instance: str,
        command_id: UUID,
        *,
        status: FleetManualScanStatus,
        result_run_id: UUID | None,
        error_code: str | None,
    ) -> FleetManualScanCommand:
        if status not in {
            FleetManualScanStatus.SUCCEEDED,
            FleetManualScanStatus.PARTIAL,
            FleetManualScanStatus.FAILED,
        }:
            raise ValueError("Manual command можно завершить только terminal status")
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.finish(
                command_id,
                instance_id,
                status=status,
                finished_at=self._now(),
                result_run_id=result_run_id,
                error_code=error_code,
            ),
        )

    def recover_interrupted(self, instance: str) -> int:
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_scan_commands.fail_running(
                instance_id,
                finished_at=self._now(),
                error_code="worker_interrupted",
            ),
        )


class FleetManualScanCoordinator:
    """Забирает и выполняет одну ручную команду на Device текущего scheduler worker."""

    def __init__(
        self,
        command_service: FleetManualScanCommandService,
        state_service: FleetStateService,
    ) -> None:
        self._command_service = command_service
        self._state_service = state_service
        self._recovered_instances: set[str] = set()

    def has_pending(self, instance: str) -> bool:
        self._recover_once(instance)
        return self._command_service.pending_exists(instance)

    def _recover_once(self, instance: str) -> None:
        if instance in self._recovered_instances:
            return
        self._command_service.recover_interrupted(instance)
        self._recovered_instances.add(instance)

    def process_next(self, instance: str) -> FleetManualScanExecution | None:
        self._recover_once(instance)
        command = self._command_service.claim_next(instance)
        if command is None:
            return None

        # После получения команды в PostgreSQL снова существует состояние RUNNING. Пока
        # завершающий переход не подтверждён, следующая безопасная граница обязана уметь
        # восстановить его.
        self._recovered_instances.discard(instance)
        try:
            batch = self._state_service.scan(
                instance,
                command.selection,
                source=FLEET_MANUAL_SCAN_SOURCE,
            )
        except Exception as primary_error:
            try:
                self._command_service.finish(
                    instance,
                    command.id,
                    status=FleetManualScanStatus.FAILED,
                    result_run_id=None,
                    error_code="manual_scan_failed",
                )
            except Exception as cleanup_error:
                primary_error.add_note(
                    "Дополнительно не удалось завершить manual Fleet command: "
                    f"{type(cleanup_error).__name__}"
                )
            else:
                self._recovered_instances.add(instance)
            raise

        status = {
            FleetScanRunStatus.SUCCEEDED: FleetManualScanStatus.SUCCEEDED,
            FleetScanRunStatus.PARTIAL: FleetManualScanStatus.PARTIAL,
            FleetScanRunStatus.FAILED: FleetManualScanStatus.FAILED,
        }[batch.status]
        finished = self._command_service.finish(
            instance,
            command.id,
            status=status,
            result_run_id=batch.run_id,
            error_code=batch.failure_code,
        )
        self._recovered_instances.add(instance)
        return FleetManualScanExecution(command=finished, batch_result=batch)


__all__ = [
    "FLEET_MANUAL_SCAN_SOURCE",
    "FleetManualScanCommand",
    "FleetManualScanCommandService",
    "FleetManualScanCoordinator",
    "FleetManualScanExecution",
    "FleetManualScanStatus",
    "FleetManualScanSubmission",
]
