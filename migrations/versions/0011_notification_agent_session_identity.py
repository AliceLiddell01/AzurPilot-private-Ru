"""Разделить identity соединения Desktop Agent и lease попытки."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_agent_session_identity"
down_revision: str | None = "0010_notification_agent_ack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"
_CONSTRAINT = "ck_notification_agent_ack_session_epoch_matches_lease"


def upgrade() -> None:
    # 0010 уже опубликована; удаляем только её временную проверку равенства
    # отдельной миграцией, сохраняя воспроизводимость старой истории.
    op.drop_constraint(
        op.f(_CONSTRAINT),
        "notification_agent_ack",
        schema=_SCHEMA,
        type_="check",
    )


def downgrade() -> None:
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "notification_agent_ack",
        "lease_token = session_epoch",
        schema=_SCHEMA,
    )
