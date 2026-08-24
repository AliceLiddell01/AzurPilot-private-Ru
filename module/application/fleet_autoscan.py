"""Policy и orchestration автоматического сканирования Formation-флотов."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from module.application.fleet_state import (
    FleetScanAttempt,
    FleetScanBatchResult,
)
from module.formation.model import FleetSelection

FLEET_AUTOSCAN_SOURCE_DAILY = "autoscan:daily"
FLEET_AUTOSCAN_SOURCE_EVERY_START = "autoscan:every_start"


def _aware(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} должен содержать timezone-aware datetime")
    return value


class FleetAutoScanMode(StrEnum):
    DISABLED = "disabled"
    EVERY_START = "every_start"
    DAILY = "daily"


@dataclass(frozen=True, slots=True)
class FleetAutoScanConfig:
    mode: FleetAutoScanMode
    selection: FleetSelection

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FleetAutoScanMode):
            raise TypeError("mode должен быть FleetAutoScanMode")
        if not isinstance(self.selection, FleetSelection):
            raise TypeError("selection должен быть FleetSelection")

    @classmethod
    def from_raw(
        cls,
        mode: object,
        fleet_indices: object,
    ) -> FleetAutoScanConfig:
        try:
            normalized_mode = FleetAutoScanMode(mode)
        except (TypeError, ValueError):
            raise ValueError("FleetAutoScan.Mode содержит неподдерживаемое значение") from None
        if not isinstance(fleet_indices, (list, tuple)):
            raise TypeError("FleetAutoScan.Fleets должен быть списком индексов")
        return cls(normalized_mode, FleetSelection(tuple(fleet_indices)))


@dataclass(frozen=True, slots=True)
class FleetAutoScanRetryPolicy:
    cooldown: timedelta = timedelta(minutes=30)
    maximum_cooldown: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if not isinstance(self.cooldown, timedelta) or self.cooldown <= timedelta(0):
            raise ValueError("Fleet autoscan cooldown должен быть положительным")
        if (
            not isinstance(self.maximum_cooldown, timedelta)
            or self.maximum_cooldown <= timedelta(0)
            or self.maximum_cooldown > timedelta(hours=24)
            or self.cooldown > self.maximum_cooldown
        ):
            raise ValueError("Fleet autoscan cooldown превышает допустимую границу")

    def retry_allowed(self, now: datetime, attempted_at: datetime | None) -> bool:
        now = _aware(now, field_name="now")
        if attempted_at is None:
            return True
        attempted_at = _aware(attempted_at, field_name="attempted_at")
        return now.astimezone(UTC) - attempted_at.astimezone(UTC) >= self.cooldown


@dataclass(frozen=True, slots=True)
class FleetAutoScanDayWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _aware(self.start, field_name="start")
        _aware(self.end, field_name="end")
        if self.end <= self.start:
            raise ValueError("Fleet autoscan day window должен быть положительным")


@dataclass(frozen=True, slots=True)
class FleetAutoScanExecution:
    mode: FleetAutoScanMode
    source: str
    due_selection: FleetSelection
    batch_result: FleetScanBatchResult
    complete_fleet_indices: tuple[int, ...]
    incomplete_fleet_indices: tuple[int, ...]


class FleetAutoScanStateService(Protocol):
    def complete_in_window(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[int, ...]: ...

    def latest_attempts(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> tuple[FleetScanAttempt, ...]: ...

    def scan(
        self,
        instance: str,
        selection: FleetSelection,
        *,
        source: str,
    ) -> FleetScanBatchResult: ...


@dataclass(frozen=True, slots=True)
class FleetAutoScanPolicy:
    runtime_timezone: ZoneInfo
    retry: FleetAutoScanRetryPolicy = field(default_factory=FleetAutoScanRetryPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_timezone, ZoneInfo):
            raise TypeError("runtime_timezone должен быть ZoneInfo")
        if not isinstance(self.retry, FleetAutoScanRetryPolicy):
            raise TypeError("retry должен быть FleetAutoScanRetryPolicy")

    def calendar_day_window(self, now: datetime) -> FleetAutoScanDayWindow:
        local_now = _aware(now, field_name="now").astimezone(self.runtime_timezone)
        local_start = datetime.combine(
            local_now.date(),
            time.min,
            tzinfo=self.runtime_timezone,
        )
        local_end = datetime.combine(
            local_now.date() + timedelta(days=1),
            time.min,
            tzinfo=self.runtime_timezone,
        )
        return FleetAutoScanDayWindow(
            start=local_start.astimezone(UTC),
            end=local_end.astimezone(UTC),
        )

    @staticmethod
    def pending_indices(
        selection: FleetSelection,
        satisfied_indices: Sequence[int],
    ) -> tuple[int, ...]:
        satisfied = set(satisfied_indices)
        return tuple(
            index for index in selection.fleet_indices if index not in satisfied
        )

    def due_selection(
        self,
        pending_indices: Sequence[int],
        *,
        now: datetime,
        attempted_at: Mapping[int, datetime],
    ) -> FleetSelection | None:
        now = _aware(now, field_name="now")
        due = tuple(
            index
            for index in pending_indices
            if self.retry.retry_allowed(now, attempted_at.get(index))
        )
        return FleetSelection(due) if due else None


class FleetAutoScanCoordinator:
    """Исполняет autoscan только для due-флотов на scheduler boundary."""

    def __init__(
        self,
        state_service: FleetAutoScanStateService,
        policy: FleetAutoScanPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state_service = state_service
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._startup_complete: set[int] = set()
        self._startup_attempted_at: dict[int, datetime] = {}
        self._daily_attempted_at: dict[int, datetime] = {}

    def _now(self) -> datetime:
        return _aware(self._clock(), field_name="clock")

    @staticmethod
    def _attempt_times(
        attempts: Sequence[FleetScanAttempt],
    ) -> dict[int, datetime]:
        return {attempt.fleet_index: attempt.started_at for attempt in attempts}

    @staticmethod
    def _merge_attempt_times(
        persisted: Mapping[int, datetime],
        local: Mapping[int, datetime],
    ) -> dict[int, datetime]:
        merged = dict(persisted)
        for fleet_index, attempted_at in local.items():
            previous = merged.get(fleet_index)
            if previous is None or attempted_at.astimezone(UTC) > previous.astimezone(UTC):
                merged[fleet_index] = attempted_at
        return merged

    def _daily_due(
        self,
        instance: str,
        config: FleetAutoScanConfig,
        now: datetime,
    ) -> FleetSelection | None:
        window = self._policy.calendar_day_window(now)
        complete = self._state_service.complete_in_window(
            instance,
            config.selection,
            start=window.start,
            end=window.end,
        )
        pending = self._policy.pending_indices(config.selection, complete)
        if not pending:
            return None
        pending_selection = FleetSelection(tuple(pending))
        persisted = self._attempt_times(
            self._state_service.latest_attempts(
                instance,
                pending_selection,
                source=FLEET_AUTOSCAN_SOURCE_DAILY,
            )
        )
        persisted = {
            fleet_index: attempted_at
            for fleet_index, attempted_at in persisted.items()
            if attempted_at.astimezone(UTC) >= window.start
        }
        local = {
            fleet_index: attempted_at
            for fleet_index, attempted_at in self._daily_attempted_at.items()
            if attempted_at.astimezone(UTC) >= window.start
        }
        attempts = self._merge_attempt_times(persisted, local)
        return self._policy.due_selection(
            pending,
            now=now,
            attempted_at=attempts,
        )

    def _startup_due(
        self,
        config: FleetAutoScanConfig,
        now: datetime,
    ) -> FleetSelection | None:
        pending = self._policy.pending_indices(
            config.selection,
            tuple(self._startup_complete),
        )
        return self._policy.due_selection(
            pending,
            now=now,
            attempted_at=self._startup_attempted_at,
        )

    def run_if_due(
        self,
        instance: str,
        config: FleetAutoScanConfig,
    ) -> FleetAutoScanExecution | None:
        if not isinstance(config, FleetAutoScanConfig):
            raise TypeError("config должен быть FleetAutoScanConfig")
        if config.mode is FleetAutoScanMode.DISABLED:
            return None

        now = self._now()
        if config.mode is FleetAutoScanMode.DAILY:
            due = self._daily_due(instance, config, now)
            source = FLEET_AUTOSCAN_SOURCE_DAILY
            local_attempts = self._daily_attempted_at
        else:
            due = self._startup_due(config, now)
            source = FLEET_AUTOSCAN_SOURCE_EVERY_START
            local_attempts = self._startup_attempted_at
        if due is None:
            return None

        for fleet_index in due.fleet_indices:
            local_attempts[fleet_index] = now

        batch = self._state_service.scan(instance, due, source=source)
        complete = tuple(
            observation.fleet_index
            for observation in batch.observations
            if observation.snapshot.complete
        )
        incomplete = tuple(
            observation.fleet_index
            for observation in batch.observations
            if not observation.snapshot.complete
        )
        if config.mode is FleetAutoScanMode.EVERY_START:
            self._startup_complete.update(complete)

        return FleetAutoScanExecution(
            mode=config.mode,
            source=source,
            due_selection=due,
            batch_result=batch,
            complete_fleet_indices=complete,
            incomplete_fleet_indices=incomplete,
        )


__all__ = [
    "FLEET_AUTOSCAN_SOURCE_DAILY",
    "FLEET_AUTOSCAN_SOURCE_EVERY_START",
    "FleetAutoScanConfig",
    "FleetAutoScanCoordinator",
    "FleetAutoScanDayWindow",
    "FleetAutoScanExecution",
    "FleetAutoScanMode",
    "FleetAutoScanPolicy",
    "FleetAutoScanRetryPolicy",
]
