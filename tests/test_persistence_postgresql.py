from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from multiprocessing import get_context
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, insert, select, text

from module.application import (
    CommissionIncome,
    CommissionItem,
    ImportBatch,
    ImportBatchStatus,
    InstanceIdentity,
    MonthlyMetric,
    OpsiItemEvent,
    ResourceSnapshot,
    StorageConflictError,
    StorageHealthState,
    StorageInvalidDataError,
    StorageUnavailableError,
)
from module.persistence import DatabaseSettings, LazyEngine, PostgresUnitOfWork
from module.persistence.config import PoolSettings
from module.persistence.database import StorageHealthChecker
from module.persistence.schema import (
    app_instance,
    commission_income_event,
    commission_income_item,
    metadata,
    monthly_aggregate,
)

REQUIRED_ENV = (
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_DATABASE",
    "AZURPILOT_POSTGRES_USER",
)
pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in REQUIRED_ENV)
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL Stage 2 DB",
)


def _increment_worker(instance_id: str, loops: int) -> None:
    engine = LazyEngine(DatabaseSettings.from_environment())
    for _ in range(loops):
        with PostgresUnitOfWork(engine) as uow:
            uow.statistics.increment_monthly_counter(
                UUID(instance_id),
                date(2026, 8, 1),
                MonthlyMetric.BATTLE_COUNT,
                Decimal(1),
            )
            uow.commit()
    engine.dispose()


@pytest.fixture
def database():
    if os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1":
        pytest.fail(
            "Фикстура очищает schema и требует AZURPILOT_POSTGRES_DISPOSABLE=1."
        )
    lazy = LazyEngine(DatabaseSettings.from_environment())
    with lazy.get().begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
    yield lazy
    lazy.dispose()


def _instance(database: LazyEngine) -> UUID:
    instance_id = uuid4()
    with database.get().begin() as connection:
        connection.execute(
            insert(app_instance).values(
                id=instance_id,
                name=f"stage2-{instance_id}",
                created_at=datetime.now(UTC),
            )
        )
    return instance_id


def _snapshot(instance_id: UUID, key: str, *, oil: int = 10) -> ResourceSnapshot:
    return ResourceSnapshot(
        id=uuid4(),
        instance_id=instance_id,
        idempotency_key=key,
        observed_at=datetime(2026, 8, 23, 1, tzinfo=UTC),
        source="sanitized_fixture",
        oil=oil,
    )


def test_health_and_migration_head_are_ready(database: LazyEngine):
    assert StorageHealthChecker(database).check().state is StorageHealthState.READY


def test_digest_only_instance_identity_resolution(database: LazyEngine):
    identity = InstanceIdentity(uuid4(), "stage2-identity")
    digest = "c" * 64
    with PostgresUnitOfWork(database) as uow:
        assert uow.instances.register(
            identity,
            alias_kind="legacy_profile",
            alias_digest=digest,
            source_provenance="sanitized_fixture",
        )
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.instances.resolve(alias_kind="legacy_profile", alias_digest=digest)
            == identity
        )
        assert (
            uow.instances.resolve(alias_kind="legacy_profile", alias_digest="d" * 64)
            is None
        )
    with PostgresUnitOfWork(database) as uow:
        assert not uow.instances.register(
            identity,
            alias_kind="legacy_profile",
            alias_digest=digest,
            source_provenance="sanitized_fixture",
        )
        uow.commit()


def test_repository_boundary_rejects_untyped_or_unbounded_data(database: LazyEngine):
    instance_id = _instance(database)
    with PostgresUnitOfWork(database) as uow:
        with pytest.raises(StorageInvalidDataError):
            uow.instances.resolve(alias_kind="legacy_profile", alias_digest="G" * 64)
        with pytest.raises(StorageInvalidDataError):
            uow.statistics.increment_monthly_counter(
                instance_id,
                date(2026, 8, 1),
                MonthlyMetric.BATTLE_COUNT,
                1,  # type: ignore[arg-type]
            )
        with pytest.raises(StorageInvalidDataError):
            uow.statistics.append_resource_snapshot(
                _snapshot(instance_id, "negative-resource", oil=-1)
            )
        with pytest.raises(StorageInvalidDataError):
            uow.imports.begin(
                ImportBatch(
                    id=uuid4(),
                    idempotency_key="invalid-digest",
                    source_kind="sanitized_fixture",
                    source_digest="Z" * 64,
                    status=ImportBatchStatus.STARTED,
                    started_at=datetime.now(UTC),
                )
            )


