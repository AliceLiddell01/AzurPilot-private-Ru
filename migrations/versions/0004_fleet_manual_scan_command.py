"""Добавить устойчивую очередь команд ручного сканирования Fleet.

Revision ID: 0004_fleet_manual_scan_command
Revises: 0003_fleet_state_core
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_fleet_manual_scan_command"
down_revision: str | None = "0003_fleet_state_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "formation_surface_fleet_scan_command",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("result_run_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_formation_surface_fleet_scan_command_finish_time_ordered"),
        ),
        sa.CheckConstraint(
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
            name=op.f("ck_formation_surface_fleet_scan_command_lifecycle_consistent"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at >= created_at",
            name=op.f("ck_formation_surface_fleet_scan_command_start_time_ordered"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_formation_surface_fleet_scan_command_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["azurpilot.app_instance.id"],
            name="fk_fleet_scan_command_instance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_run_id", "instance_id"],
            [
                "azurpilot.formation_surface_fleet_scan_run.id",
                "azurpilot.formation_surface_fleet_scan_run.instance_id",
            ],
            name="fk_formation_fleet_command_result_run_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_formation_surface_fleet_scan_command"),
        ),
        schema="azurpilot",
    )
    op.create_index(
        "uq_formation_surface_fleet_scan_command_active_instance",
        "formation_surface_fleet_scan_command",
        ["instance_id"],
        unique=True,
        schema="azurpilot",
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_formation_surface_fleet_scan_command_instance_created",
        "formation_surface_fleet_scan_command",
        ["instance_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        schema="azurpilot",
    )
    op.create_index(
        "ix_formation_surface_fleet_scan_command_pending_claim",
        "formation_surface_fleet_scan_command",
        ["instance_id", "status", "created_at", "id"],
        unique=False,
        schema="azurpilot",
    )
    op.create_table(
        "formation_surface_fleet_scan_command_fleet",
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("fleet_index", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "fleet_index BETWEEN 1 AND 6",
            name=op.f(
                "ck_formation_surface_fleet_scan_command_fleet_fleet_index_range"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["azurpilot.formation_surface_fleet_scan_command.id"],
            name="fk_formation_fleet_command_fleet_command",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "command_id",
            "fleet_index",
            name=op.f("pk_formation_surface_fleet_scan_command_fleet"),
        ),
        schema="azurpilot",
    )


def downgrade() -> None:
    op.drop_table(
        "formation_surface_fleet_scan_command_fleet",
        schema="azurpilot",
    )
    op.drop_index(
        "ix_formation_surface_fleet_scan_command_pending_claim",
        table_name="formation_surface_fleet_scan_command",
        schema="azurpilot",
    )
    op.drop_index(
        "ix_formation_surface_fleet_scan_command_instance_created",
        table_name="formation_surface_fleet_scan_command",
        schema="azurpilot",
    )
    op.drop_index(
        "uq_formation_surface_fleet_scan_command_active_instance",
        table_name="formation_surface_fleet_scan_command",
        schema="azurpilot",
    )
    op.drop_table("formation_surface_fleet_scan_command", schema="azurpilot")
