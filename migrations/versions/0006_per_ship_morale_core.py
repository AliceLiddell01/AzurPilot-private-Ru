"""Добавить append-only Per-ship Morale observations.

Revision ID: 0006_per_ship_morale_core
Revises: 0005_fleet_ship_form
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_per_ship_morale_core"
down_revision: str | None = "0005_fleet_ship_form"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"
_TABLE = "formation_surface_fleet_morale_observation"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_formation_fleet_snapshot_provenance",
        "formation_surface_fleet_snapshot",
        ["id", "instance_id", "fleet_index"],
        schema=_SCHEMA,
    )
    op.create_unique_constraint(
        "uq_formation_fleet_slot_morale_identity",
        "formation_surface_fleet_slot",
        [
            "snapshot_id",
            "side",
            "position",
            "canonical_identity_key",
            "ship_form",
        ],
        schema=_SCHEMA,
    )
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("formation_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("fleet_index", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("canonical_identity_key", sa.String(length=128), nullable=False),
        sa.Column("ship_form", sa.String(length=16), nullable=False),
        sa.Column("baseline", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recovery_per_hour", sa.Numeric(precision=10, scale=6), nullable=False
        ),
        sa.Column(
            "recovery_ceiling", sa.Numeric(precision=9, scale=6), nullable=False
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("recovery_source", sa.String(length=64), nullable=False),
        sa.Column("knowledge", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "baseline BETWEEN 0 AND 150",
            name=op.f("ck_formation_surface_fleet_morale_observation_baseline_range"),
        ),
        sa.CheckConstraint(
            "recovery_ceiling BETWEEN 0 AND 150",
            name=op.f("ck_formation_surface_fleet_morale_observation_ceiling_range"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_formation_surface_fleet_morale_observation_digest_sha256"),
        ),
        sa.CheckConstraint(
            "fleet_index BETWEEN 1 AND 6",
            name=op.f("ck_formation_surface_fleet_morale_observation_fleet_range"),
        ),
        sa.CheckConstraint(
            "knowledge = 'exact'",
            name=op.f("ck_formation_surface_fleet_morale_observation_knowledge_exact"),
        ),
        sa.CheckConstraint(
            "recovery_per_hour BETWEEN 0 AND 1500",
            name=op.f("ck_formation_surface_fleet_morale_observation_rate_range"),
        ),
        sa.CheckConstraint(
            "btrim(recovery_source) <> ''",
            name=op.f("ck_formation_surface_fleet_morale_observation_recover_not_blank"),
        ),
        sa.CheckConstraint(
            "ship_form IN ('base', 'retrofit')",
            name=op.f("ck_formation_surface_fleet_morale_observation_ship_form_allowed"),
        ),
        sa.CheckConstraint(
            "side IN ('main', 'vanguard')",
            name=op.f("ck_formation_surface_fleet_morale_observation_side_allowed"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 1 AND 3",
            name=op.f("ck_formation_surface_fleet_morale_observation_position_range"),
        ),
        sa.CheckConstraint(
            "btrim(source) <> ''",
            name=op.f("ck_formation_surface_fleet_morale_observation_source_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["formation_snapshot_id", "instance_id", "fleet_index"],
            [
                "azurpilot.formation_surface_fleet_snapshot.id",
                "azurpilot.formation_surface_fleet_snapshot.instance_id",
                "azurpilot.formation_surface_fleet_snapshot.fleet_index",
            ],
            name="fk_morale_observation_snapshot_provenance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "formation_snapshot_id",
                "side",
                "position",
                "canonical_identity_key",
                "ship_form",
            ],
            [
                "azurpilot.formation_surface_fleet_slot.snapshot_id",
                "azurpilot.formation_surface_fleet_slot.side",
                "azurpilot.formation_surface_fleet_slot.position",
                "azurpilot.formation_surface_fleet_slot.canonical_identity_key",
                "azurpilot.formation_surface_fleet_slot.ship_form",
            ],
            name="fk_morale_observation_slot_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_formation_surface_fleet_morale_observation")
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f(
                "uq_formation_surface_fleet_morale_observation_idempotency_key"
            ),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_formation_fleet_morale_latest",
        _TABLE,
        [
            "instance_id",
            "fleet_index",
            "side",
            "position",
            sa.text("observed_at DESC"),
            sa.text("id DESC"),
        ],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_formation_fleet_morale_latest", table_name=_TABLE, schema=_SCHEMA
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
    op.drop_constraint(
        "uq_formation_fleet_slot_morale_identity",
        "formation_surface_fleet_slot",
        schema=_SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        "uq_formation_fleet_snapshot_provenance",
        "formation_surface_fleet_snapshot",
        schema=_SCHEMA,
        type_="unique",
    )
