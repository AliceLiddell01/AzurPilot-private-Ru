"""SQLAlchemy Core metadata для PostgreSQL schema v1."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from module.application.resource_fields import RESOURCE_FIELDS
from module.application.storage_models import MonthlyMetric

SCHEMA_NAME = "azurpilot"
EXPECTED_ALEMBIC_HEAD = "0005_fleet_ship_form"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(schema=SCHEMA_NAME, naming_convention=NAMING_CONVENTION)


def _enum_values_sql(enum_type: type[MonthlyMetric]) -> str:
    return ", ".join(repr(member.value) for member in enum_type)


def _instance_fk() -> ForeignKey:
    return ForeignKey(f"{SCHEMA_NAME}.app_instance.id", ondelete="RESTRICT")


app_instance = Table(
    "app_instance",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", String(128), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("name"),
)

legacy_instance_alias = Table(
    "legacy_instance_alias",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("alias_kind", String(32), nullable=False),
    Column("alias_digest", String(64), nullable=False),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("source_provenance", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("alias_kind", "alias_digest"),
    CheckConstraint("alias_digest ~ '^[0-9a-f]{64}$'", name="alias_digest_sha256"),
)
Index("ix_legacy_instance_alias_instance_id", legacy_instance_alias.c.instance_id)

import_batch = Table(
    "import_batch",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("source_kind", String(64), nullable=False),
    Column("source_digest", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("record_count", Integer, nullable=False, server_default="0"),
    Column("imported_count", Integer, nullable=False, server_default="0"),
    Column("conflict_count", Integer, nullable=False, server_default="0"),
    Column("quarantine_count", Integer, nullable=False, server_default="0"),
    Column("error_code", String(64), nullable=True),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name="source_digest_sha256"),
    CheckConstraint(
        "status IN ('started', 'completed', 'failed', 'conflict')",
        name="status_allowed",
    ),
    CheckConstraint(
        "record_count >= 0 AND imported_count >= 0 AND conflict_count >= 0 "
        "AND quarantine_count >= 0",
        name="counts_nonnegative",
    ),
    CheckConstraint(
        "imported_count + conflict_count + quarantine_count <= record_count",
        name="counts_within_record_count",
    ),
)

import_record = Table(
    "import_record",
    metadata,
    Column(
        "batch_id",
        Uuid,
        ForeignKey(f"{SCHEMA_NAME}.import_batch.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_object", String(128), nullable=False),
    Column("source_locator", String(256), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("disposition", String(32), nullable=False),
    Column("target_table", String(63), nullable=True),
    Column("target_key", String(128), nullable=True),
    Column("quarantine_metadata", JSONB, nullable=True),
    PrimaryKeyConstraint("batch_id", "source_object", "source_locator"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint(
        "quarantine_metadata IS NULL OR "
        "octet_length(quarantine_metadata::text) <= 8192",
        name="quarantine_metadata_bounded",
    ),
)

monthly_aggregate = Table(
    "monthly_aggregate",
    metadata,
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("month", Date, nullable=False),
    Column("metric", String(64), nullable=False),
    Column("value", Numeric(30, 6), nullable=False),
    Column("source_kind", String(32), nullable=False),
    Column("source_digest", String(64), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    PrimaryKeyConstraint("instance_id", "month", "metric"),
    CheckConstraint(
        f"metric IN ({_enum_values_sql(MonthlyMetric)})",
        name="metric_allowed",
    ),
    CheckConstraint("value >= 0", name="value_nonnegative"),
    CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month_first_day"),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(
        "source_digest IS NULL OR source_digest ~ '^[0-9a-f]{64}$'",
        name="source_digest_optional_sha256",
    ),
)

RESOURCE_COLUMNS = RESOURCE_FIELDS

resource_snapshot = Table(
    "resource_snapshot",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("source", String(64), nullable=False),
    *(Column(name, BigInteger, nullable=True) for name in RESOURCE_COLUMNS),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    *(
        CheckConstraint(f"{name} IS NULL OR {name} >= 0", name=f"{name}_nonnegative")
        for name in RESOURCE_COLUMNS
    ),
)
Index(
    "ix_resource_snapshot_instance_observed_id",
    resource_snapshot.c.instance_id,
    resource_snapshot.c.observed_at.desc().nulls_last(),
    resource_snapshot.c.id.desc(),
)

opsi_item_event = Table(
    "opsi_item_event",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("imgid", String(128), nullable=False),
    Column("server", String(32), nullable=True),
    Column("zone", String(128), nullable=True),
    Column("zone_type", String(64), nullable=True),
    Column("zone_id", Integer, nullable=True),
    Column("hazard_level", Integer, nullable=True),
    Column("item_code", String(128), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("tag", String(64), nullable=True),
    Column("genre", String(64), nullable=False),
    Column("combat_count", Integer, nullable=True),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint("amount >= 0", name="amount_nonnegative"),
    CheckConstraint(
        "hazard_level IS NULL OR hazard_level BETWEEN 1 AND 6",
        name="hazard_level_range",
    ),
    CheckConstraint(
        "combat_count IS NULL OR combat_count >= 0",
        name="combat_count_nonnegative",
    ),
)
Index(
    "ix_opsi_item_instance_genre_observed",
    opsi_item_event.c.instance_id,
    opsi_item_event.c.genre,
    opsi_item_event.c.observed_at,
)
Index("ix_opsi_item_imgid", opsi_item_event.c.imgid)

cl1_ap_snapshot = Table(
    "cl1_ap_snapshot",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("ap", BigInteger, nullable=False),
    Column("ap_total", BigInteger, nullable=True),
    Column("asset", Numeric(18, 2), nullable=True),
    Column("yellow_coin", BigInteger, nullable=True),
    Column("distance", Integer, nullable=True),
    Column("source", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint(
        "ap >= 0 AND (ap_total IS NULL OR ap_total >= 0) "
        "AND (asset IS NULL OR asset >= 0) "
        "AND (yellow_coin IS NULL OR yellow_coin >= 0) "
        "AND (distance IS NULL OR distance >= 0)",
        name="values_nonnegative",
    ),
)
Index(
    "ix_cl1_ap_snapshot_instance_observed",
    cl1_ap_snapshot.c.instance_id,
    cl1_ap_snapshot.c.observed_at.desc().nulls_last(),
)

cl1_ap_purchase_event = Table(
    "cl1_ap_purchase_event",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("amount", BigInteger, nullable=False),
    Column("base_amount", BigInteger, nullable=False),
    Column("purchase_count", Integer, nullable=False),
    Column("source", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint(
        "amount >= 0 AND base_amount >= 0 AND purchase_count >= 0",
        name="values_nonnegative",
    ),
)
Index(
    "ix_cl1_ap_purchase_instance_observed",
    cl1_ap_purchase_event.c.instance_id,
    cl1_ap_purchase_event.c.observed_at.desc().nulls_last(),
)

cl1_currency_snapshot = Table(
    "cl1_currency_snapshot",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("currency_code", String(32), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("source", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint("amount >= 0", name="amount_nonnegative"),
)
Index(
    "ix_cl1_currency_instance_code_observed",
    cl1_currency_snapshot.c.instance_id,
    cl1_currency_snapshot.c.currency_code,
    cl1_currency_snapshot.c.observed_at.desc().nulls_last(),
)

commission_income_event = Table(
    "commission_income_event",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("commission_count", Integer, nullable=False),
    Column("source", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint("commission_count > 0", name="commission_count_positive"),
)
Index(
    "ix_commission_income_instance_observed",
    commission_income_event.c.instance_id,
    commission_income_event.c.observed_at.desc().nulls_last(),
)

commission_income_item = Table(
    "commission_income_item",
    metadata,
    Column(
        "event_id",
        Uuid,
        ForeignKey(f"{SCHEMA_NAME}.commission_income_event.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("item_code", String(128), nullable=False),
    Column("amount", BigInteger, nullable=False),
    PrimaryKeyConstraint("event_id", "item_code"),
    CheckConstraint("amount >= 0", name="amount_nonnegative"),
)

meow_timing_sample = Table(
    "meow_timing_sample",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("month", Date, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("sample_kind", String(16), nullable=False),
    Column("duration_seconds", Numeric(18, 6), nullable=False),
    Column("hazard_level", Integer, nullable=True),
    Column("source", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint("sample_kind IN ('battle', 'round')", name="sample_kind_allowed"),
    CheckConstraint("duration_seconds >= 0", name="duration_nonnegative"),
    CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month_first_day"),
    CheckConstraint(
        "hazard_level IS NULL OR hazard_level BETWEEN 1 AND 6",
        name="hazard_level_range",
    ),
)
Index(
    "ix_meow_timing_instance_month_kind",
    meow_timing_sample.c.instance_id,
    meow_timing_sample.c.month,
    meow_timing_sample.c.sample_kind,
)

meow_hazard_aggregate = Table(
    "meow_hazard_aggregate",
    metadata,
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("month", Date, nullable=False),
    Column("hazard_level", Integer, nullable=False),
    Column("raw_battle_count", BigInteger, nullable=False),
    Column("effective_rounds", Numeric(18, 6), nullable=False),
    Column("source", String(64), nullable=False),
    PrimaryKeyConstraint("instance_id", "month", "hazard_level"),
    CheckConstraint("hazard_level BETWEEN 1 AND 6", name="hazard_level_range"),
    CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month_first_day"),
    CheckConstraint(
        "raw_battle_count >= 0 AND effective_rounds >= 0",
        name="counts_nonnegative",
    ),
)

siren_research_device_stat = Table(
    "siren_research_device_stat",
    metadata,
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("month", Date, nullable=False),
    Column("source", String(16), nullable=False),
    Column("hazard_level", Integer, nullable=False, server_default="0"),
    Column("device_count", BigInteger, nullable=False),
    PrimaryKeyConstraint("instance_id", "month", "source", "hazard_level"),
    CheckConstraint("source IN ('cl1', 'meow')", name="source_allowed"),
    CheckConstraint("EXTRACT(DAY FROM month) = 1", name="month_first_day"),
    CheckConstraint("hazard_level BETWEEN 0 AND 6", name="hazard_level_range"),
    CheckConstraint("device_count >= 0", name="device_count_nonnegative"),
    # Агрегированные CL1-записи используют 0 как явный sentinel всех hazards.
    CheckConstraint(
        "(source = 'cl1' AND hazard_level = 0) OR "
        "(source = 'meow' AND hazard_level BETWEEN 1 AND 6)",
        name="source_hazard_consistent",
    ),
)

siren_research_device_event = Table(
    "siren_research_device_event",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("source", String(16), nullable=False),
    Column("hazard_level", Integer, nullable=True),
    UniqueConstraint("idempotency_key"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint("source IN ('cl1', 'meow')", name="source_allowed"),
    # У отдельных CL1-событий hazard отсутствует, а Meow требует значение 1..6.
    CheckConstraint(
        "(source = 'cl1' AND hazard_level IS NULL) OR "
        "(source = 'meow' AND hazard_level IS NOT NULL "
        "AND hazard_level BETWEEN 1 AND 6)",
        name="source_hazard_consistent",
    ),
)
Index(
    "ix_siren_device_event_instance_observed",
    siren_research_device_event.c.instance_id,
    siren_research_device_event.c.observed_at.desc().nulls_last(),
)

ap_notification_state = Table(
    "ap_notification_state",
    metadata,
    Column("instance_id", Uuid, _instance_fk(), primary_key=True),
    Column("last_ap", BigInteger, nullable=False),
    Column("notified_at", DateTime(timezone=True), nullable=True),
    Column("legacy_timestamp_text", String(64), nullable=True),
    Column("legacy_timezone", String(64), nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    CheckConstraint("last_ap >= 0", name="last_ap_nonnegative"),
    CheckConstraint("version >= 1", name="version_positive"),
)

resource_current_state = Table(
    "resource_current_state",
    metadata,
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("resource_code", String(32), nullable=False),
    Column("value", BigInteger, nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("instance_id", "resource_code"),
    CheckConstraint("value >= 0", name="value_nonnegative"),
    CheckConstraint("version >= 1", name="version_positive"),
)

formation_surface_fleet_scan_run = Table(
    "formation_surface_fleet_scan_run",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("source", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False, server_default="started"),
    Column("error_code", String(64), nullable=True),
    CheckConstraint(
        "status IN ('started', 'succeeded', 'partial', 'failed')",
        name="status_allowed",
    ),
    CheckConstraint(
        "(status = 'started' AND finished_at IS NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
        "(status IN ('partial', 'failed') AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL)",
        name="lifecycle_consistent",
    ),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name="time_ordered",
    ),
    UniqueConstraint("id", "instance_id", name="uq_formation_fleet_run_instance"),
)
Index(
    "ix_formation_surface_fleet_scan_run_instance_started",
    formation_surface_fleet_scan_run.c.instance_id,
    formation_surface_fleet_scan_run.c.started_at.desc(),
)

formation_surface_fleet_scan_request = Table(
    "formation_surface_fleet_scan_request",
    metadata,
    Column(
        "run_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_run.id",
            ondelete="CASCADE",
            name="fk_formation_fleet_request_run",
        ),
        nullable=False,
    ),
    Column("fleet_index", Integer, nullable=False),
    PrimaryKeyConstraint("run_id", "fleet_index"),
    CheckConstraint("fleet_index BETWEEN 1 AND 6", name="fleet_index_range"),
)

formation_surface_fleet_snapshot = Table(
    "formation_surface_fleet_snapshot",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "run_id",
        Uuid,
        nullable=False,
    ),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("fleet_index", Integer, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("complete", Boolean, nullable=False),
    Column("catalog_fingerprint", String(64), nullable=False),
    UniqueConstraint("idempotency_key"),
    UniqueConstraint("run_id", "fleet_index"),
    ForeignKeyConstraint(
        ("run_id", "fleet_index"),
        (
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_request.run_id",
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_request.fleet_index",
        ),
        ondelete="CASCADE",
        name="fk_formation_fleet_snapshot_request",
    ),
    ForeignKeyConstraint(
        ("run_id", "instance_id"),
        (
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_run.id",
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_run.instance_id",
        ),
        ondelete="CASCADE",
        name="fk_formation_fleet_snapshot_run_instance",
    ),
    CheckConstraint("fleet_index BETWEEN 1 AND 6", name="fleet_index_range"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_sha256"),
    CheckConstraint(
        "catalog_fingerprint ~ '^[0-9a-f]{64}$'",
        name="catalog_fingerprint_sha256",
    ),
)
Index(
    "ix_formation_surface_fleet_snapshot_instance_fleet_observed_id",
    formation_surface_fleet_snapshot.c.instance_id,
    formation_surface_fleet_snapshot.c.fleet_index,
    formation_surface_fleet_snapshot.c.observed_at.desc(),
    formation_surface_fleet_snapshot.c.id.desc(),
)

formation_surface_fleet_slot = Table(
    "formation_surface_fleet_slot",
    metadata,
    Column(
        "snapshot_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.formation_surface_fleet_snapshot.id",
            ondelete="CASCADE",
            name="fk_formation_fleet_slot_snapshot",
        ),
        nullable=False,
    ),
    Column("side", String(8), nullable=False),
    Column("position", Integer, nullable=False),
    Column("occupied", Boolean, nullable=False),
    Column("identity_status", String(16), nullable=True),
    Column("raw_name_ocr", String(256), nullable=True),
    Column("displayed_name", String(256), nullable=True),
    Column("canonical_identity_key", String(128), nullable=True),
    Column("canonical_name", String(256), nullable=True),
    Column("ship_form", String(16), nullable=True),
    PrimaryKeyConstraint("snapshot_id", "side", "position"),
    CheckConstraint("side IN ('main', 'vanguard')", name="side_allowed"),
    CheckConstraint("position BETWEEN 1 AND 3", name="position_range"),
    CheckConstraint(
        "identity_status IS NULL OR identity_status IN "
        "('unresolved', 'matched', 'ambiguous')",
        name="identity_status_allowed",
    ),
    CheckConstraint(
        "ship_form IS NULL OR ship_form IN ('base', 'retrofit')",
        name="ship_form_allowed",
    ),
    CheckConstraint(
        "(occupied = false AND identity_status IS NULL AND raw_name_ocr IS NULL "
        "AND displayed_name IS NULL AND canonical_identity_key IS NULL "
        "AND canonical_name IS NULL AND ship_form IS NULL) OR "
        "(occupied = true AND identity_status IN ('unresolved', 'ambiguous') "
        "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
        "AND canonical_identity_key IS NULL AND canonical_name IS NULL "
        "AND ship_form IS NULL) OR "
        "(occupied = true AND identity_status = 'matched' "
        "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
        "AND canonical_identity_key IS NOT NULL AND canonical_name IS NOT NULL "
        "AND ship_form IS NOT NULL AND ship_form IN ('base', 'retrofit'))",
        name="identity_consistent",
    ),
)

formation_surface_fleet_scan_command = Table(
    "formation_surface_fleet_scan_command",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "instance_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.app_instance.id",
            ondelete="RESTRICT",
            name="fk_fleet_scan_command_instance",
        ),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("result_run_id", Uuid, nullable=True),
    Column("error_code", String(64), nullable=True),
    ForeignKeyConstraint(
        ("result_run_id", "instance_id"),
        (
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_run.id",
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_run.instance_id",
        ),
        ondelete="RESTRICT",
        name="fk_formation_fleet_command_result_run_instance",
    ),
    CheckConstraint(
        "status IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
        name="status_allowed",
    ),
    CheckConstraint(
        "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
        "AND result_run_id IS NULL AND error_code IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
        "AND result_run_id IS NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND result_run_id IS NOT NULL AND error_code IS NULL) OR "
        "(status = 'partial' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND result_run_id IS NOT NULL AND error_code IS NOT NULL) OR "
        "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
        "AND error_code IS NOT NULL)",
        name="lifecycle_consistent",
    ),
    CheckConstraint(
        "started_at IS NULL OR started_at >= created_at",
        name="start_time_ordered",
    ),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name="finish_time_ordered",
    ),
)
Index(
    "uq_formation_surface_fleet_scan_command_active_instance",
    formation_surface_fleet_scan_command.c.instance_id,
    unique=True,
    postgresql_where=formation_surface_fleet_scan_command.c.status.in_(
        ("pending", "running")
    ),
)
Index(
    "ix_formation_surface_fleet_scan_command_instance_created",
    formation_surface_fleet_scan_command.c.instance_id,
    formation_surface_fleet_scan_command.c.created_at.desc(),
    formation_surface_fleet_scan_command.c.id.desc(),
)
Index(
    "ix_formation_surface_fleet_scan_command_pending_claim",
    formation_surface_fleet_scan_command.c.instance_id,
    formation_surface_fleet_scan_command.c.status,
    formation_surface_fleet_scan_command.c.created_at,
    formation_surface_fleet_scan_command.c.id,
)

formation_surface_fleet_scan_command_fleet = Table(
    "formation_surface_fleet_scan_command_fleet",
    metadata,
    Column(
        "command_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.formation_surface_fleet_scan_command.id",
            ondelete="CASCADE",
            name="fk_formation_fleet_command_fleet_command",
        ),
        nullable=False,
    ),
    Column("fleet_index", Integer, nullable=False),
    PrimaryKeyConstraint("command_id", "fleet_index"),
    CheckConstraint("fleet_index BETWEEN 1 AND 6", name="fleet_index_range"),
)
