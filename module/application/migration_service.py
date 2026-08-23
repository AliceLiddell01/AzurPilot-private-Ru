"""Application-owned orchestration offline-миграции и reconciliation."""

from __future__ import annotations

from dataclasses import replace

from module.application.errors import StorageConflictError
from module.application.migration_models import (
    IdentityEvidence,
    LegacyMigrationPlan,
    MigrationDelta,
    ReconciliationReport,
    RecordDisposition,
)
from module.application.migration_ports import LegacyMigrationSource, MigrationTarget


class MigrationService:
    """Детерминированно переносит bounded chunks без production singleton."""

    def __init__(self, source: LegacyMigrationSource, target: MigrationTarget):
        self._source = source
        self._target = target

    def inspect(self) -> LegacyMigrationPlan:
        return self._source.capture()

    def run(self, *, chunk_size: int = 500) -> ReconciliationReport:
        if not 1 <= chunk_size <= 5_000:
            raise ValueError("chunk_size должен быть в диапазоне 1..5000")
        plan = self.inspect()
        self._target.preflight()
        state = self._target.begin(plan)
        delta = MigrationDelta()
        try:
            if not state.already_completed:
                delta += self._target.import_identities(state.batch_id, plan.identities)
                ordered = sorted(
                    plan.records,
                    key=lambda item: (
                        item.dataset,
                        item.identity_digest,
                        item.source_object,
                        item.source_locator,
                    ),
                )
                for offset in range(0, len(ordered), chunk_size):
                    delta += self._target.import_records(
                        state.batch_id, tuple(ordered[offset : offset + chunk_size])
                    )
                self._target.complete(state.batch_id, plan, delta)
        except StorageConflictError:
            self._target.fail(state.batch_id, "IDEMPOTENCY_CONFLICT", conflict=True)
            raise
        except Exception:
            self._target.fail(state.batch_id, "IMPORT_FAILED", conflict=False)
            raise

        projection = self._target.project(state.batch_id, plan)
        source_counts = plan.dataset_counts()
        source_digests = plan.dataset_digests()
        quarantines = sum(
            record.disposition is RecordDisposition.QUARANTINE
            for record in plan.records
        )
        coverage = projection.covered_records == len(plan.records)
        semantic_parity = (
            projection.domain_rows_match
            and projection.dataset_counts == source_counts
            and projection.dataset_digests == source_digests
        )
        reasons: list[str] = []
        if quarantines:
            reasons.append("QUARANTINED_RECORDS")
        if not coverage:
            reasons.append("SOURCE_COVERAGE_MISMATCH")
        if not semantic_parity:
            reasons.append("SEMANTIC_SHADOW_MISMATCH")
        if plan.derived_csv_parity is False:
            reasons.append("DERIVED_CSV_MISMATCH")
        if projection.postgres_major != 18:
            reasons.append("POSTGRES_MAJOR_MISMATCH")
        reasons.append("DUMP_RESTORE_NOT_VERIFIED")

        return ReconciliationReport(
            manifest_digest=plan.manifest_digest,
            sources=plan.manifest,
            timezone_policy=plan.timezone_policy,
            source_dataset_counts=source_counts,
            target_dataset_counts=projection.dataset_counts,
            safe_summary=plan.safe_summary(),
            unresolved_identities=sum(
                identity.evidence is IdentityEvidence.UNRESOLVED
                for identity in plan.identities
            ),
            run_delta=delta,
            repeat_import_zero_delta=state.already_completed and delta.inserted == 0,
            derived_csv_parity=plan.derived_csv_parity,
            postgres_major=projection.postgres_major,
            schema_head=projection.schema_head,
            source_record_coverage=coverage,
            semantic_shadow_parity=semantic_parity,
            dump_restore_parity=None,
            cutover_ready=not reasons,
            reason_codes=tuple(sorted(reasons)),
        )


def finalize_rehearsal(
    first: ReconciliationReport,
    repeat: ReconciliationReport,
    restored: ReconciliationReport,
) -> ReconciliationReport:
    """Связать import/repeat/dump-restore evidence в один readiness verdict."""

    repeat_zero = (
        repeat.run_delta.inserted == 0
        and repeat.run_delta.quarantined == 0
        and repeat.run_delta.conflicts == 0
        and repeat.semantic_shadow_parity
        and repeat.manifest_digest == first.manifest_digest
        and repeat.source_dataset_counts == first.source_dataset_counts
        and repeat.target_dataset_counts == first.target_dataset_counts
        and repeat.safe_summary == first.safe_summary
        and repeat.schema_head == first.schema_head
        and repeat.postgres_major == first.postgres_major
    )
    restore_parity = (
        restored.semantic_shadow_parity
        and restored.source_record_coverage
        and restored.manifest_digest == first.manifest_digest
        and restored.source_dataset_counts == first.source_dataset_counts
        and restored.target_dataset_counts == first.target_dataset_counts
        and restored.safe_summary == first.safe_summary
        and restored.derived_csv_parity == first.derived_csv_parity
        and restored.schema_head == first.schema_head
        and restored.postgres_major == first.postgres_major
    )
    reasons = set(first.reason_codes)
    reasons.discard("DUMP_RESTORE_NOT_VERIFIED")
    if not repeat_zero:
        reasons.add("REPEAT_IMPORT_DELTA")
    if not restore_parity:
        reasons.add("DUMP_RESTORE_MISMATCH")
    return replace(
        first,
        repeat_import_zero_delta=repeat_zero,
        dump_restore_parity=restore_parity,
        cutover_ready=not reasons,
        reason_codes=tuple(sorted(reasons)),
    )
