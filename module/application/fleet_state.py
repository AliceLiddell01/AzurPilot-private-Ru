"""Fleet State orchestration и transport-neutral persistence contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, TypeVar
from uuid import UUID, uuid4

from module.application.errors import StorageInvalidDataError
from module.application.instance_identity import (
    resolve_existing_runtime_instance,
    resolve_runtime_instance,
)
from module.application.storage_ports import StorageUnitOfWork
from module.formation.model import (
    FleetSelection,
    FormationFleetSnapshot,
    validate_surface_fleet_index,
)

_T = TypeVar("_T")
_MAX_HISTORY_LIMIT = 500


def _aware(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} должен содержать timezone-aware datetime")
    return value


def _source(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("source должен быть непустой строкой длиной до 64 символов")
    return value


class FleetScanRunStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class FleetRefreshPolicy(StrEnum):
    NEVER = "never"
    IF_MISSING = "if_missing"
    IF_STALE = "if_stale"
    ALWAYS = "always"


@dataclass(frozen=True, slots=True)
class FleetScanRun:
    id: UUID
    instance_id: UUID
    selection: FleetSelection
    source: str
    started_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.instance_id, UUID):
            raise TypeError("Scan run identity должен быть UUID")
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        _source(self.source)
        _aware(self.started_at, field="started_at")


@dataclass(frozen=True, slots=True)
class FleetScanAttempt:
    """Последняя логическая попытка сканирования одного выбранного флота."""

    run_id: UUID
    fleet_index: int
    source: str
    started_at: datetime
    status: FleetScanRunStatus
    error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            raise TypeError("Scan attempt run_id должен быть UUID")
        validate_surface_fleet_index(self.fleet_index)
        _source(self.source)
        _aware(self.started_at, field="started_at")
        if not isinstance(self.status, FleetScanRunStatus):
            raise TypeError("status должен быть FleetScanRunStatus")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not self.error_code
            or len(self.error_code) > 64
        ):
            raise ValueError("error_code некорректен")
        if self.status in {
            FleetScanRunStatus.STARTED,
            FleetScanRunStatus.SUCCEEDED,
        }:
            if self.error_code is not None:
                raise ValueError("Started/succeeded attempt не содержит error_code")
        elif self.error_code is None:
            raise ValueError("Неуспешный attempt требует error_code")


@dataclass(frozen=True, slots=True)
class FleetStateObservation:
    id: UUID
    run_id: UUID
    instance_id: UUID
    idempotency_key: str
    observed_at: datetime
    snapshot: FormationFleetSnapshot

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (self.id, self.run_id, self.instance_id)
        ):
            raise TypeError("Observation identity должен быть UUID")
        if (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key
            or len(self.idempotency_key) > 128
        ):
            raise ValueError("idempotency_key должен содержать до 128 символов")
        _aware(self.observed_at, field="observed_at")
        if not isinstance(self.snapshot, FormationFleetSnapshot):
            raise TypeError("snapshot должен быть FormationFleetSnapshot")

    @property
    def fleet_index(self) -> int:
        return self.snapshot.fleet_index


@dataclass(frozen=True, slots=True)
class FleetScanBatchResult:
    run_id: UUID
    selection: FleetSelection
    observations: tuple[FleetStateObservation, ...]
    failed_fleet_index: int | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            raise TypeError("run_id должен быть UUID")
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        if not isinstance(self.observations, tuple) or any(
            not isinstance(item, FleetStateObservation) for item in self.observations
        ):
            raise TypeError("observations должен быть tuple FleetStateObservation")
        indices = tuple(item.fleet_index for item in self.observations)
        if indices != tuple(sorted(indices)) or len(indices) != len(set(indices)):
            raise ValueError("Batch observations должны быть уникальны и упорядочены")
        if any(index not in self.selection.fleet_indices for index in indices):
            raise ValueError("Batch observation не входит в selection")
        if self.failure_code is None:
            if self.failed_fleet_index is not None or len(indices) != len(
                self.selection.fleet_indices
            ):
                raise ValueError("Успешный batch должен содержать все наблюдения")
        else:
            if not self.failure_code or len(self.failure_code) > 64:
                raise ValueError("failure_code некорректен")
            failed_index = validate_surface_fleet_index(self.failed_fleet_index)
            if failed_index not in self.selection.fleet_indices:
                raise ValueError("failed_fleet_index не входит в selection")
            if failed_index in indices:
                raise ValueError("Failed fleet не должен считаться успешным")

    @property
    def status(self) -> FleetScanRunStatus:
        if self.failure_code is None:
            return FleetScanRunStatus.SUCCEEDED
        if self.observations:
            return FleetScanRunStatus.PARTIAL
        return FleetScanRunStatus.FAILED


@dataclass(frozen=True, slots=True)
class FleetStateRequest:
    selection: FleetSelection
    refresh_policy: FleetRefreshPolicy
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        if not isinstance(self.refresh_policy, FleetRefreshPolicy):
            raise TypeError("refresh_policy должен быть FleetRefreshPolicy")
        if self.refresh_policy is FleetRefreshPolicy.IF_STALE:
            if not isinstance(self.max_age, timedelta) or self.max_age < timedelta(0):
                raise ValueError("IF_STALE требует неотрицательный max_age")
        elif self.max_age is not None:
            raise ValueError("max_age допустим только для IF_STALE")


@dataclass(frozen=True, slots=True)
class FleetStateResult:
    request: FleetStateRequest
    observations: tuple[FleetStateObservation, ...]
    missing_fleet_indices: tuple[int, ...]
    refresh_result: FleetScanBatchResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, FleetStateRequest):
            raise TypeError("request должен быть FleetStateRequest")
        if not isinstance(self.observations, tuple) or any(
            not isinstance(item, FleetStateObservation) for item in self.observations
        ):
            raise TypeError("observations должен быть tuple FleetStateObservation")
        observed = tuple(item.fleet_index for item in self.observations)
        if observed != tuple(sorted(observed)) or len(observed) != len(set(observed)):
            raise ValueError("State observations должны быть уникальны и упорядочены")
        expected_missing = tuple(
            index
            for index in self.request.selection.fleet_indices
            if index not in observed
        )
        if self.missing_fleet_indices != expected_missing:
            raise ValueError("missing_fleet_indices не соответствует observations")


class FormationFleetScanController(Protocol):
    def scan_surface_fleet(self, fleet_index: int) -> FormationFleetSnapshot: ...


class FleetStateRepository(Protocol):
    def create_run(self, run: FleetScanRun) -> None: ...

    def append_observation(self, observation: FleetStateObservation) -> bool: ...

    def finish_run(
        self,
        run_id: UUID,
        *,
        status: FleetScanRunStatus,
        finished_at: datetime,
        error_code: str | None,
    ) -> None: ...

    def latest(
        self,
        instance_id: UUID,
        selection: FleetSelection,
    ) -> tuple[FleetStateObservation, ...]: ...

    def history(
        self,
        instance_id: UUID,
        fleet_index: int,
        *,
        limit: int,
    ) -> tuple[FleetStateObservation, ...]: ...

    def complete_in_window(
        self,
        instance_id: UUID,
        selection: FleetSelection,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[int, ...]: ...

    def latest_attempts(
        self,
        instance_id: UUID,
        selection: FleetSelection,
        *,
        source: str,
    ) -> tuple[FleetScanAttempt, ...]: ...


class FleetStateUnitOfWork(StorageUnitOfWork, Protocol):
    fleet_state: FleetStateRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class _FleetStateTransactions:
    def __init__(self, uow_factory: Callable[[], FleetStateUnitOfWork]):
        self._uow_factory = uow_factory

    def _transaction(
        self,
        instance: str,
        operation: Callable[[FleetStateUnitOfWork, UUID], _T],
    ) -> _T:
        with self._uow_factory() as uow:
            instance_id = resolve_runtime_instance(uow, instance)
            result = operation(uow, instance_id)
            uow.commit()
            return result


class FleetScanService(_FleetStateTransactions):
    """Последовательно сканирует выбранные флоты и сразу фиксирует успехи."""

    def __init__(
        self,
        uow_factory: Callable[[], FleetStateUnitOfWork],
        controller: FormationFleetScanController,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ):
        super().__init__(uow_factory)
        self._controller = controller
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    def _now(self) -> datetime:
        return _aware(self._clock(), field="clock")

    def scan(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> FleetScanBatchResult:
        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        source = _source(source)
        run_id = self._id_factory()
        started_at = self._now()

        def create(uow: FleetStateUnitOfWork, instance_id: UUID) -> UUID:
            uow.fleet_state.create_run(
                FleetScanRun(
                    id=run_id,
                    instance_id=instance_id,
                    selection=selection,
                    source=source,
                    started_at=started_at,
                )
            )
            return instance_id

        instance_id = self._transaction(instance, create)
        observations: list[FleetStateObservation] = []
        failed_fleet_index: int | None = None
        failure_code: str | None = None

        for fleet_index in selection.fleet_indices:
            try:
                snapshot = self._controller.scan_surface_fleet(fleet_index)
                if not isinstance(snapshot, FormationFleetSnapshot):
                    raise TypeError("Formation controller вернул неверный snapshot")
                if snapshot.fleet_index != fleet_index:
                    raise ValueError("Formation controller вернул другой fleet_index")
            except Exception:  # noqa: BLE001 - после неизвестного UI state scan прекращается.
                failed_fleet_index = fleet_index
                failure_code = "physical_scan_failed"
                break

            observation = FleetStateObservation(
                id=self._id_factory(),
                run_id=run_id,
                instance_id=instance_id,
                idempotency_key=f"fleet-scan-v1:{run_id}:{fleet_index}",
                observed_at=self._now(),
                snapshot=snapshot,
            )
            try:
                self._transaction(
                    instance,
                    lambda uow, _instance_id, item=observation: (
                        uow.fleet_state.append_observation(item)
                    ),
                )
            except Exception as error:
                self._finish_after_persistence_failure(
                    instance,
                    run_id,
                    error,
                    had_successes=bool(observations),
                )
                raise
            observations.append(observation)

        status = (
            FleetScanRunStatus.SUCCEEDED
            if failure_code is None
            else FleetScanRunStatus.PARTIAL
            if observations
            else FleetScanRunStatus.FAILED
        )
        self._finish(
            instance,
            run_id,
            status=status,
            error_code=failure_code,
        )
        return FleetScanBatchResult(
            run_id=run_id,
            selection=selection,
            observations=tuple(observations),
            failed_fleet_index=failed_fleet_index,
            failure_code=failure_code,
        )

    def _finish(
        self,
        instance: str,
        run_id: UUID,
        *,
        status: FleetScanRunStatus,
        error_code: str | None,
    ) -> None:
        finished_at = self._now()
        self._transaction(
            instance,
            lambda uow, _instance_id: uow.fleet_state.finish_run(
                run_id,
                status=status,
                finished_at=finished_at,
                error_code=error_code,
            ),
        )

    def _finish_after_persistence_failure(
        self,
        instance: str,
        run_id: UUID,
        primary_error: Exception,
        *,
        had_successes: bool,
    ) -> None:
        try:
            self._finish(
                instance,
                run_id,
                status=(
                    FleetScanRunStatus.PARTIAL
                    if had_successes
                    else FleetScanRunStatus.FAILED
                ),
                error_code="persistence_failed",
            )
        except Exception as cleanup_error:  # noqa: BLE001 - primary DB error остаётся главной.
            primary_error.add_note(
                "Дополнительно не удалось завершить Fleet scan run: "
                f"{type(cleanup_error).__name__}"
            )


class FleetStateService(_FleetStateTransactions):
    """Единый API сохранённого состояния и физического refresh флотов."""

    def __init__(
        self,
        uow_factory: Callable[[], FleetStateUnitOfWork],
        scan_service: FleetScanService | Callable[[], FleetScanService],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        super().__init__(uow_factory)
        if isinstance(scan_service, FleetScanService):
            self._scan_service_factory = lambda: scan_service
        elif callable(scan_service):
            self._scan_service_factory = scan_service
        else:
            raise TypeError("scan_service должен быть FleetScanService или factory")
        self._clock = clock or (lambda: datetime.now(UTC))

    def scan(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> FleetScanBatchResult:
        scan_service = self._scan_service_factory()
        if not isinstance(scan_service, FleetScanService):
            raise TypeError("scan_service factory вернула неверный объект")
        return scan_service.scan(instance, selection, source=source)

    def state(
        self,
        instance: str,
        request: FleetStateRequest,
        *,
        source: str,
    ) -> FleetStateResult:
        if not isinstance(request, FleetStateRequest):
            raise TypeError("request должен быть FleetStateRequest")
        _source(source)
        current = self._latest(instance, request.selection)
        by_index = {item.fleet_index: item for item in current}
        refresh_indices: tuple[int, ...]

        if request.refresh_policy is FleetRefreshPolicy.NEVER:
            refresh_indices = ()
        elif request.refresh_policy is FleetRefreshPolicy.IF_MISSING:
            refresh_indices = tuple(
                index
                for index in request.selection.fleet_indices
                if index not in by_index
            )
        elif request.refresh_policy is FleetRefreshPolicy.ALWAYS:
            refresh_indices = request.selection.fleet_indices
        else:
            now = _aware(self._clock(), field="clock")
            max_age = request.max_age
            if max_age is None:
                raise StorageInvalidDataError("IF_STALE не содержит max_age.")
            refresh_indices = tuple(
                index
                for index in request.selection.fleet_indices
                if index not in by_index
                or now.astimezone(UTC)
                - by_index[index].observed_at.astimezone(UTC)
                > max_age
            )

        refresh_result = None
        if refresh_indices:
            refresh_result = self.scan(
                instance,
                FleetSelection(refresh_indices),
                source=source,
            )
            current = self._latest(instance, request.selection)

        observed_indices = {item.fleet_index for item in current}
        missing = tuple(
            index
            for index in request.selection.fleet_indices
            if index not in observed_indices
        )
        return FleetStateResult(
            request=request,
            observations=current,
            missing_fleet_indices=missing,
            refresh_result=refresh_result,
        )

    def history(
        self,
        instance: str,
        fleet_index: int,
        *,
        limit: int,
    ) -> tuple[FleetStateObservation, ...]:
        fleet_index = validate_surface_fleet_index(fleet_index)
        if type(limit) is not int or not 1 <= limit <= _MAX_HISTORY_LIMIT:
            raise ValueError(f"limit должен быть int в диапазоне 1..{_MAX_HISTORY_LIMIT}")
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_state.history(
                instance_id,
                fleet_index,
                limit=limit,
            ),
        )

    def complete_in_window(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[int, ...]:
        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        start = _aware(start, field="start")
        end = _aware(end, field="end")
        if end <= start:
            raise ValueError("end должен быть позже start")
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_state.complete_in_window(
                instance_id,
                selection,
                start=start,
                end=end,
            ),
        )

    def latest_attempts(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> tuple[FleetScanAttempt, ...]:
        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        source = _source(source)
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_state.latest_attempts(
                instance_id,
                selection,
                source=source,
            ),
        )

    def _latest(
        self,
        instance: str,
        selection: FleetSelection,
    ) -> tuple[FleetStateObservation, ...]:
        return self._transaction(
            instance,
            lambda uow, instance_id: uow.fleet_state.latest(
                instance_id,
                selection,
            ),
        )


class FleetStateReadService:
    """Прочитать сохранённый Fleet State без регистрации профиля или commit."""

    def __init__(self, uow_factory: Callable[[], FleetStateUnitOfWork]):
        self._uow_factory = uow_factory

    def state_read_only(
        self,
        instance: str,
        selection: FleetSelection,
    ) -> FleetStateResult:
        """Вернуть только уже сохранённые наблюдения выбранного профиля."""

        if not isinstance(selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")
        request = FleetStateRequest(selection, FleetRefreshPolicy.NEVER)
        with self._uow_factory() as uow:
            instance_id = resolve_existing_runtime_instance(uow, instance)
            if instance_id is None:
                return FleetStateResult(
                    request=request,
                    observations=(),
                    missing_fleet_indices=selection.fleet_indices,
                )
            raw_observations = uow.fleet_state.latest(instance_id, selection)
            if not isinstance(raw_observations, tuple):
                raise StorageInvalidDataError(
                    "Fleet State repository вернул не tuple наблюдений."
                )
            observations = tuple(raw_observations)
            if any(
                not isinstance(item, FleetStateObservation)
                or item.instance_id != instance_id
                for item in observations
            ):
                raise StorageInvalidDataError(
                    "Fleet State repository вернул наблюдение другого профиля."
                )
        indices = tuple(item.fleet_index for item in observations)
        if indices != tuple(sorted(indices)) or len(indices) != len(set(indices)):
            raise StorageInvalidDataError(
                "Fleet State repository вернул неуникальные наблюдения."
            )
        if any(index not in selection.fleet_indices for index in indices):
            raise StorageInvalidDataError(
                "Fleet State repository вернул наблюдение вне selection."
            )
        return FleetStateResult(
            request=request,
            observations=observations,
            missing_fleet_indices=tuple(
                index for index in selection.fleet_indices if index not in indices
            ),
        )

    def state(self, instance: str, selection: FleetSelection) -> FleetStateResult:
        """Короткий alias для transport-neutral read-only запроса."""

        return self.state_read_only(instance, selection)
