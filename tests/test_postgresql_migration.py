from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update

from module.application import StorageConflictError
from module.application.migration_models import MigrationRecord, canonical_digest
from module.application.migration_service import MigrationService
from module.persistence import DatabaseSettings, LazyEngine
from module.persistence.legacy import LegacySourceReader
from module.persistence.migration_target import PostgresMigrationTarget
from module.persistence.schema import (
    cl1_ap_snapshot,
    import_batch,
    import_record,
    metadata,
    monthly_aggregate,
)

ROOT = Path(__file__).resolve().parents[1]
CL1_FIXTURE = ROOT / "tests" / "fixtures" / "postgresql_migration" / "cl1_shapes.json"
REQUIRED_ENV = (
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_DATABASE",
    "AZURPILOT_POSTGRES_USER",
)
pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in REQUIRED_ENV)
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL Stage 3 DB",
)


@pytest.fixture
def database():
    engine = LazyEngine(DatabaseSettings.from_environment())
    with engine.get().begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
    yield engine
    engine.dispose()


def _source(tmp_path: Path) -> LegacySourceReader:
    payload = CL1_FIXTURE.read_text(encoding="utf-8")
    database = tmp_path / "config" / "cl1_data.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE cl1_data (instance TEXT, month TEXT, data_json TEXT, "
            "encrypted_blob BLOB, PRIMARY KEY (instance, month))"
        )
        connection.execute(
            "INSERT INTO cl1_data VALUES ('fixture', '2026-08', ?, NULL)",
            (payload,),
        )
    return LegacySourceReader(
        tmp_path,
        legacy_timezone="Asia/Novosibirsk",
        profile_names=("fixture",),
    )


def test_full_import_reconciliation_and_repeat_zero_delta(database, tmp_path):
    service = MigrationService(_source(tmp_path), PostgresMigrationTarget(database))

    first = service.run(chunk_size=3)
    repeat = service.run(chunk_size=3)

    assert first.run_delta.inserted == sum(dict(first.source_dataset_counts).values())
    assert first.semantic_shadow_parity
    assert first.source_record_coverage
    assert first.reason_codes == ("DUMP_RESTORE_NOT_VERIFIED",)
    assert repeat.run_delta.inserted == 0
    assert repeat.repeat_import_zero_delta
    assert repeat.semantic_shadow_parity


def test_same_locator_different_digest_is_hard_conflict(database, tmp_path):
    reader = _source(tmp_path)
    plan = reader.capture()
    target = PostgresMigrationTarget(database)
    target.preflight()
    state = target.begin(plan)
    target.import_identities(state.batch_id, plan.identities)
    original = plan.records[0]
    target.import_records(state.batch_id, (original,))
    changed = replace(
        original,
        payload_digest="f" * 64,
        values=(*original.values, ("synthetic_conflict", 1)),
    )

    with pytest.raises(StorageConflictError):
        target.import_records(state.batch_id, (changed,))


def test_distinct_source_locators_preserve_identical_event_payloads(database, tmp_path):
    plan = _source(tmp_path).capture()
    target = PostgresMigrationTarget(database)
    target.preflight()
    state = target.begin(plan)
    target.import_identities(state.batch_id, plan.identities)
    original = next(
        record for record in plan.records if record.dataset == "ap_snapshot"
    )
    second = replace(original, source_locator=original.source_locator + "/duplicate")

    delta = target.import_records(state.batch_id, (original, second))

    assert delta.inserted == 2
    with database.get().connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(cl1_ap_snapshot)
            ).scalar_one()
            == 2
        )


def test_chunk_failure_rolls_back_parent_children_and_ledger(database, tmp_path):
    plan = _source(tmp_path).capture()
    target = PostgresMigrationTarget(database)
    target.preflight()
    state = target.begin(plan)
    target.import_identities(state.batch_id, plan.identities)
    commission = next(
        record for record in plan.records if record.dataset == "commission"
    )
    invalid = MigrationRecord(
        dataset="unsupported",
        identity_digest=commission.identity_digest,
        source_object="fixture-invalid",
        source_locator="invalid/1",
        values=(("value", 1),),
        payload_digest=canonical_digest(("invalid", 1)),
    )

    with pytest.raises(StorageConflictError):
        target.import_records(state.batch_id, (commission, invalid))

    with database.get().connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(import_record)
            ).scalar_one()
            == 0
        )


def test_report_is_deterministic_and_contains_no_raw_identity(database, tmp_path):
    report = MigrationService(
        _source(tmp_path), PostgresMigrationTarget(database)
    ).run()

    first = report.to_json()
    second = report.to_json()
    assert first == second
    assert "fixture" not in first
    assert "password" not in first.casefold()
    assert str(tmp_path) not in first
    assert json.loads(first)["cutover_ready"] is False
    assert json.loads(first)["safe_summary"]["commission"]["item_count"] == 2


def test_reconciliation_reads_domain_rows_not_only_import_ledger(database, tmp_path):
    service = MigrationService(_source(tmp_path), PostgresMigrationTarget(database))
    assert service.run().semantic_shadow_parity
    with database.get().begin() as connection:
        connection.execute(
            update(monthly_aggregate)
            .where(monthly_aggregate.c.metric == "battle_count")
            .values(value=monthly_aggregate.c.value + 1)
        )

    report = service.run()

    assert not report.semantic_shadow_parity
    assert "SEMANTIC_SHADOW_MISMATCH" in report.reason_codes


def test_failed_batch_can_resume_with_same_manifest(database, tmp_path):
    plan = _source(tmp_path).capture()
    target = PostgresMigrationTarget(database)
    target.preflight()
    first = target.begin(plan)
    target.fail(first.batch_id, "SYNTHETIC_FAILURE", conflict=False)

    resumed = target.begin(plan)

    assert resumed.batch_id == first.batch_id
    assert not resumed.already_completed
    with database.get().connect() as connection:
        status = connection.execute(
            select(import_batch.c.status).where(import_batch.c.id == first.batch_id)
        ).scalar_one()
    assert status == "started"
