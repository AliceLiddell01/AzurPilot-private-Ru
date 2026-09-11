"""Разделить identity соединения Desktop Agent и lease попытки."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_agent_session_identity"
down_revision: str | None = "0010_notification_agent_ack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"
_CONSTRAINT = "ck_notification_agent_ack_session_epoch_matches_lease"


def upgrade() -> None:
    # Удалять constraint можно повторно после non-lossless downgrade.
    op.execute(
        sa.text(
            f'ALTER TABLE "{_SCHEMA}"."notification_agent_ack" '
            f'DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}"'
        )
    )


def downgrade() -> None:
    # Старый equality-check нельзя безопасно восстановить после появления
    # receipt с независимыми server-issued session и lease identity.
    # Оставляем таблицу работоспособной; ограничение rollback описано в README.
    return