def test_health_authentication_and_unavailable_are_distinct():
    settings = DatabaseSettings.from_environment()
    wrong_password = DatabaseSettings(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password="definitely-wrong-synthetic-password",
        connect_timeout_seconds=1,
        sslmode=settings.sslmode,
        pool=PoolSettings(timeout_seconds=1),
    )
    unavailable = DatabaseSettings(
        host=settings.host,
        port=1,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout_seconds=1,
        sslmode=settings.sslmode,
        pool=PoolSettings(timeout_seconds=1),
    )
    wrong_password_engine = LazyEngine(wrong_password)
    unavailable_engine = LazyEngine(unavailable)
    try:
        assert StorageHealthChecker(wrong_password_engine).check().state is (
            StorageHealthState.AUTHENTICATION_FAILED
        )
        assert StorageHealthChecker(unavailable_engine).check().state is (
            StorageHealthState.UNAVAILABLE
        )
    finally:
        wrong_password_engine.dispose()
        unavailable_engine.dispose()


def test_pool_exhaustion_is_mapped_to_storage_unavailable():
    settings = DatabaseSettings.from_environment()
    constrained = LazyEngine(
        DatabaseSettings(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.user,
            password=settings.password,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            sslmode=settings.sslmode,
            pool=PoolSettings(size=1, max_overflow=0, timeout_seconds=0.1),
        )
    )
    held = constrained.get().connect()
    try:
        with pytest.raises(StorageUnavailableError), PostgresUnitOfWork(constrained):
            pass
    finally:
        held.close()
        constrained.dispose()


def test_atomic_counter_across_spawned_processes(database: LazyEngine):
    instance_id = _instance(database)
    context = get_context("spawn")
    processes = [
        context.Process(target=_increment_worker, args=(str(instance_id), 15))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(60)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
    with database.get().connect() as connection:
        row = connection.execute(
            select(monthly_aggregate.c.value, monthly_aggregate.c.version).where(
                monthly_aggregate.c.instance_id == instance_id
            )
        ).one()
    assert row.value == Decimal(45)
    assert row.version == 45


def test_idempotent_snapshot_conflict_and_deterministic_timeline(database: LazyEngine):
    instance_id = _instance(database)
    first = _snapshot(instance_id, "snapshot-1")
    with PostgresUnitOfWork(database) as uow:
        assert uow.statistics.append_resource_snapshot(first) is True
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.statistics.append_resource_snapshot(replace(first, id=uuid4())) is False
        )
        uow.commit()
    with PostgresUnitOfWork(database) as uow, pytest.raises(StorageConflictError):
        uow.statistics.append_resource_snapshot(replace(first, id=uuid4(), oil=11))
    second = replace(
        first,
        id=uuid4(),
        idempotency_key="snapshot-2",
        observed_at=first.observed_at + timedelta(seconds=1),
    )
    with PostgresUnitOfWork(database) as uow:
        uow.statistics.append_resource_snapshot(second)
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        timeline = uow.statistics.resource_timeline(instance_id, limit=10)
    assert tuple(item.idempotency_key for item in timeline) == (
        "snapshot-1",
        "snapshot-2",
    )


def test_unit_of_work_tracks_transactions_after_commit(database: LazyEngine):
    instance_id = _instance(database)
    with PostgresUnitOfWork(database) as uow:
        assert uow.statistics.append_resource_snapshot(
            _snapshot(instance_id, "multi-commit-1")
        )
        uow.commit()
        assert uow.statistics.append_resource_snapshot(
            _snapshot(instance_id, "multi-commit-2")
        )
        uow.commit()
    with PostgresUnitOfWork(database) as read_uow:
        assert len(read_uow.statistics.resource_timeline(instance_id, limit=10)) == 2
    assert not hasattr(read_uow, "statistics")


