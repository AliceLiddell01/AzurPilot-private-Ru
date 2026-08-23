"""Транспортно-нейтральные модели offline-миграции legacy-хранилищ."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import TypeAlias
from uuid import UUID


class IdentityEvidence(StrEnum):
    EXACT_PROFILE = "exact_profile"
    UNRESOLVED = "unresolved"


class RecordDisposition(StrEnum):
    IMPORT = "import"
    QUARANTINE = "quarantine"


LegacyScalar: TypeAlias = str | int | Decimal | date | datetime | None
LegacyValue: TypeAlias = LegacyScalar | tuple[tuple[str, int], ...]


def canonical_digest(value: object) -> str:
    """Вернуть стабильный SHA-256 без зависимости от locale/repr."""

    def default(item: object) -> str:
        if isinstance(item, (date, datetime, Decimal)):
            return str(item)
        raise TypeError(
            f"Неподдерживаемый тип canonical payload: {type(item).__name__}"
        )

    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceManifestEntry:
    logical_id: str
    source_kind: str
    size: int
    sha256: str
    schema_fingerprint: str | None = None
    integrity: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyIdentity:
    alias_kind: str
    alias_digest: str
    internal_id: UUID
    evidence: IdentityEvidence


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    dataset: str
    identity_digest: str
    source_object: str
    source_locator: str
    values: tuple[tuple[str, LegacyValue], ...]
    payload_digest: str
    disposition: RecordDisposition = RecordDisposition.IMPORT
    reason_code: str | None = None

    def as_dict(self) -> dict[str, LegacyValue]:
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class LegacyMigrationPlan:
    manifest: tuple[SourceManifestEntry, ...]
    manifest_digest: str
    identities: tuple[LegacyIdentity, ...]
    records: tuple[MigrationRecord, ...]
    timezone_policy: str
    derived_csv_parity: bool | None

    def dataset_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.dataset] = counts.get(record.dataset, 0) + 1
        return tuple(sorted(counts.items()))

    def dataset_digests(self) -> tuple[tuple[str, str], ...]:
        grouped: dict[str, list[str]] = {}
        for record in self.records:
            grouped.setdefault(record.dataset, []).append(record.payload_digest)
        return tuple(
            (dataset, canonical_digest(sorted(digests)))
            for dataset, digests in sorted(grouped.items())
        )

    def safe_summary(self) -> "SafeReconciliationSummary":
        identity_month: dict[tuple[str, str], int] = defaultdict(int)
        scalar_values: dict[str, list[Decimal]] = defaultdict(list)
        timestamp_values: dict[str, list[str]] = defaultdict(list)
        resource_nulls: dict[str, int] = defaultdict(int)
        commission_parents = 0
        commission_items = 0
        commission_amount = 0
        for record in self.records:
            if record.disposition is RecordDisposition.QUARANTINE:
                continue
            values = record.as_dict()
            month = values.get("month")
            if isinstance(month, date):
                identity_month[(record.identity_digest, month.isoformat())] += 1
            for field, value in values.items():
                key = f"{record.dataset}.{field}"
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, Decimal)):
                    scalar_values[key].append(Decimal(value))
                elif isinstance(value, datetime):
                    timestamp_values[key].append(value.isoformat())
            if record.dataset == "resource_snapshot":
                for field, value in values.items():
                    if (
                        field
                        not in {
                            "legacy_row_id",
                            "observed_at",
                            "legacy_timestamp_text",
                            "legacy_timezone",
                        }
                        and value is None
                    ):
                        resource_nulls[field] += 1
            if record.dataset == "commission":
                commission_parents += 1
                items = values.get("items")
                if isinstance(items, tuple):
                    commission_items += len(items)
                    commission_amount += sum(amount for _, amount in items)
        scalar_sums = tuple(
            (key, str(sum(values, Decimal(0))))
            for key, values in sorted(scalar_values.items())
        )
        scalar_ranges = tuple(
            (key, str(min(values)), str(max(values)))
            for key, values in sorted(scalar_values.items())
        )
        timestamp_ranges = tuple(
            (key, min(values), max(values))
            for key, values in sorted(timestamp_values.items())
        )
        return SafeReconciliationSummary(
            identity_month_counts=tuple(
                (identity, month, count)
                for (identity, month), count in sorted(identity_month.items())
            ),
            scalar_sums=scalar_sums,
            scalar_ranges=scalar_ranges,
            timestamp_ranges=timestamp_ranges,
            resource_null_counts=tuple(sorted(resource_nulls.items())),
            commission_parent_count=commission_parents,
            commission_item_count=commission_items,
            commission_item_amount_sum=commission_amount,
        )


@dataclass(frozen=True, slots=True)
class SafeReconciliationSummary:
    identity_month_counts: tuple[tuple[str, str, int], ...]
    scalar_sums: tuple[tuple[str, str], ...]
    scalar_ranges: tuple[tuple[str, str, str], ...]
    timestamp_ranges: tuple[tuple[str, str, str], ...]
    resource_null_counts: tuple[tuple[str, int], ...]
    commission_parent_count: int
    commission_item_count: int
    commission_item_amount_sum: int

    def as_dict(self) -> dict[str, object]:
        return {
            "identity_month_counts": [
                {"identity_digest": identity, "month": month, "count": count}
                for identity, month, count in self.identity_month_counts
            ],
            "scalar_sums": dict(self.scalar_sums),
            "scalar_ranges": {
                key: {"min": minimum, "max": maximum}
                for key, minimum, maximum in self.scalar_ranges
            },
            "timestamp_ranges": {
                key: {"min": minimum, "max": maximum}
                for key, minimum, maximum in self.timestamp_ranges
            },
            "resource_null_counts": dict(self.resource_null_counts),
            "commission": {
                "parent_count": self.commission_parent_count,
                "item_count": self.commission_item_count,
                "item_amount_sum": self.commission_item_amount_sum,
            },
        }


@dataclass(frozen=True, slots=True)
class MigrationBatchState:
    batch_id: UUID
    already_completed: bool


@dataclass(frozen=True, slots=True)
class MigrationDelta:
    inserted: int = 0
    skipped: int = 0
    quarantined: int = 0
    conflicts: int = 0

    def __add__(self, other: "MigrationDelta") -> "MigrationDelta":
        return MigrationDelta(
            inserted=self.inserted + other.inserted,
            skipped=self.skipped + other.skipped,
            quarantined=self.quarantined + other.quarantined,
            conflicts=self.conflicts + other.conflicts,
        )


@dataclass(frozen=True, slots=True)
class TargetProjection:
    postgres_major: int
    schema_head: str
    covered_records: int
    dataset_counts: tuple[tuple[str, int], ...]
    dataset_digests: tuple[tuple[str, str], ...]
    domain_rows_match: bool


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    manifest_digest: str
    sources: tuple[SourceManifestEntry, ...]
    timezone_policy: str
    source_dataset_counts: tuple[tuple[str, int], ...]
    target_dataset_counts: tuple[tuple[str, int], ...]
    safe_summary: SafeReconciliationSummary
    unresolved_identities: int
    run_delta: MigrationDelta
    repeat_import_zero_delta: bool
    derived_csv_parity: bool | None
    postgres_major: int
    schema_head: str
    source_record_coverage: bool
    semantic_shadow_parity: bool
    dump_restore_parity: bool | None
    cutover_ready: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "azurpilot-postgresql-migration-report-v1",
            "manifest_digest": self.manifest_digest,
            "sources": [
                {
                    "logical_id": item.logical_id,
                    "source_kind": item.source_kind,
                    "size": item.size,
                    "sha256": item.sha256,
                    "schema_fingerprint": item.schema_fingerprint,
                    "integrity": item.integrity,
                }
                for item in self.sources
            ],
            "timezone_policy": self.timezone_policy,
            "source_dataset_counts": dict(self.source_dataset_counts),
            "target_dataset_counts": dict(self.target_dataset_counts),
            "safe_summary": self.safe_summary.as_dict(),
            "unresolved_identities": self.unresolved_identities,
            "run_delta": {
                "inserted": self.run_delta.inserted,
                "skipped": self.run_delta.skipped,
                "quarantined": self.run_delta.quarantined,
                "conflicts": self.run_delta.conflicts,
            },
            "repeat_import_zero_delta": self.repeat_import_zero_delta,
            "derived_csv_parity": self.derived_csv_parity,
            "postgres_major": self.postgres_major,
            "schema_head": self.schema_head,
            "source_record_coverage": self.source_record_coverage,
            "semantic_shadow_parity": self.semantic_shadow_parity,
            "dump_restore_parity": self.dump_restore_parity,
            "cutover_ready": self.cutover_ready,
            "reason_codes": list(self.reason_codes),
        }

    def to_json(self) -> str:
        return (
            json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
