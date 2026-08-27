"""Сохранить смысловой ключ идемпотентности скана Dorm в пределах экземпляра приложения.

Revision ID: 0008_dorm_morale_idempotency
Revises: 0007_dorm_morale_reconciliation

Существующие строки, созданные схемой ``0007``, содержат SHA-256, рассчитанный из
``instance_id`` и исходного ключа вызывающего кода. Массово восстановить исходные
ключи по уже сохранённым хэшам нельзя. Миграция не вводит параллельный путь
совместимости со старым форматом: прежние значения остаются непрозрачными
историческими ключами, а новые записи хранят исходный ключ напрямую.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_dorm_morale_idempotency"
down_revision: str | None = "0007_dorm_morale_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"
_SCAN = "dorm_morale_scan_run"
_OLD_UNIQUE = "uq_dorm_morale_scan_run_idempotency_key"
_NEW_UNIQUE = "uq_dorm_morale_scan_run_instance_id_idempotency_key"


def upgrade() -> None:
    op.drop_constraint(_OLD_UNIQUE, _SCAN, schema=_SCHEMA, type_="unique")
    op.create_unique_constraint(
        _NEW_UNIQUE,
        _SCAN,
        ("instance_id", "idempotency_key"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT idempotency_key FROM azurpilot.dorm_morale_scan_run "
            "GROUP BY idempotency_key HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Откат 0008 невозможен: одинаковый ключ идемпотентности скана Dorm "
            "уже используется несколькими экземплярами приложения."
        )
    op.drop_constraint(_NEW_UNIQUE, _SCAN, schema=_SCHEMA, type_="unique")
    op.create_unique_constraint(
        _OLD_UNIQUE,
        _SCAN,
        ("idempotency_key",),
        schema=_SCHEMA,
    )
