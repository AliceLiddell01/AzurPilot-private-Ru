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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from module.application.resource_fields import RESOURCE_FIELDS
from module.application.storage_models import MonthlyMetric

SCHEMA_NAME = "azurpilot"
EXPECTED_ALEMBIC_HEAD = "0010_notification_agent_ack"

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
    UniqueConstraint(
        "id",
        "instance_id",
        "fleet_index",
        name="uq_formation_fleet_snapshot_provenance",
    ),
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
    UniqueConstraint(
        "snapshot_id",
        "side",
        "position",
        "canonical_identity_key",
        "ship_form",
        name="uq_formation_fleet_slot_morale_identity",
    ),
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

dorm_morale_scan_run = Table(
    "dorm_morale_scan_run",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("instance_id", Uuid, _instance_fk(), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(64), nullable=False),
    Column("catalog_fingerprint", String(64), nullable=True),
    Column("floor_1_status", String(16), nullable=False),
    Column("floor_1_observed_at", DateTime(timezone=True), nullable=True),
    Column("floor_1_error_code", String(64), nullable=True),
    Column("floor_2_status", String(16), nullable=False),
    Column("floor_2_observed_at", DateTime(timezone=True), nullable=True),
    Column("floor_2_error_code", String(64), nullable=True),
    UniqueConstraint("instance_id", "idempotency_key"),
    UniqueConstraint("id", "instance_id", name="uq_dorm_morale_scan_run_provenance"),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="digest_sha256"),
    CheckConstraint("finished_at >= started_at", name="time_order"),
    CheckConstraint(
        "status IN ('succeeded', 'partial', 'failed')", name="status_allowed"
    ),
    CheckConstraint(
        "catalog_fingerprint IS NULL OR catalog_fingerprint ~ '^[0-9a-f]{64}$'",
        name="catalog_fingerprint_sha256",
    ),
    CheckConstraint(
        "floor_1_status IN ('succeeded', 'failed') AND "
        "floor_2_status IN ('succeeded', 'failed')",
        name="floor_status_allowed",
    ),
    CheckConstraint(
        "(floor_1_status = 'succeeded' AND floor_1_observed_at IS NOT NULL "
        "AND floor_1_error_code IS NULL) OR "
        "(floor_1_status = 'failed' AND floor_1_observed_at IS NULL "
        "AND floor_1_error_code IS NOT NULL)",
        name="floor_1_consistent",
    ),
    CheckConstraint(
        "(floor_2_status = 'succeeded' AND floor_2_observed_at IS NOT NULL "
        "AND floor_2_error_code IS NULL) OR "
        "(floor_2_status = 'failed' AND floor_2_observed_at IS NULL "
        "AND floor_2_error_code IS NOT NULL)",
        name="floor_2_consistent",
    ),
    CheckConstraint(
        "(status = 'succeeded' AND floor_1_status = 'succeeded' "
        "AND floor_2_status = 'succeeded') OR "
        "(status = 'partial' AND floor_1_status <> floor_2_status) OR "
        "(status = 'failed' AND floor_1_status = 'failed' "
        "AND floor_2_status = 'failed')",
        name="status_consistent",
    ),
    CheckConstraint("btrim(source) <> ''", name="source_not_blank"),
)
Index(
    "ix_dorm_morale_scan_run_latest",
    dorm_morale_scan_run.c.instance_id,
    dorm_morale_scan_run.c.finished_at.desc(),
    dorm_morale_scan_run.c.id.desc(),
)

