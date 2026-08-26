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

# До этой ревизии форма отдельно не сохранялась. Исторический writer принимал
# MATCHED только от общего Formation identity resolver: его Retrofit-ветка
# всегда оставляла в displayed_name структурный suffix, а exact/fuzzy/truncated
# ветки без такого evidence семантически соответствуют BASE.
_LEGACY_RETROFIT_EVIDENCE = (
    "displayed_name ~* "
    "'[[:space:]]+[(]retrofit[)]$' "
    "OR displayed_name ~* "
    "'[[:space:]]+[(](r|re|ret|retr|retro|retrof|retrofi|retrofit)"
    "([.]{2,3}|…)+$' "
    "OR displayed_name ~* "
    "'[[:space:]]+[(]retro(f(i(t)?)?)?[0-9]?$'"
)


def upgrade() -> None:
    op.add_column(
        "formation_surface_fleet_slot",
        sa.Column("ship_form", sa.String(length=16), nullable=True),
        schema="azurpilot",
    )
    op.execute(
        sa.text(
            "DO $$ "
            "DECLARE invalid_count bigint; "
            "BEGIN "
            "SELECT count(*) INTO invalid_count "
            "FROM azurpilot.formation_surface_fleet_slot "
            "WHERE identity_status = 'matched' AND ("
            "raw_name_ocr IS NULL OR btrim(raw_name_ocr) = '' "
            "OR displayed_name IS NULL OR btrim(displayed_name) = '' "
            "OR canonical_name IS NULL OR btrim(canonical_name) = '' "
            "OR canonical_identity_key IS NULL "
            "OR canonical_identity_key !~ '^azur_lane_ship_group:[0-9]+$' "
            "OR displayed_name ~* '[[:space:]]+[(]([.]{2,3}|…)+$'"
            "); "
            "IF invalid_count > 0 THEN "
            "RAISE EXCEPTION "
            "'Миграция 0005: обнаружено % структурно некорректных исторических MATCHED-слотов.', "
            "invalid_count "
            "USING HINT = 'Требуется сверка Fleet State или повторное "
            "сканирование; форма для некорректной записи не назначается.'; "
            "END IF; "
            "END; $$"
        )
    )
    op.execute(
        sa.text(
            "UPDATE azurpilot.formation_surface_fleet_slot "
            "SET ship_form = 'retrofit' "
            "WHERE identity_status = 'matched' AND ("
            f"{_LEGACY_RETROFIT_EVIDENCE}"
            ")"
        )
    )
    op.execute(
        sa.text(
            "UPDATE azurpilot.formation_surface_fleet_slot "
            "SET ship_form = 'base' "
            "WHERE identity_status = 'matched' AND ship_form IS NULL"
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
