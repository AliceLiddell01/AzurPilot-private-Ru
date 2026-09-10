"""Добавить durable receipt для authenticated Desktop Agent ACK."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_notification_agent_ack"
down_revision: str | None = "0009_notification_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"


def upgrade() -> None:
    op.create_table(
        "notification_agent_ack",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_ordinal", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_source", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("session_epoch", sa.Uuid(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_ordinal > 0",
            name=op.f("ck_notification_agent_ack_attempt_ordinal_positive"),
        ),
        sa.CheckConstraint(
            "btrim(event_source) <> ''",
            name=op.f("ck_notification_agent_ack_event_source_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(profile_id) <> ''",
            name=op.f("ck_notification_agent_ack_profile_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(agent_id) <> ''",
            name=op.f("ck_notification_agent_ack_agent_id_not_blank"),
        ),
        sa.CheckConstraint(
            "lease_token = session_epoch",
            name=op.f("ck_notification_agent_ack_session_epoch_matches_lease"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_notification_agent_ack_payload_digest_format"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id", "attempt_ordinal"],
            [
                "azurpilot.notification_delivery_attempt.delivery_id",
                "azurpilot.notification_delivery_attempt.attempt_ordinal",
            ],
            ondelete="CASCADE",
            name=op.f("fk_notification_agent_ack_attempt"),
        ),
        sa.PrimaryKeyConstraint(
            "delivery_id",
            "attempt_ordinal",
            name=op.f("pk_notification_agent_ack"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        op.f("ix_notification_agent_ack_event"),
        "notification_agent_ack",
        ["event_id", "event_source"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notification_agent_ack_event"),
        table_name="notification_agent_ack",
        schema=_SCHEMA,
    )
    op.drop_table("notification_agent_ack", schema=_SCHEMA)
