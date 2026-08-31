"""Узкие repository и Unit of Work contracts без persistence types."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from module.application.database_diagnostics import DatabaseDiagnosticsReader
from module.application.storage_models import (
    CommissionIncome,
    ImportBatch,
    InstanceIdentity,
    MonthlyAggregate,
    MonthlyMetric,
    OpsiItemEvent,
    ResourceSnapshot,
    StorageHealth,
)


class InstanceIdentityRepository(Protocol):
    def register(
        self,
        identity: InstanceIdentity,
        *,
        alias_kind: str,
        alias_digest: str,
        source_provenance: str,
    ) -> bool: ...

    def resolve(
        self, *, alias_kind: str, alias_digest: str
    ) -> InstanceIdentity | None: ...


class StatisticsRepository(Protocol):
    def increment_monthly_counter(
        self, instance_id: UUID, month: date, metric: MonthlyMetric, delta: Decimal
    ) -> MonthlyAggregate: ...

    def append_resource_snapshot(self, snapshot: ResourceSnapshot) -> bool: ...

    def resource_timeline(
        self, instance_id: UUID, *, limit: int
    ) -> tuple[ResourceSnapshot, ...]: ...

    def append_opsi_item_event(self, event: OpsiItemEvent) -> bool: ...

    def record_commission_income(self, income: CommissionIncome) -> bool: ...

    def update_current_resource(
        self,
        instance_id: UUID,
        resource_code: str,
        value: int,
        *,
        expected_version: int,
    ) -> int: ...


class ImportLedgerRepository(Protocol):
    def begin(self, batch: ImportBatch) -> bool: ...

    def complete(
        self,
        batch_id: UUID,
        *,
        record_count: int,
        imported_count: int,
        conflict_count: int = 0,
        quarantine_count: int = 0,
    ) -> None: ...

    def fail(self, batch_id: UUID, *, error_code: str) -> None: ...


class StorageUnitOfWork(Protocol):
    instances: InstanceIdentityRepository
    statistics: StatisticsRepository
    imports: ImportLedgerRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class StorageHealthReader(Protocol):
    def check(self) -> StorageHealth: ...


class DatabaseDiagnosticsPort(DatabaseDiagnosticsReader, Protocol):
    """Read-only catalog boundary for developer diagnostics."""
