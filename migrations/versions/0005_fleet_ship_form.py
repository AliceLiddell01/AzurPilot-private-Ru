"""Сохранить форму корабля отдельно от canonical Fleet identity.

Revision ID: 0005_fleet_ship_form
Revises: 0004_fleet_manual_scan_command
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_fleet_ship_form"
down_revision: str | None = "0004_fleet_manual_scan_command"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDENTITY_CONSTRAINT = "ck_formation_surface_fleet_slot_identity_consistent"
_SHIP_FORM_CONSTRAINT = "ck_formation_surface_fleet_slot_ship_form_allowed"


def upgrade() -> None:
    op.add_column(
        "formation_surface_fleet_slot",
        sa.Column("ship_form", sa.String(length=16), nullable=True),
        schema="azurpilot",
    )
    op.execute(
        sa.text(
            "UPDATE azurpilot.formation_surface_fleet_slot "
            "SET ship_form = CASE "
            "WHEN lower(coalesce(nullif(displayed_name, ''), raw_name_ocr)) "
            "LIKE '%(retro%' THEN 'retrofit' ELSE 'base' END "
            "WHERE identity_status = 'matched'"
        )
    )
    op.drop_constraint(
        op.f(_IDENTITY_CONSTRAINT),
        "formation_surface_fleet_slot",
        schema="azurpilot",
        type_="check",
    )
    op.create_check_constraint(
        op.f(_SHIP_FORM_CONSTRAINT),
        "formation_surface_fleet_slot",
        "ship_form IS NULL OR ship_form IN ('base', 'retrofit')",
        schema="azurpilot",
    )
    op.create_check_constraint(
        op.f(_IDENTITY_CONSTRAINT),
        "formation_surface_fleet_slot",
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
        schema="azurpilot",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f(_IDENTITY_CONSTRAINT),
        "formation_surface_fleet_slot",
        schema="azurpilot",
        type_="check",
    )
    op.drop_constraint(
        op.f(_SHIP_FORM_CONSTRAINT),
        "formation_surface_fleet_slot",
        schema="azurpilot",
        type_="check",
    )
    op.drop_column(
        "formation_surface_fleet_slot",
        "ship_form",
        schema="azurpilot",
    )
    op.create_check_constraint(
        op.f(_IDENTITY_CONSTRAINT),
        "formation_surface_fleet_slot",
        "(occupied = false AND identity_status IS NULL AND raw_name_ocr IS NULL "
        "AND displayed_name IS NULL AND canonical_identity_key IS NULL "
        "AND canonical_name IS NULL) OR "
        "(occupied = true AND identity_status IN ('unresolved', 'ambiguous') "
        "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
        "AND canonical_identity_key IS NULL AND canonical_name IS NULL) OR "
        "(occupied = true AND identity_status = 'matched' "
        "AND raw_name_ocr IS NOT NULL AND displayed_name IS NOT NULL "
        "AND canonical_identity_key IS NOT NULL AND canonical_name IS NOT NULL)",
        schema="azurpilot",
    )