dorm_morale_scan_observation = Table(
    "dorm_morale_scan_observation",
    metadata,
    Column("scan_id", Uuid, nullable=False),
    Column("instance_id", Uuid, nullable=False),
    Column("floor", String(2), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("raw_name_ocr", String(256), nullable=False),
    Column("displayed_name", String(256), nullable=False),
    Column("identity_status", String(16), nullable=False),
    Column("canonical_identity_key", String(128), nullable=True),
    Column("canonical_name", String(256), nullable=True),
    Column("ship_form", String(16), nullable=True),
    Column("morale", Numeric(9, 6), nullable=False),
    Column("recovery_per_hour", Numeric(10, 6), nullable=False),
    PrimaryKeyConstraint("scan_id", "floor", "ordinal"),
    ForeignKeyConstraint(
        ("scan_id", "instance_id"),
        (
            f"{SCHEMA_NAME}.dorm_morale_scan_run.id",
            f"{SCHEMA_NAME}.dorm_morale_scan_run.instance_id",
        ),
        ondelete="CASCADE",
        name="fk_dorm_morale_observation_scan_instance",
    ),
    CheckConstraint("floor IN ('1F', '2F')", name="floor_allowed"),
    CheckConstraint("ordinal BETWEEN 1 AND 5", name="ordinal_range"),
    CheckConstraint(
        "identity_status IN ('unresolved', 'matched', 'ambiguous')",
        name="identity_status_allowed",
    ),
    CheckConstraint(
        "ship_form IS NULL OR ship_form IN ('base', 'retrofit')",
        name="ship_form_allowed",
    ),
    CheckConstraint(
        "(identity_status = 'matched' AND canonical_identity_key IS NOT NULL "
        "AND canonical_name IS NOT NULL) OR "
        "(identity_status IN ('unresolved', 'ambiguous') "
        "AND canonical_identity_key IS NULL AND canonical_name IS NULL "
        "AND ship_form IS NULL)",
        name="identity_consistent",
    ),
    CheckConstraint("morale BETWEEN 0 AND 150", name="morale_range"),
    CheckConstraint("recovery_per_hour BETWEEN 0 AND 1500", name="recovery_rate_range"),
)

formation_surface_fleet_morale_observation = Table(
    "formation_surface_fleet_morale_observation",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("formation_snapshot_id", Uuid, nullable=False),
    Column("instance_id", Uuid, nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("fleet_index", Integer, nullable=False),
    Column("side", String(8), nullable=False),
    Column("position", Integer, nullable=False),
    Column("canonical_identity_key", String(128), nullable=False),
    Column("ship_form", String(16), nullable=False),
    Column("baseline", Numeric(9, 6), nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("recovery_per_hour", Numeric(10, 6), nullable=False),
    Column("recovery_ceiling", Numeric(9, 6), nullable=False),
    Column("source", String(64), nullable=False),
    Column("recovery_source", String(64), nullable=False),
    Column("knowledge", String(16), nullable=False),
    Column("location", String(32), nullable=False, server_default="unknown"),
    Column("dorm_scan_id", Uuid, nullable=True),
    UniqueConstraint("idempotency_key"),
    ForeignKeyConstraint(
        ("formation_snapshot_id", "instance_id", "fleet_index"),
        (
            f"{SCHEMA_NAME}.formation_surface_fleet_snapshot.id",
            f"{SCHEMA_NAME}.formation_surface_fleet_snapshot.instance_id",
            f"{SCHEMA_NAME}.formation_surface_fleet_snapshot.fleet_index",
        ),
        ondelete="RESTRICT",
        name="fk_morale_observation_snapshot_provenance",
    ),
    ForeignKeyConstraint(
        (
            "formation_snapshot_id",
            "side",
            "position",
            "canonical_identity_key",
            "ship_form",
        ),
        (
            f"{SCHEMA_NAME}.formation_surface_fleet_slot.snapshot_id",
            f"{SCHEMA_NAME}.formation_surface_fleet_slot.side",
            f"{SCHEMA_NAME}.formation_surface_fleet_slot.position",
            f"{SCHEMA_NAME}.formation_surface_fleet_slot.canonical_identity_key",
            f"{SCHEMA_NAME}.formation_surface_fleet_slot.ship_form",
        ),
        ondelete="RESTRICT",
        name="fk_morale_observation_slot_identity",
    ),
    ForeignKeyConstraint(
        ("dorm_scan_id", "instance_id"),
        (
            f"{SCHEMA_NAME}.dorm_morale_scan_run.id",
            f"{SCHEMA_NAME}.dorm_morale_scan_run.instance_id",
        ),
        ondelete="RESTRICT",
        name="fk_morale_observation_dorm_scan_instance",
    ),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="digest_sha256"),
    CheckConstraint("fleet_index BETWEEN 1 AND 6", name="fleet_range"),
    CheckConstraint("side IN ('main', 'vanguard')", name="side_allowed"),
    CheckConstraint("position BETWEEN 1 AND 3", name="position_range"),
    CheckConstraint("ship_form IN ('base', 'retrofit')", name="ship_form_allowed"),
    CheckConstraint(
        "baseline IS NULL OR baseline BETWEEN 0 AND 150", name="baseline_range"
    ),
    CheckConstraint("recovery_per_hour BETWEEN 0 AND 1500", name="rate_range"),
    CheckConstraint("recovery_ceiling BETWEEN 0 AND 150", name="ceiling_range"),
    CheckConstraint("btrim(source) <> ''", name="source_not_blank"),
    CheckConstraint("btrim(recovery_source) <> ''", name="recover_not_blank"),
    CheckConstraint("knowledge IN ('exact', 'unknown')", name="knowledge_allowed"),
    CheckConstraint(
        "(knowledge = 'exact' AND baseline IS NOT NULL) OR "
        "(knowledge = 'unknown' AND baseline IS NULL)",
        name="knowledge_value",
    ),
    CheckConstraint(
        "location IN ('unknown', 'dorm_floor_1', 'dorm_floor_2', 'outside_dorm')",
        name="location_allowed",
    ),
    CheckConstraint(
        "(location = 'unknown' AND dorm_scan_id IS NULL) OR "
        "(location <> 'unknown' AND dorm_scan_id IS NOT NULL)",
        name="dorm_provenance",
    ),
)
Index(
    "ix_formation_fleet_morale_latest",
    formation_surface_fleet_morale_observation.c.instance_id,
    formation_surface_fleet_morale_observation.c.fleet_index,
    formation_surface_fleet_morale_observation.c.side,
    formation_surface_fleet_morale_observation.c.position,
    formation_surface_fleet_morale_observation.c.observed_at.desc(),
    formation_surface_fleet_morale_observation.c.id.desc(),
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


notification_event = Table(
    "notification_event",
    metadata,
    Column("row_id", Uuid, primary_key=True),
    Column("id", Uuid, nullable=False),
    Column("source", String(64), nullable=False),
    Column("type", String(128), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("profile_id", String(128), nullable=False),
    Column("runtime_instance_id", String(128), nullable=True),
    Column("subject_kind", String(32), nullable=True),
    Column("subject_id", String(128), nullable=True),
    Column("severity", String(16), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("persisted_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("profile_sequence", BigInteger, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("dedup_key", String(128), nullable=True),
    Column("correlation", JSONB(none_as_null=True), nullable=True),
    Column("sensitivity", String(16), nullable=False),
    UniqueConstraint(
        "profile_id",
        "profile_sequence",
        name="uq_notification_event_profile_sequence",
    ),
    UniqueConstraint(
        "source",
        "id",
        name="uq_notification_event_occurrence",
    ),
    CheckConstraint("schema_version > 0", name="schema_version_positive"),
    CheckConstraint("profile_sequence > 0", name="profile_sequence_positive"),
    CheckConstraint("btrim(source) <> ''", name="source_not_blank"),
    CheckConstraint("btrim(type) <> ''", name="type_not_blank"),
    CheckConstraint("btrim(profile_id) <> ''", name="profile_not_blank"),
    CheckConstraint(
        "dedup_key IS NULL OR btrim(dedup_key) <> ''",
        name="dedup_key_not_blank",
    ),
    CheckConstraint(
        "severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')",
        name="severity_allowed",
    ),
    CheckConstraint(
        "sensitivity IN ('NORMAL', 'SENSITIVE')",
        name="sensitivity_allowed",
    ),
    CheckConstraint("payload_digest ~ '^[0-9a-f]{64}$'", name="digest_sha256"),
    CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
    CheckConstraint(
        "correlation IS NULL OR jsonb_typeof(correlation) = 'object'",
        name="correlation_object",
    ),
    CheckConstraint(
        "(subject_kind IS NULL AND subject_id IS NULL) OR "
        "(subject_kind IS NOT NULL AND subject_id IS NOT NULL)",
        name="subject_consistent",
    ),
)
Index(
    "ix_notification_event_logical_identity",
    notification_event.c.source,
    notification_event.c.profile_id,
    notification_event.c.type,
    notification_event.c.dedup_key,
    unique=True,
    postgresql_where=text("dedup_key IS NOT NULL"),
)
Index(
    "ix_notification_event_profile_history",
    notification_event.c.profile_id,
    notification_event.c.profile_sequence,
    notification_event.c.id,
)

notification_policy_decision = Table(
    "notification_policy_decision",
    metadata,
    Column(
        "event_row_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.notification_event.row_id",
            ondelete="CASCADE",
            name="fk_notification_policy_decision_event",
        ),
        primary_key=True,
    ),
    Column("state", String(16), nullable=False),
    Column("matched_rule_id", String(128), nullable=True),
    Column("policy_version", Integer, nullable=False),
    Column("reason", String(128), nullable=False),
    Column("channel_instance_ids", JSONB, nullable=False),
    Column("policy_snapshot", JSONB, nullable=False),
    Column("policy_snapshot_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    CheckConstraint(
        "state IN ('ROUTED', 'SUPPRESSED')", name="state_allowed"
    ),
    CheckConstraint("policy_version > 0", name="policy_version_positive"),
    CheckConstraint("btrim(reason) <> ''", name="reason_not_blank"),
    CheckConstraint(
        "jsonb_typeof(channel_instance_ids) = 'array' AND "
        "jsonb_array_length(channel_instance_ids) <= 32",
        name="channels_bounded",
    ),
    CheckConstraint(
        "state <> 'SUPPRESSED' OR jsonb_array_length(channel_instance_ids) = 0",
        name="suppressed_channels_empty",
    ),
    CheckConstraint(
        "jsonb_typeof(policy_snapshot) = 'object'",
        name="snapshot_object",
    ),
    CheckConstraint(
        "policy_snapshot_hash ~ '^[0-9a-f]{64}$'", name="snapshot_hash_sha256"
    ),
)

notification_delivery = Table(
    "notification_delivery",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "event_row_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.notification_event.row_id",
            ondelete="CASCADE",
            name="fk_notification_delivery_event",
        ),
        nullable=False,
    ),
    Column("channel_instance_id", String(128), nullable=False),
    Column("channel_type", String(64), nullable=False),
    Column("state", String(32), nullable=False, server_default="PENDING"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("deadline_at", DateTime(timezone=True), nullable=True),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_token", Uuid, nullable=True),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("last_safe_error_code", String(64), nullable=True),
    Column("rendered_snapshot", JSONB, nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint(
        "event_row_id",
        "channel_instance_id",
        name="uq_notification_delivery_event_channel",
    ),
    UniqueConstraint("idempotency_key", name="uq_notification_delivery_idempotency"),
    CheckConstraint(
        "state IN ('PENDING', 'IN_FLIGHT', 'RETRY_WAIT', 'FAILED', "
        "'PROVIDER_ACCEPTED', 'AWAITING_AGENT_ACK', 'DELIVERED', 'SUPPRESSED')",
        name="state_allowed",
    ),
    CheckConstraint("priority BETWEEN -100 AND 100", name="priority_range"),
    CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    CheckConstraint(
        "jsonb_typeof(rendered_snapshot) = 'object'", name="snapshot_object"
    ),
    CheckConstraint(
        "(state = 'IN_FLIGHT' AND lease_owner IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
        "state <> 'IN_FLIGHT'",
        name="in_flight_lease_consistent",
    ),
    CheckConstraint(
        "state <> 'AWAITING_AGENT_ACK' OR "
        "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
        name="awaiting_ack_lease_consistent",
    ),
    CheckConstraint(
        "last_safe_error_code IS NULL OR "
        "last_safe_error_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'",
        name="last_safe_error_code_format",
    ),
)
Index(
    "ix_notification_delivery_claim_due",
    notification_delivery.c.priority.desc(),
    notification_delivery.c.next_attempt_at,
    notification_delivery.c.id,
    postgresql_where=text("state IN ('PENDING', 'RETRY_WAIT')"),
)
Index(
    "ix_notification_delivery_deadline_expiry",
    notification_delivery.c.deadline_at,
    notification_delivery.c.id,
    postgresql_where=text(
        "state IN ('PENDING', 'RETRY_WAIT') AND deadline_at IS NOT NULL"
    ),
)
Index(
    "ix_notification_delivery_event",
    notification_delivery.c.event_row_id,
    notification_delivery.c.id,
)
Index(
    "ix_notification_delivery_lease_expiry",
    notification_delivery.c.lease_until,
    notification_delivery.c.id,
    postgresql_where=text("lease_until IS NOT NULL"),
)

notification_delivery_attempt = Table(
    "notification_delivery_attempt",
    metadata,
    Column(
        "delivery_id",
        Uuid,
        ForeignKey(
            f"{SCHEMA_NAME}.notification_delivery.id",
            ondelete="CASCADE",
            name="fk_notification_delivery_attempt_delivery",
        ),
        nullable=False,
    ),
    Column("attempt_ordinal", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("result_class", String(32), nullable=True),
    Column("safe_error_code", String(64), nullable=True),
    Column("safe_error_summary", String(256), nullable=True),
    Column("retry_after_seconds", Integer, nullable=True),
    Column("provider_message_id", String(128), nullable=True),
    Column("lease_token", Uuid, nullable=True),
    Column("trace_id", String(32), nullable=True),
    Column("span_id", String(16), nullable=True),
    PrimaryKeyConstraint(
        "delivery_id", "attempt_ordinal", name="pk_notification_delivery_attempt"
    ),
    CheckConstraint("attempt_ordinal > 0", name="attempt_ordinal_positive"),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name="finished_time_ordered",
    ),
    CheckConstraint(
        "result_class IS NULL OR result_class IN ('DELIVERED', 'PROVIDER_ACCEPTED', "
        "'TRANSIENT_FAILURE', 'PERMANENT_FAILURE', 'UNAVAILABLE', 'SUPPRESSED')",
        name="result_class_allowed",
    ),
    CheckConstraint(
        "(result_class IS NULL) = (finished_at IS NULL)",
        name="result_class_matches_finished",
    ),
    CheckConstraint(
        "result_class IS NOT NULL OR ("
        "safe_error_code IS NULL AND safe_error_summary IS NULL AND "
        "retry_after_seconds IS NULL AND provider_message_id IS NULL)",
        name="open_result_fields_null",
    ),
    CheckConstraint(
        "retry_after_seconds IS NULL OR retry_after_seconds BETWEEN 0 AND 3600",
        name="retry_after_range",
    ),
    CheckConstraint(
        "safe_error_code IS NULL OR "
        "safe_error_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'",
        name="safe_error_code_format",
    ),
    CheckConstraint(
        "safe_error_summary IS NULL OR safe_error_summary !~ '[[:cntrl:]]'",
        name="safe_error_summary_no_control",
    ),
    CheckConstraint(
        "trace_id IS NULL OR trace_id ~ '^[0-9a-f]{16}$|^[0-9a-f]{32}$'",
        name="trace_id_format",
    ),
    CheckConstraint(
        "span_id IS NULL OR span_id ~ '^[0-9a-f]{16}$'",
        name="span_id_format",
    ),
)

notification_agent_ack = Table(
    "notification_agent_ack",
    metadata,
    Column(
        "delivery_id",
        Uuid,
        nullable=False,
    ),
    Column("attempt_ordinal", Integer, nullable=False),
    Column("event_id", Uuid, nullable=False),
    Column("event_source", String(64), nullable=False),
    Column("profile_id", String(128), nullable=False),
    Column("agent_id", String(128), nullable=False),
    Column("lease_token", Uuid, nullable=False),
    Column("session_epoch", Uuid, nullable=False),
    Column("payload_digest", String(64), nullable=False),
    Column("acknowledged_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint(
        "delivery_id", "attempt_ordinal", name="pk_notification_agent_ack"
    ),
    ForeignKeyConstraint(
        ["delivery_id", "attempt_ordinal"],
        [
            f"{SCHEMA_NAME}.notification_delivery_attempt.delivery_id",
            f"{SCHEMA_NAME}.notification_delivery_attempt.attempt_ordinal",
        ],
        ondelete="CASCADE",
        name="fk_notification_agent_ack_attempt",
    ),
    CheckConstraint("attempt_ordinal > 0", name="attempt_ordinal_positive"),
    CheckConstraint("btrim(event_source) <> ''", name="event_source_not_blank"),
    CheckConstraint("btrim(profile_id) <> ''", name="profile_not_blank"),
    CheckConstraint("btrim(agent_id) <> ''", name="agent_id_not_blank"),
    CheckConstraint("lease_token = session_epoch", name="session_epoch_matches_lease"),
    CheckConstraint(
        "payload_digest ~ '^[0-9a-f]{64}$'", name="payload_digest_format"
    ),
)
Index(
    "ix_notification_agent_ack_event",
    notification_agent_ack.c.event_id,
    notification_agent_ack.c.event_source,
)

notification_profile_sequence = Table(
    "notification_profile_sequence",
    metadata,
    Column("profile_id", String(128), primary_key=True),
    Column("next_sequence", BigInteger, nullable=False, server_default="1"),
    CheckConstraint("btrim(profile_id) <> ''", name="profile_not_blank"),
    CheckConstraint("next_sequence > 0", name="next_sequence_positive"),
)
