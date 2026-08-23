"""Порты offline pipeline без SQLite, SQLAlchemy и Psycopg types."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from module.application.migration_models import (
    LegacyIdentity,
    LegacyMigrationPlan,
    MigrationBatchState,
    MigrationDelta,
    MigrationRecord,
    TargetProjection,
)


class LegacyMigrationSource(Protocol):
    def capture(self) -> LegacyMigrationPlan: ...


class MigrationTarget(Protocol):
    def preflight(self) -> None: ...

    def begin(self, plan: LegacyMigrationPlan) -> MigrationBatchState: ...

    def import_identities(
        self, batch_id: UUID, identities: tuple[LegacyIdentity, ...]
    ) -> MigrationDelta: ...

    def import_records(
        self, batch_id: UUID, records: tuple[MigrationRecord, ...]
    ) -> MigrationDelta: ...

    def complete(
        self, batch_id: UUID, plan: LegacyMigrationPlan, delta: MigrationDelta
    ) -> None: ...

    def fail(self, batch_id: UUID, reason_code: str, *, conflict: bool) -> None: ...

    def project(
        self, batch_id: UUID, plan: LegacyMigrationPlan
    ) -> TargetProjection: ...
