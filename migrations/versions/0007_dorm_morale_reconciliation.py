"""Добавить Dorm scan provenance и recovery context без fake baseline.

Revision ID: 0007_dorm_morale_reconciliation
Revises: 0006_per_ship_morale_core
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_dorm_morale_reconciliation"
down_revision: str | None = "0006_per_ship_morale_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"
_SCAN = "dorm_morale_scan_run"
_SCAN_OBSERVATION = "dorm_morale_scan_observation"
_MORALE = "formation_surface_fleet_morale_observation"


def upgrade() -> None:
    op.create_table(
        _SCAN,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("catalog_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("floor_1_status", sa.String(length=16), nullable=False),
        sa.Column("floor_1_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("floor_1_error_code", sa.String(length=64), nullable=True),
        sa.Column("floor_2_status", sa.String(length=16), nullable=False),
        sa.Column("floor_2_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("floor_2_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "catalog_fingerprint IS NULL OR catalog_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_dorm_morale_scan_run_catalog_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "(floor_1_status = 'succeeded' AND floor_1_observed_at IS NOT NULL "
            "AND floor_1_error_code IS NULL) OR "
            "(floor_1_status = 'failed' AND floor_1_observed_at IS NULL "
            "AND floor_1_error_code IS NOT NULL)",
            name=op.f("ck_dorm_morale_scan_run_floor_1_consistent"),
        ),
        sa.CheckConstraint(
            "(floor_2_status = 'succeeded' AND floor_2_observed_at IS NOT NULL "
            "AND floor_2_error_code IS NULL) OR "
            "(floor_2_status = 'failed' AND floor_2_observed_at IS NULL "
            "AND floor_2_error_code IS NOT NULL)",
            name=op.f("ck_dorm_morale_scan_run_floor_2_consistent"),
        ),
        sa.CheckConstraint(
            "floor_1_status IN ('succeeded', 'failed') AND "
            "floor_2_status IN ('succeeded', 'failed')",
            name=op.f("ck_dorm_morale_scan_run_floor_status_allowed"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_dorm_morale_scan_run_digest_sha256"),
        ),
        sa.CheckConstraint(
            "btrim(source) <> ''",
            name=op.f("ck_dorm_morale_scan_run_source_not_blank"),
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'partial', 'failed')",
            name=op.f("ck_dorm_morale_scan_run_status_allowed"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND floor_1_status = 'succeeded' "
            "AND floor_2_status = 'succeeded') OR "
            "(status = 'partial' AND floor_1_status <> floor_2_status) OR "
            "(status = 'failed' AND floor_1_status = 'failed' "
            "AND floor_2_status = 'failed')",
            name=op.f("ck_dorm_morale_scan_run_status_consistent"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name=op.f("ck_dorm_morale_scan_run_time_order"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["azurpilot.app_instance.id"],
            name=op.f("fk_dorm_morale_scan_run_instance_id_app_instance"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dorm_morale_scan_run")),
        sa.UniqueConstraint(
            "id", "instance_id", name=op.f("uq_dorm_morale_scan_run_provenance")
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_dorm_morale_scan_run_idempotency_key"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_dorm_morale_scan_run_latest",
        _SCAN,
        ["instance_id", sa.text("finished_at DESC"), sa.text("id DESC")],
        unique=False,
        schema=_SCHEMA,
    )
    op.create_table(
        _SCAN_OBSERVATION,
        sa.Column("scan_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("floor", sa.String(length=2), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("raw_name_ocr", sa.String(length=256), nullable=False),
        sa.Column("displayed_name", sa.String(length=256), nullable=False),
        sa.Column("identity_status", sa.String(length=16), nullable=False),
        sa.Column("canonical_identity_key", sa.String(length=128), nullable=True),
        sa.Column("canonical_name", sa.String(length=256), nullable=True),
        sa.Column("ship_form", sa.String(length=16), nullable=True),
        sa.Column("morale", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column(
            "recovery_per_hour", sa.Numeric(precision=10, scale=6), nullable=False
        ),
        sa.CheckConstraint(
            "floor IN ('1F', '2F')",
            name=op.f("ck_dorm_morale_scan_observation_floor_allowed"),
        ),
        sa.CheckConstraint(
            "(identity_status = 'matched' AND canonical_identity_key IS NOT NULL "
            "AND canonical_name IS NOT NULL) OR "
            "(identity_status IN ('unresolved', 'ambiguous') "
            "AND canonical_identity_key IS NULL AND canonical_name IS NULL "
            "AND ship_form IS NULL)",
            name=op.f("ck_dorm_morale_scan_observation_identity_consistent"),
        ),
        sa.CheckConstraint(
            "identity_status IN ('unresolved', 'matched', 'ambiguous')",
            name=op.f("ck_dorm_morale_scan_observation_identity_status_allowed"),
        ),
        sa.CheckConstraint(
            "morale BETWEEN 0 AND 150",
            name=op.f("ck_dorm_morale_scan_observation_morale_range"),
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 5",
            name=op.f("ck_dorm_morale_scan_observation_ordinal_range"),
        ),
        sa.CheckConstraint(
            "recovery_per_hour BETWEEN 0 AND 1500",
            name=op.f("ck_dorm_morale_scan_observation_recovery_rate_range"),
        ),
        sa.CheckConstraint(
            "ship_form IS NULL OR ship_form IN ('base', 'retrofit')",
            name=op.f("ck_dorm_morale_scan_observation_ship_form_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["scan_id", "instance_id"],
            [
                "azurpilot.dorm_morale_scan_run.id",
                "azurpilot.dorm_morale_scan_run.instance_id",
            ],
            name=op.f("fk_dorm_morale_observation_scan_instance"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "scan_id",
            "floor",
            "ordinal",
            name=op.f("pk_dorm_morale_scan_observation"),
        ),
        schema=_SCHEMA,
    )

    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_baseline_range"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_exact"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.alter_column(
        _MORALE,
        "baseline",
        existing_type=sa.Numeric(precision=9, scale=6),
        nullable=True,
        schema=_SCHEMA,
    )
    op.add_column(
        _MORALE,
        sa.Column(
            "location",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        _MORALE,
        sa.Column("dorm_scan_id", sa.Uuid(), nullable=True),
        schema=_SCHEMA,
    )
    op.create_foreign_key(
        op.f("fk_morale_observation_dorm_scan_instance"),
        _MORALE,
        _SCAN,
        ["dorm_scan_id", "instance_id"],
        ["id", "instance_id"],
        source_schema=_SCHEMA,
        referent_schema=_SCHEMA,
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_baseline_range"),
        _MORALE,
        "baseline IS NULL OR baseline BETWEEN 0 AND 150",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_allowed"),
        _MORALE,
        "knowledge IN ('exact', 'unknown')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_value"),
        _MORALE,
        "(knowledge = 'exact' AND baseline IS NOT NULL) OR "
        "(knowledge = 'unknown' AND baseline IS NULL)",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_location_allowed"),
        _MORALE,
        "location IN ('unknown', 'dorm_floor_1', 'dorm_floor_2', 'outside_dorm')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_dorm_provenance"),
        _MORALE,
        "(location = 'unknown' AND dorm_scan_id IS NULL) OR "
        "(location <> 'unknown' AND dorm_scan_id IS NOT NULL)",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    connection = op.get_bind()
    stage_2_rows = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM azurpilot.formation_surface_fleet_morale_observation "
            "WHERE knowledge <> 'exact' OR dorm_scan_id IS NOT NULL)"
        )
    ).scalar_one()
    if stage_2_rows:
        raise RuntimeError(
            "Downgrade 0007 запрещён: присутствуют Dorm/recovery observations."
        )

    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_dorm_provenance"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_location_allowed"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_value"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_allowed"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_baseline_range"),
        _MORALE,
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_morale_observation_dorm_scan_instance"),
        _MORALE,
        schema=_SCHEMA,
        type_="foreignkey",
    )
    op.drop_column(_MORALE, "dorm_scan_id", schema=_SCHEMA)
    op.drop_column(_MORALE, "location", schema=_SCHEMA)
    op.alter_column(
        _MORALE,
        "baseline",
        existing_type=sa.Numeric(precision=9, scale=6),
        nullable=False,
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_baseline_range"),
        _MORALE,
        "baseline BETWEEN 0 AND 150",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        op.f("ck_formation_surface_fleet_morale_observation_knowledge_exact"),
        _MORALE,
        "knowledge = 'exact'",
        schema=_SCHEMA,
    )
    op.drop_table(_SCAN_OBSERVATION, schema=_SCHEMA)
    op.drop_index(
        "ix_dorm_morale_scan_run_latest",
        table_name=_SCAN,
        schema=_SCHEMA,
    )
    op.drop_table(_SCAN, schema=_SCHEMA)