def test_versioned_state_and_commission_rollback(database: LazyEngine):
    instance_id = _instance(database)
    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.statistics.update_current_resource(
                instance_id, "oil", 100, expected_version=0
            )
            == 1
        )
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.statistics.update_current_resource(
                instance_id, "oil", 101, expected_version=1
            )
            == 2
        )
        uow.commit()
    with PostgresUnitOfWork(database) as uow, pytest.raises(StorageConflictError):
        uow.statistics.update_current_resource(
            instance_id, "oil", 102, expected_version=1
        )

    income = CommissionIncome(
        id=uuid4(),
        instance_id=instance_id,
        idempotency_key="commission-rollback",
        observed_at=datetime.now(UTC),
        commission_count=2,
        source="sanitized_fixture",
        items=(CommissionItem("coin", 10), CommissionItem("oil", 20)),
    )
    with pytest.raises(RuntimeError), PostgresUnitOfWork(database) as uow:
        assert uow.statistics.record_commission_income(income)
        raise RuntimeError("rollback")
    with database.get().connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(commission_income_event)
                .where(commission_income_event.c.instance_id == instance_id)
            )
            == 0
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(
                    commission_income_item.join(
                        commission_income_event,
                        commission_income_item.c.event_id
                        == commission_income_event.c.id,
                    )
                )
                .where(commission_income_event.c.instance_id == instance_id)
            )
            == 0
        )
    with PostgresUnitOfWork(database) as uow:
        assert uow.statistics.record_commission_income(income)
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert not uow.statistics.record_commission_income(
            replace(income, id=uuid4(), items=tuple(reversed(income.items)))
        )
        uow.commit()
    with database.get().connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(commission_income_event)
                .where(commission_income_event.c.instance_id == instance_id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(
                    commission_income_item.join(
                        commission_income_event,
                        commission_income_item.c.event_id
                        == commission_income_event.c.id,
                    )
                )
                .where(commission_income_event.c.instance_id == instance_id)
            )
            == 2
        )


def test_opsi_event_and_import_ledger_semantics(database: LazyEngine):
    instance_id = _instance(database)
    event = OpsiItemEvent(
        id=uuid4(),
        instance_id=instance_id,
        idempotency_key="opsi-1",
        observed_at=datetime.now(UTC),
        imgid="fixture-image",
        genre="opsi_meowfficer_farming",
        item_code="OperationCoin",
        amount=10,
        hazard_level=1,
    )
    with PostgresUnitOfWork(database) as uow:
        assert uow.statistics.append_opsi_item_event(event)
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert not uow.statistics.append_opsi_item_event(replace(event, id=uuid4()))
        uow.commit()

    batch = ImportBatch(
        id=uuid4(),
        idempotency_key="batch-1",
        source_kind="sanitized_fixture",
        source_digest="a" * 64,
        status=ImportBatchStatus.STARTED,
        started_at=datetime.now(UTC),
    )
    with PostgresUnitOfWork(database) as uow:
        assert uow.imports.begin(batch)
        uow.commit()
    with PostgresUnitOfWork(database) as uow:
        assert not uow.imports.begin(batch)
        uow.commit()
    with PostgresUnitOfWork(database) as uow, pytest.raises(StorageConflictError):
        uow.imports.begin(replace(batch, id=uuid4(), source_digest="b" * 64))
    with PostgresUnitOfWork(database) as uow:
        uow.imports.complete(batch.id, record_count=2, imported_count=2)
        uow.commit()
    with PostgresUnitOfWork(database) as uow, pytest.raises(StorageConflictError):
        uow.imports.fail(batch.id, error_code="already_done")


def test_health_fails_closed_for_wrong_and_multiple_heads(database: LazyEngine):
    with database.get().begin() as connection:
        original_heads = tuple(
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
        )
        assert original_heads, "alembic_version пуста: миграции не применены"
    try:
        with database.get().begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = 'wrong_head'")
            )
        assert (
            StorageHealthChecker(database).check().state
            is StorageHealthState.INCOMPATIBLE_SCHEMA
        )
        with database.get().begin() as connection:
            connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES ('second_head')")
            )
        assert (
            StorageHealthChecker(database).check().state
            is StorageHealthState.INCOMPATIBLE_SCHEMA
        )
    finally:
        with database.get().begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            for head in original_heads:
                connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                    {"head": head},
                )
