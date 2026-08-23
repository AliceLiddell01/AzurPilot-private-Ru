"""PostgreSQL Core adapters; транзакцией всегда владеет Unit of Work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.storage_models import (
    CommissionIncome,
    ImportBatch,
    InstanceIdentity,
    MonthlyAggregate,
    MonthlyMetric,
    OpsiItemEvent,
    ResourceSnapshot,
)
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    RESOURCE_COLUMNS,
    app_instance,
    commission_income_event,
    commission_income_item,
    import_batch,
    legacy_instance_alias,
    monthly_aggregate,
    opsi_item_event,
    resource_current_state,
    resource_snapshot,
)


def _bounded(value: str, *, label: str, maximum: int) -> str:
    if not value or len(value) > maximum:
        raise StorageInvalidDataError(f"Поле {label} некорректно.")
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_values(values: dict[str, object]) -> dict[str, object]:
    """Исключает surrogate identity из idempotency payload."""

    return {key: value for key, value in values.items() if key != "id"}


def _sha256_digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise StorageInvalidDataError(f"Поле {label} должно быть lowercase SHA-256.")
    return value


class PostgresInstanceIdentityRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def register(
        self,
        identity: InstanceIdentity,
        *,
        alias_kind: str,
        alias_digest: str,
        source_provenance: str,
    ) -> bool:
        _bounded(identity.name, label="instance name", maximum=128)
        _bounded(alias_kind, label="alias_kind", maximum=32)
        _bounded(source_provenance, label="source_provenance", maximum=128)
        _sha256_digest(alias_digest, label="alias_digest")
        try:
            self._connection.execute(
                insert(app_instance)
                .values(
                    id=identity.id,
                    name=identity.name,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["name"])
            )
            stored_identity = self._connection.execute(
                select(app_instance.c.id).where(app_instance.c.name == identity.name)
            ).scalar_one()
            if stored_identity != identity.id:
                raise StorageConflictError(
                    "Instance name уже связан с другой identity."
                )
            inserted = self._connection.execute(
                insert(legacy_instance_alias)
                .values(
                    alias_kind=alias_kind,
                    alias_digest=alias_digest,
                    instance_id=identity.id,
                    source_provenance=source_provenance,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["alias_kind", "alias_digest"])
                .returning(legacy_instance_alias.c.id)
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            mapped = self._connection.execute(
                select(legacy_instance_alias.c.instance_id).where(
                    legacy_instance_alias.c.alias_kind == alias_kind,
                    legacy_instance_alias.c.alias_digest == alias_digest,
                )
            ).scalar_one()
        except StorageConflictError:
            raise
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        if mapped == identity.id:
            return False
        raise StorageConflictError("Legacy alias уже связан с другой identity.")

    def resolve(self, *, alias_kind: str, alias_digest: str) -> InstanceIdentity | None:
        _bounded(alias_kind, label="alias_kind", maximum=32)
        _sha256_digest(alias_digest, label="alias_digest")
        try:
            row = self._connection.execute(
                select(app_instance.c.id, app_instance.c.name)
                .select_from(
                    legacy_instance_alias.join(
                        app_instance,
                        legacy_instance_alias.c.instance_id == app_instance.c.id,
                    )
                )
                .where(
                    legacy_instance_alias.c.alias_kind == alias_kind,
                    legacy_instance_alias.c.alias_digest == alias_digest,
                )
            ).one_or_none()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        return InstanceIdentity(*row) if row is not None else None


class PostgresStatisticsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def increment_monthly_counter(
        self, instance_id: UUID, month: date, metric: MonthlyMetric, delta: Decimal
    ) -> MonthlyAggregate:
        if not isinstance(metric, MonthlyMetric):
            raise StorageInvalidDataError("Monthly metric должен быть типизирован.")
        if month.day != 1:
            raise StorageInvalidDataError(
                "Month должен указывать на первый день месяца."
            )
        if not isinstance(delta, Decimal) or delta <= 0:
            raise StorageInvalidDataError("Counter delta должен быть положительным.")
        statement = insert(monthly_aggregate).values(
            instance_id=instance_id,
            month=month,
            metric=metric.value,
            value=delta,
            source_kind="runtime",
            source_digest=None,
            version=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["instance_id", "month", "metric"],
            set_={
                "value": monthly_aggregate.c.value + statement.excluded.value,
                "version": monthly_aggregate.c.version + 1,
            },
        ).returning(
            monthly_aggregate.c.instance_id,
            monthly_aggregate.c.month,
            monthly_aggregate.c.metric,
            monthly_aggregate.c.value,
            monthly_aggregate.c.version,
        )
        try:
            row = self._connection.execute(statement).one()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        return MonthlyAggregate(
            row.instance_id,
            row.month,
            MonthlyMetric(row.metric),
            row.value,
            row.version,
        )

    def append_resource_snapshot(self, snapshot: ResourceSnapshot) -> bool:
        _bounded(snapshot.idempotency_key, label="idempotency_key", maximum=128)
        _bounded(snapshot.source, label="source", maximum=64)
        if any(
            value is not None and value < 0
            for value in (getattr(snapshot, name) for name in RESOURCE_COLUMNS)
        ):
            raise StorageInvalidDataError(
                "Resource values не могут быть отрицательными."
            )
        values = asdict(snapshot)
        digest = _digest(_semantic_values(values))
        values["payload_digest"] = digest
        try:
            inserted = self._connection.execute(
                insert(resource_snapshot)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(resource_snapshot.c.id)
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            existing = self._connection.execute(
                select(resource_snapshot.c.payload_digest).where(
                    resource_snapshot.c.idempotency_key == snapshot.idempotency_key
                )
            ).scalar_one()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        if existing == digest:
            return False
        raise StorageConflictError(
            "Idempotency key снимка уже связан с другими данными."
        )

    def resource_timeline(
        self, instance_id: UUID, *, limit: int
    ) -> tuple[ResourceSnapshot, ...]:
        if limit < 1 or limit > 1000:
            raise StorageInvalidDataError("Timeline limit должен быть от 1 до 1000.")
        columns = (
            resource_snapshot.c.id,
            resource_snapshot.c.instance_id,
            resource_snapshot.c.idempotency_key,
            resource_snapshot.c.observed_at,
            resource_snapshot.c.source,
            *(resource_snapshot.c[name] for name in RESOURCE_COLUMNS),
            resource_snapshot.c.legacy_timestamp_text,
            resource_snapshot.c.legacy_timezone,
        )
        try:
            rows = self._connection.execute(
                select(*columns)
                .where(resource_snapshot.c.instance_id == instance_id)
                .order_by(
                    resource_snapshot.c.observed_at.desc().nulls_last(),
                    resource_snapshot.c.id.desc(),
                )
                .limit(limit)
            ).all()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        return tuple(reversed(tuple(ResourceSnapshot(*row) for row in rows)))

    def append_opsi_item_event(self, event: OpsiItemEvent) -> bool:
        _bounded(event.idempotency_key, label="idempotency_key", maximum=128)
        _bounded(event.imgid, label="imgid", maximum=128)
        _bounded(event.genre, label="genre", maximum=64)
        _bounded(event.item_code, label="item_code", maximum=128)
        if event.amount < 0:
            raise StorageInvalidDataError("Opsi amount не может быть отрицательным.")
        if event.hazard_level is not None and not 1 <= event.hazard_level <= 6:
            raise StorageInvalidDataError("Opsi hazard level некорректен.")
        if event.combat_count is not None and event.combat_count < 0:
            raise StorageInvalidDataError(
                "Opsi combat count не может быть отрицательным."
            )
        values = asdict(event)
        digest = _digest(_semantic_values(values))
        values["payload_digest"] = digest
        return self._append_with_digest(
            opsi_item_event, values, event.idempotency_key, digest
        )

    def record_commission_income(self, income: CommissionIncome) -> bool:
        _bounded(income.idempotency_key, label="idempotency_key", maximum=128)
        _bounded(income.source, label="source", maximum=64)
        if income.commission_count < 1 or not income.items:
            raise StorageInvalidDataError("Commission income должен содержать items.")
        if len({item.item_code for item in income.items}) != len(income.items):
            raise StorageInvalidDataError("Commission items должны быть уникальны.")
        for item in income.items:
            _bounded(item.item_code, label="item_code", maximum=128)
            if item.amount < 0:
                raise StorageInvalidDataError(
                    "Commission amount не может быть отрицательным."
                )
        digest = _digest(_semantic_values(asdict(income)))
        header = {key: value for key, value in asdict(income).items() if key != "items"}
        header["payload_digest"] = digest
        try:
            inserted = self._connection.execute(
                insert(commission_income_event)
                .values(**header)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(commission_income_event.c.id)
            ).scalar_one_or_none()
            if inserted is None:
                existing = self._connection.execute(
                    select(commission_income_event.c.payload_digest).where(
                        commission_income_event.c.idempotency_key
                        == income.idempotency_key
                    )
                ).scalar_one()
                if existing == digest:
                    return False
                raise StorageConflictError(
                    "Idempotency key комиссии уже связан с другими данными."
                )
            self._connection.execute(
                insert(commission_income_item),
                [
                    {
                        "event_id": income.id,
                        "item_code": item.item_code,
                        "amount": item.amount,
                    }
                    for item in income.items
                ],
            )
            return True
        except StorageConflictError:
            raise
        except DBAPIError as exc:
            raise translate_database_error(exc) from None

    def update_current_resource(
        self,
        instance_id: UUID,
        resource_code: str,
        value: int,
        *,
        expected_version: int,
    ) -> int:
        _bounded(resource_code, label="resource_code", maximum=32)
        if value < 0 or expected_version < 0:
            raise StorageInvalidDataError("Resource value/version некорректны.")
        now = datetime.now(UTC)
        try:
            if expected_version == 0:
                created = self._connection.execute(
                    insert(resource_current_state)
                    .values(
                        instance_id=instance_id,
                        resource_code=resource_code,
                        value=value,
                        version=1,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["instance_id", "resource_code"]
                    )
                    .returning(resource_current_state.c.version)
                ).scalar_one_or_none()
                if created is not None:
                    return created
                raise StorageConflictError("Current state уже существует.")
            current = self._connection.execute(
                select(resource_current_state.c.version)
                .where(
                    resource_current_state.c.instance_id == instance_id,
                    resource_current_state.c.resource_code == resource_code,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current != expected_version:
                raise StorageConflictError("Optimistic version устарела.")
            next_version = expected_version + 1
            self._connection.execute(
                update(resource_current_state)
                .where(
                    resource_current_state.c.instance_id == instance_id,
                    resource_current_state.c.resource_code == resource_code,
                    resource_current_state.c.version == expected_version,
                )
                .values(value=value, version=next_version, updated_at=now)
            )
            return next_version
        except StorageConflictError:
            raise
        except DBAPIError as exc:
            raise translate_database_error(exc) from None

    def _append_with_digest(
        self, table, values: dict[str, object], idempotency_key: str, digest: str
    ) -> bool:
        try:
            inserted = self._connection.execute(
                insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(table.c.id)
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            existing = self._connection.execute(
                select(table.c.payload_digest).where(
                    table.c.idempotency_key == idempotency_key
                )
            ).scalar_one()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        if existing == digest:
            return False
        raise StorageConflictError("Idempotency key уже связан с другими данными.")


class PostgresImportLedgerRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def begin(self, batch: ImportBatch) -> bool:
        if batch.status.value != "started":
            raise StorageInvalidDataError(
                "Import batch должен начинаться в status=started."
            )
        _bounded(batch.idempotency_key, label="idempotency_key", maximum=128)
        _bounded(batch.source_kind, label="source_kind", maximum=64)
        _sha256_digest(batch.source_digest, label="source_digest")
        values = asdict(batch)
        values["status"] = batch.status.value
        try:
            inserted = self._connection.execute(
                insert(import_batch)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(import_batch.c.id)
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            existing = self._connection.execute(
                select(import_batch.c.source_digest).where(
                    import_batch.c.idempotency_key == batch.idempotency_key
                )
            ).scalar_one()
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        if existing == batch.source_digest:
            return False
        raise StorageConflictError("Import key уже связан с другим source digest.")

    def complete(
        self,
        batch_id: UUID,
        *,
        record_count: int,
        imported_count: int,
        conflict_count: int = 0,
        quarantine_count: int = 0,
    ) -> None:
        if min(record_count, imported_count, conflict_count, quarantine_count) < 0:
            raise StorageInvalidDataError(
                "Import counters не могут быть отрицательными."
            )
        if imported_count + conflict_count + quarantine_count > record_count:
            raise StorageInvalidDataError("Import counters превышают record_count.")
        self._transition(
            batch_id,
            status="completed" if conflict_count == 0 else "conflict",
            finished_at=datetime.now(UTC),
            record_count=record_count,
            imported_count=imported_count,
            conflict_count=conflict_count,
            quarantine_count=quarantine_count,
        )

    def fail(self, batch_id: UUID, *, error_code: str) -> None:
        _bounded(error_code, label="error_code", maximum=64)
        self._transition(
            batch_id,
            status="failed",
            finished_at=datetime.now(UTC),
            error_code=error_code,
        )

    def _transition(self, batch_id: UUID, **values: object) -> None:
        try:
            result = self._connection.execute(
                update(import_batch)
                .where(
                    import_batch.c.id == batch_id, import_batch.c.status == "started"
                )
                .values(**values)
            )
        except DBAPIError as exc:
            raise translate_database_error(exc) from None
        if result.rowcount != 1:
            raise StorageConflictError("Import batch transition недопустим.")
