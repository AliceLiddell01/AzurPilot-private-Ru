"""Добавить append-only Formation Surface Fleet State.

Revision ID: 0003_fleet_state_core
Revises: 0002_migration_shapes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_fleet_state_core"
down_revision: str | None = "0002_migration_shapes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "formation_surface_fleet_scan_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="started",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "(status = 'started' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('partial', 'failed') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name=op.f("ck_formation_surface_fleet_scan_run_lifecycle_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_formation_surface_fleet_scan_run_status_allowed"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_formation_surface_fleet_scan_run_time_ordered"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["azurpilot.app_instance.id"],
            name=op.f(
                "fk_formation_surface_fleet_scan_run_instance_id_app_instance"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_formation_surface_fleet_scan_run"),
        ),
        sa.UniqueConstraint(
            "id",
            "instance_id",
            name="uq_formation_fleet_run_instance",
        ),
        schema="azurpilot",
    )
    op.create_index(
        "ix_formation_surface_fleet_scan_run_instance_started",
        "formation_surface_fleet_scan_run",
        ["instance_id", sa.literal_column("started_at DESC")],
        unique=False,
        schema="azurpilot",
    )
    op.create_table(
        "formation_surface_fleet_scan_request",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("fleet_index", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "fleet_index BETWEEN 1 AND 6",
            name=op.f("ck_formation_surface_fleet_scan_request_fleet_index_range"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["azurpilot.formation_surface_fleet_scan_run.id"],
            name="fk_formation_fleet_request_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "fleet_index",
            name=op.f("pk_formation_surface_fleet_scan_request"),
        ),
        schema="azurpilot",
    )
    op.create_table(
        "formation_surface_fleet_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("fleet_index", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("catalog_fingerprint", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "catalog_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f(
                "ck_formation_surface_fleet_snapshot_catalog_fingerprint_sha256"
            ),
        ),
        sa.CheckConstraint(
            "fleet_index BETWEEN 1 AND 6",
            name=op.f("ck_formation_surface_fleet_snapshot_fleet_index_range"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_formation_surface_fleet_snapshot_payload_digest_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["azurpilot.app_instance.id"],
            name=op.f(
                "fk_formation_surface_fleet_snapshot_instance_id_app_instance"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "fleet_index"],
            [
                "azurpilot.formation_surface_fleet_scan_request.run_id",
                "azurpilot.formation_surface_fleet_scan_request.fleet_index",
            ],
            name="fk_formation_fleet_snapshot_request",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "instance_id"],
            [
                "azurpilot.formation_surface_fleet_scan_run.id",
                "azurpilot.formation_surface_fleet_scan_run.instance_id",
            ],
            name="fk_formation_fleet_snapshot_run_instance",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_formation_surface_fleet_snapshot"),
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_formation_surface_fleet_snapshot_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "fleet_index",
            name=op.f("uq_formation_surface_fleet_snapshot_run_id_fleet_index"),
        ),
        schema="azurpilot",
    )
    op.create_index(
        "ix_formation_surface_fleet_snapshot_instance_fleet_observed_id",
        "formation_surface_fleet_snapshot",
        [
            "instance_id",
            "fleet_index",
            sa.literal_column("observed_at DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
        schema="azurpilot",
    )
    op.create_table(
        "formation_surface_fleet_slot",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("occupied", sa.Boolean(), nullable=False),
        sa.Column("identity_status", sa.String(length=16), nullable=True),
        sa.Column("raw_name_ocr", sa.String(length=256), nullable=True),
        sa.Column("displayed_name", sa.String(length=256), nullable=True),
        sa.Column("canonical_identity_key", sa.String(length=128), nullable=True),
        sa.Column("canonical_name", sa.String(length=256), nullable=True),
        sa.CheckConstraint(
            "(occupied = false AND identity_status IS NULL AND raw_name_ocr IS NULL "
            "AND displayed_name IS NULL AND canonical_identity_key IS NULL "
            "AND canonical_name IS NULL) OR "
            "(occupied = true AND identity_status IN ('unresolved', 'ambiguous') "
            "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
            "AND canonical_identity_key IS NULL AND canonical_name IS NULL) OR "
            "(occupied = true AND identity_status = 'matched' "
            "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
            "AND canonical_identity_key IS NOT NULL AND canonical_name IS NOT NULL)",
            name=op.f("ck_formation_surface_fleet_slot_identity_consistent"),
        ),
        sa.CheckConstraint(
            "identity_status IS NULL OR identity_status IN "
            "('unresolved', 'matched', 'ambiguous')",
            name=op.f("ck_formation_surface_fleet_slot_identity_status_allowed"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 3",
            name=op.f("ck_formation_surface_fleet_slot_position_range"),
        ),
        sa.CheckConstraint(
            "side IN ('main', 'vanguard')",
            name=op.f("ck_formation_surface_fleet_slot_side_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["azurpilot.formation_surface_fleet_snapshot.id"],
            name="fk_formation_fleet_slot_snapshot",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            "side",
            "position",
            name=op.f("pk_formation_surface_fleet_slot"),
        ),
        schema="azurpilot",
    )


def downgrade() -> None:
    op.drop_table("formation_surface_fleet_slot", schema="azurpilot")
    op.drop_index(
        "ix_formation_surface_fleet_snapshot_instance_fleet_observed_id",
        table_name="formation_surface_fleet_snapshot",
        schema="azurpilot",
    )
    op.drop_table("formation_surface_fleet_snapshot", schema="azurpilot")
    op.drop_table("formation_surface_fleet_scan_request", schema="azurpilot")
    op.drop_index(
        "ix_formation_surface_fleet_scan_run_instance_started",
        table_name="formation_surface_fleet_scan_run",
        schema="azurpilot",
    )
    op.drop_table("formation_surface_fleet_scan_run", schema="azurpilot")
