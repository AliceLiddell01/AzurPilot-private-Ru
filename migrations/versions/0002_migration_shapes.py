"""Согласовать schema v1 с доказанными legacy migration shapes.

Revision ID: 0002_migration_shapes
Revises: 0001_storage_foundation

Downgrade предназначен только для пустой disposable БД: при Stage 3 data он
может быть отклонён из-за `akashi_ap`, а Numeric asset нельзя вернуть в bigint
без потери дробной точности.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_migration_shapes"
down_revision: str | None = "0001_storage_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_monthly_aggregate_metric_allowed"),
        "monthly_aggregate",
        schema="azurpilot",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_monthly_aggregate_metric_allowed"),
        "monthly_aggregate",
        "metric IN ('battle_count', 'akashi_encounters', 'akashi_ap', "
        "'meow_battle_raw_count', 'meow_battle_count')",
        schema="azurpilot",
    )
    op.alter_column(
        "cl1_ap_snapshot",
        "asset",
        schema="azurpilot",
        existing_type=sa.BigInteger(),
        type_=sa.Numeric(18, 2),
        existing_nullable=True,
        postgresql_using="asset::numeric(18, 2)",
    )


def downgrade() -> None:
    op.alter_column(
        "cl1_ap_snapshot",
        "asset",
        schema="azurpilot",
        existing_type=sa.Numeric(18, 2),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using="ROUND(asset)::bigint",
    )
    op.drop_constraint(
        op.f("ck_monthly_aggregate_metric_allowed"),
        "monthly_aggregate",
        schema="azurpilot",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_monthly_aggregate_metric_allowed"),
        "monthly_aggregate",
        "metric IN ('battle_count', 'akashi_encounters', "
        "'meow_battle_raw_count', 'meow_battle_count')",
        schema="azurpilot",
    )
