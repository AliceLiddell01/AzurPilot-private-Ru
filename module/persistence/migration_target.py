"""PostgreSQL target для application-owned offline migration pipeline."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageConflictError
from module.application.migration_models import (
    LegacyIdentity,
    LegacyMigrationPlan,
    MigrationBatchState,
    MigrationDelta,
    MigrationRecord,
    RecordDisposition,
    TargetProjection,
    canonical_digest,
)
from module.persistence.database import (
    LazyEngine,
    StorageHealthChecker,
    translate_database_error,
)
from module.persistence.schema import (
    EXPECTED_ALEMBIC_HEAD,
    ap_notification_state,
    app_instance,
    cl1_ap_purchase_event,
    cl1_ap_snapshot,
    cl1_currency_snapshot,
    commission_income_event,
    commission_income_item,
    import_batch,
    import_record,
    legacy_instance_alias,
    meow_hazard_aggregate,
    meow_timing_sample,
    monthly_aggregate,
    opsi_item_event,
    resource_snapshot,
    siren_research_device_event,
    siren_research_device_stat,
)

_TARGET_NAMESPACE = UUID("51686062-fac5-4bb5-89ee-70f34854d195")
_EVENT_TABLES = {
    "resource_snapshot": resource_snapshot,
    "opsi_item": opsi_item_event,
    "ap_snapshot": cl1_ap_snapshot,
    "ap_purchase": cl1_ap_purchase_event,
    "currency_snapshot": cl1_currency_snapshot,
    "commission": commission_income_event,
    "meow_timing": meow_timing_sample,
    "siren_event": siren_research_device_event,
}


class PostgresMigrationTarget:
    """Каждый публичный write выполняется в явной короткой transaction."""

    def __init__(self, engine: LazyEngine):
        self._engine = engine
        self._health = StorageHealthChecker(engine)

    def preflight(self) -> None:
        self._health.require_ready()

    def dispose(self) -> None:
        self._engine.dispose()

    def begin(self, plan: LegacyMigrationPlan) -> MigrationBatchState:
        batch_id = uuid5(_TARGET_NAMESPACE, plan.manifest_digest)
        key = f"migration-v1:{plan.manifest_digest}"
        try:
            with self._engine.get().begin() as connection:
                row = (
                    connection.execute(
                        select(import_batch).where(
                            import_batch.c.idempotency_key == key
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is not None:
                    if row["source_digest"] != plan.manifest_digest:
                        raise StorageConflictError("Конфликт manifest idempotency key.")
                    if row["id"] != batch_id:
                        raise StorageConflictError(
                            "Конфликт deterministic batch identity."
                        )
                    if row["status"] == "completed":
                        return MigrationBatchState(batch_id, True)
                    connection.execute(
                        update(import_batch)
                        .where(import_batch.c.id == batch_id)
                        .values(status="started", finished_at=None, error_code=None)
                    )
                    return MigrationBatchState(batch_id, False)
                connection.execute(
                    insert(import_batch).values(
                        id=batch_id,
                        idempotency_key=key,
                        source_kind="legacy_manifest_v1",
                        source_digest=plan.manifest_digest,
                        status="started",
                        started_at=datetime.now(UTC),
                    )
                )
            return MigrationBatchState(batch_id, False)
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def import_identities(
        self, batch_id: UUID, identities: tuple[LegacyIdentity, ...]
    ) -> MigrationDelta:
        del batch_id
        try:
            with self._engine.get().begin() as connection:
                for identity in identities:
                    alias = connection.execute(
                        select(legacy_instance_alias.c.instance_id).where(
                            legacy_instance_alias.c.alias_kind == identity.alias_kind,
                            legacy_instance_alias.c.alias_digest
                            == identity.alias_digest,
                        )
                    ).scalar_one_or_none()
                    if alias is not None:
                        if alias != identity.internal_id:
                            raise StorageConflictError(
                                "Конфликт legacy identity mapping."
                            )
                        continue
                    if (
                        connection.execute(
                            select(app_instance.c.id).where(
                                app_instance.c.id == identity.internal_id
                            )
                        ).scalar_one_or_none()
                        is None
                    ):
                        connection.execute(
                            insert(app_instance).values(
                                id=identity.internal_id,
                                name=f"legacy-{identity.evidence.value}-{identity.alias_digest}",
                                active=False,
                                created_at=datetime.now(UTC),
                            )
                        )
                    connection.execute(
                        insert(legacy_instance_alias).values(
                            alias_kind=identity.alias_kind,
                            alias_digest=identity.alias_digest,
                            instance_id=identity.internal_id,
                            source_provenance=identity.evidence.value,
                            created_at=datetime.now(UTC),
                        )
                    )
            return MigrationDelta()
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def import_records(
        self, batch_id: UUID, records: tuple[MigrationRecord, ...]
    ) -> MigrationDelta:
        delta = MigrationDelta()
        try:
            with self._engine.get().begin() as connection:
                for record in records:
                    existing = connection.execute(
                        select(import_record.c.payload_digest).where(
                            import_record.c.batch_id == batch_id,
                            import_record.c.source_object == record.source_object,
                            import_record.c.source_locator == record.source_locator,
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        if existing != record.payload_digest:
                            raise StorageConflictError(
                                "Конфликт legacy source locator."
                            )
                        delta += MigrationDelta(skipped=1)
                        continue
                    if record.disposition is RecordDisposition.QUARANTINE:
                        self._insert_ledger(connection, batch_id, record, None)
                        delta += MigrationDelta(quarantined=1)
                        continue
                    target_key = self._insert_domain(connection, batch_id, record)
                    self._insert_ledger(connection, batch_id, record, target_key)
                    delta += MigrationDelta(inserted=1)
            return delta
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def complete(
        self, batch_id: UUID, plan: LegacyMigrationPlan, delta: MigrationDelta
    ) -> None:
        del delta
        try:
            with self._engine.get().begin() as connection:
                rows = (
                    connection.execute(
                        select(import_record.c.disposition).where(
                            import_record.c.batch_id == batch_id
                        )
                    )
                    .scalars()
                    .all()
                )
                quarantine_count = sum(
                    value.startswith("quarantine:") for value in rows
                )
                imported_count = len(rows) - quarantine_count
                if len(rows) != len(plan.records):
                    raise StorageConflictError("Import record coverage неполон.")
                result = connection.execute(
                    update(import_batch)
                    .where(
                        import_batch.c.id == batch_id,
                        import_batch.c.status == "started",
                    )
                    .values(
                        status="completed",
                        finished_at=datetime.now(UTC),
                        record_count=len(rows),
                        imported_count=imported_count,
                        conflict_count=0,
                        quarantine_count=quarantine_count,
                        error_code=None,
                    )
                )
                if result.rowcount != 1:
                    raise StorageConflictError("Import batch transition отклонён.")
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def fail(self, batch_id: UUID, reason_code: str, *, conflict: bool) -> None:
        try:
            with self._engine.get().begin() as connection:
                connection.execute(
                    update(import_batch)
                    .where(
                        import_batch.c.id == batch_id,
                        import_batch.c.status != "completed",
                    )
                    .values(
                        status="conflict" if conflict else "failed",
                        finished_at=datetime.now(UTC),
                        error_code=reason_code,
                    )
                )
        except SQLAlchemyError:
            # Сохраняем исходную безопасную application error как root cause.
            return

    def project(self, batch_id: UUID, plan: LegacyMigrationPlan) -> TargetProjection:
        try:
            with self._engine.get().connect() as connection:
                version = int(
                    connection.execute(text("SHOW server_version_num")).scalar_one()
                )
                heads = tuple(
                    connection.execute(
                        text(
                            "SELECT version_num FROM alembic_version ORDER BY version_num"
                        )
                    ).scalars()
                )
                if heads != (EXPECTED_ALEMBIC_HEAD,):
                    raise StorageConflictError(
                        "Schema head изменился во время reconciliation."
                    )
                rows = connection.execute(
                    select(
                        import_record.c.source_object,
                        import_record.c.source_locator,
                        import_record.c.disposition,
                        import_record.c.payload_digest,
                        import_record.c.target_table,
                        import_record.c.target_key,
                    ).where(import_record.c.batch_id == batch_id)
                ).all()
                records = {
                    (record.source_object, record.source_locator): record
                    for record in plan.records
                }
                domain_rows_match = len(rows) == len(records)
                for (
                    source_object,
                    source_locator,
                    disposition,
                    digest,
                    target_table,
                    target_key,
                ) in rows:
                    record = records.get((source_object, source_locator))
                    if record is None or digest != record.payload_digest:
                        domain_rows_match = False
                        continue
                    if record.disposition is RecordDisposition.QUARANTINE:
                        domain_rows_match = domain_rows_match and (
                            disposition == f"quarantine:{record.dataset}"
                            and target_table is None
                            and target_key is None
                        )
                    else:
                        domain_rows_match = domain_rows_match and (
                            disposition == record.dataset
                            and target_table == record.dataset
                            and self._domain_row_matches(connection, record, target_key)
                        )
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        grouped: dict[str, list[str]] = defaultdict(list)
        for _, _, disposition, digest, _, _ in rows:
            dataset = disposition.split(":", 1)[-1]
            grouped[dataset].append(digest)
        return TargetProjection(
            postgres_major=version // 10_000,
            schema_head=heads[0],
            covered_records=len(rows),
            dataset_counts=tuple(
                sorted((dataset, len(digests)) for dataset, digests in grouped.items())
            ),
            dataset_digests=tuple(
                sorted(
                    (dataset, canonical_digest(sorted(digests)))
                    for dataset, digests in grouped.items()
                )
            ),
            domain_rows_match=domain_rows_match,
        )

    @staticmethod
    def _domain_row_matches(
        connection: Connection,
        record: MigrationRecord,
        ledger_target_key: str | None,
    ) -> bool:
        values = record.as_dict()
        identity_id = uuid5(
            UUID("bc6db2da-cb91-4d6e-bc33-bb598d715c13"),
            record.identity_digest,
        )

        if record.dataset == "monthly_aggregate":
            expected = {
                "instance_id": identity_id,
                "month": values["month"],
                "metric": values["metric"],
                "value": values["value"],
                "source_kind": values["source_kind"],
                "source_digest": record.payload_digest,
            }
            conditions = (
                monthly_aggregate.c.instance_id == identity_id,
                monthly_aggregate.c.month == values["month"],
                monthly_aggregate.c.metric == values["metric"],
            )
            table = monthly_aggregate
            expected_target_key = f"{identity_id}:{values['month']}:{values['metric']}"
        elif record.dataset == "meow_hazard":
            expected = {"instance_id": identity_id, **values}
            conditions = (
                meow_hazard_aggregate.c.instance_id == identity_id,
                meow_hazard_aggregate.c.month == values["month"],
                meow_hazard_aggregate.c.hazard_level == values["hazard_level"],
            )
            table = meow_hazard_aggregate
            expected_target_key = (
                f"{identity_id}:{values['month']}:{values['hazard_level']}"
            )
        elif record.dataset == "siren_stat":
            expected = {"instance_id": identity_id, **values}
            conditions = (
                siren_research_device_stat.c.instance_id == identity_id,
                siren_research_device_stat.c.month == values["month"],
                siren_research_device_stat.c.source == values["source"],
                siren_research_device_stat.c.hazard_level == values["hazard_level"],
            )
            table = siren_research_device_stat
            expected_target_key = ":".join(
                map(
                    str,
                    (
                        identity_id,
                        values["month"],
                        values["source"],
                        values["hazard_level"],
                    ),
                )
            )
        elif record.dataset == "ap_notification":
            expected = {
                "instance_id": identity_id,
                "last_ap": values["last_ap"],
                "notified_at": values["observed_at"],
                "legacy_timestamp_text": values["legacy_timestamp_text"],
                "legacy_timezone": values["legacy_timezone"],
            }
            conditions = (ap_notification_state.c.instance_id == identity_id,)
            table = ap_notification_state
            expected_target_key = str(identity_id)
        else:
            table = _EVENT_TABLES.get(record.dataset)
            if table is None:
                return False
            try:
                target_id = UUID(ledger_target_key or "")
            except ValueError:
                return False
            expected = {
                key: value
                for key, value in values.items()
                if key in table.c and key not in {"legacy_row_id"}
            }
            expected.update(
                id=target_id,
                instance_id=identity_id,
                idempotency_key=(
                    "legacy-v1:"
                    + canonical_digest((record.source_object, record.source_locator))
                ),
                payload_digest=record.payload_digest,
            )
            conditions = (table.c.id == target_id,)
            expected_target_key = str(target_id)

        if ledger_target_key != expected_target_key:
            return False
        row = (
            connection.execute(select(table).where(*conditions))
            .mappings()
            .one_or_none()
        )
        if row is None or any(row[key] != value for key, value in expected.items()):
            return False
        if record.dataset != "commission":
            return True
        items = values.get("items")
        if not isinstance(items, tuple):
            return False
        actual_items = tuple(
            connection.execute(
                select(
                    commission_income_item.c.item_code,
                    commission_income_item.c.amount,
                )
                .where(commission_income_item.c.event_id == expected["id"])
                .order_by(commission_income_item.c.item_code)
            )
        )
        return actual_items == tuple(sorted(items))

    @staticmethod
    def _insert_ledger(
        connection: Connection,
        batch_id: UUID,
        record: MigrationRecord,
        target_key: str | None,
    ) -> None:
        disposition = (
            f"quarantine:{record.dataset}"
            if record.disposition is RecordDisposition.QUARANTINE
            else record.dataset
        )
        connection.execute(
            insert(import_record).values(
                batch_id=batch_id,
                source_object=record.source_object,
                source_locator=record.source_locator,
                payload_digest=record.payload_digest,
                disposition=disposition,
                target_table=None
                if record.disposition is RecordDisposition.QUARANTINE
                else record.dataset,
                target_key=target_key,
                quarantine_metadata=None
                if record.disposition is RecordDisposition.IMPORT
                else {"reason_code": record.reason_code},
            )
        )

    def _insert_domain(
        self, connection: Connection, batch_id: UUID, record: MigrationRecord
    ) -> str:
        values = record.as_dict()
        identity_id = uuid5(
            UUID("bc6db2da-cb91-4d6e-bc33-bb598d715c13"),
            record.identity_digest,
        )
        target_id = uuid5(
            _TARGET_NAMESPACE,
            f"{batch_id}:{record.source_object}:{record.source_locator}",
        )
        idempotency_key = "legacy-v1:" + canonical_digest(
            (record.source_object, record.source_locator)
        )

        if record.dataset == "monthly_aggregate":
            payload = {
                "instance_id": identity_id,
                "month": values["month"],
                "metric": values["metric"],
                "value": values["value"],
                "source_kind": values["source_kind"],
                "source_digest": record.payload_digest,
            }
            self._insert_composite(
                connection,
                monthly_aggregate,
                {
                    "instance_id": identity_id,
                    "month": values["month"],
                    "metric": values["metric"],
                },
                payload,
                digest_column="source_digest",
                digest=record.payload_digest,
            )
            return f"{identity_id}:{values['month']}:{values['metric']}"

        if record.dataset == "meow_hazard":
            payload = {"instance_id": identity_id, **values}
            keys = {
                "instance_id": identity_id,
                "month": values["month"],
                "hazard_level": values["hazard_level"],
            }
            self._insert_composite(connection, meow_hazard_aggregate, keys, payload)
            return f"{identity_id}:{values['month']}:{values['hazard_level']}"

        if record.dataset == "siren_stat":
            payload = {"instance_id": identity_id, **values}
            keys = {
                "instance_id": identity_id,
                "month": values["month"],
                "source": values["source"],
                "hazard_level": values["hazard_level"],
            }
            self._insert_composite(
                connection, siren_research_device_stat, keys, payload
            )
            return ":".join(map(str, keys.values()))

        if record.dataset == "ap_notification":
            payload = {
                "instance_id": identity_id,
                "last_ap": values["last_ap"],
                "notified_at": values["observed_at"],
                "legacy_timestamp_text": values["legacy_timestamp_text"],
                "legacy_timezone": values["legacy_timezone"],
            }
            self._insert_composite(
                connection,
                ap_notification_state,
                {"instance_id": identity_id},
                payload,
            )
            return str(identity_id)

        table = _EVENT_TABLES.get(record.dataset)
        if table is None:
            raise StorageConflictError("Dataset не входит в migration allowlist.")
        payload = {
            key: value
            for key, value in values.items()
            if key in table.c and key not in {"legacy_row_id"}
        }
        payload.update(
            id=target_id,
            instance_id=identity_id,
            idempotency_key=idempotency_key,
            payload_digest=record.payload_digest,
        )
        existing_event = connection.execute(
            select(table.c.id, table.c.payload_digest).where(
                table.c.idempotency_key == idempotency_key
            )
        ).one_or_none()
        if existing_event is not None:
            if existing_event.payload_digest != record.payload_digest:
                raise StorageConflictError("Конфликт event idempotency key.")
            return str(existing_event.id)
        if record.dataset == "commission":
            items = values.get("items")
            payload.pop("items", None)
            connection.execute(insert(table).values(**payload))
            if not isinstance(items, tuple):
                raise StorageConflictError("Commission items имеют неверный тип.")
            if items:
                connection.execute(
                    insert(commission_income_item),
                    [
                        {"event_id": target_id, "item_code": code, "amount": amount}
                        for code, amount in items
                    ],
                )
        else:
            connection.execute(insert(table).values(**payload))
        return str(target_id)

    @staticmethod
    def _insert_composite(
        connection: Connection,
        table,
        keys: dict[str, object],
        payload: dict[str, object],
        *,
        digest_column: str | None = None,
        digest: str | None = None,
    ) -> None:
        conditions = [table.c[name] == value for name, value in keys.items()]
        existing = (
            connection.execute(select(table).where(*conditions))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            connection.execute(insert(table).values(**payload))
            return
        if digest_column is not None and existing[digest_column] == digest:
            return
        comparable = {
            key: value
            for key, value in payload.items()
            if key in existing and key not in {"version"}
        }
        if any(existing[key] != value for key, value in comparable.items()):
            raise StorageConflictError("Конфликт composite migration target.")
