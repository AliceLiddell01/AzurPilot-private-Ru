"""Добавить durable typed notification foundation и PostgreSQL dispatcher state."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_notification_foundation"
down_revision: str | None = "0008_dorm_morale_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "azurpilot"


def upgrade() -> None:
    op.create_table(
        "notification_event",
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("runtime_instance_id", sa.String(length=128), nullable=True),
        sa.Column("subject_kind", sa.String(length=32), nullable=True),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "persisted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("profile_sequence", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
        sa.Column("correlation", postgresql.JSONB(), nullable=True),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_notification_event_schema_version_positive")),
        sa.CheckConstraint("profile_sequence > 0", name=op.f("ck_notification_event_profile_sequence_positive")),
        sa.CheckConstraint("btrim(source) <> ''", name=op.f("ck_notification_event_source_not_blank")),
        sa.CheckConstraint("btrim(type) <> ''", name=op.f("ck_notification_event_type_not_blank")),
        sa.CheckConstraint("btrim(profile_id) <> ''", name=op.f("ck_notification_event_profile_not_blank")),
        sa.CheckConstraint(
            "dedup_key IS NULL OR btrim(dedup_key) <> ''",
            name=op.f("ck_notification_event_dedup_key_not_blank"),
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')",
            name=op.f("ck_notification_event_severity_allowed"),
        ),
        sa.CheckConstraint(
            "sensitivity IN ('NORMAL', 'SENSITIVE')",
            name=op.f("ck_notification_event_sensitivity_allowed"),
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_notification_event_digest_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_notification_event_payload_object"),
        ),
        sa.CheckConstraint(
            "correlation IS NULL OR jsonb_typeof(correlation) = 'object'",
            name=op.f("ck_notification_event_correlation_object"),
        ),
        sa.CheckConstraint(
            "(subject_kind IS NULL AND subject_id IS NULL) OR "
            "(subject_kind IS NOT NULL AND subject_id IS NOT NULL)",
            name=op.f("ck_notification_event_subject_consistent"),
        ),
        sa.PrimaryKeyConstraint("row_id", name=op.f("pk_notification_event")),
        sa.UniqueConstraint(
            "profile_id",
            "profile_sequence",
            name=op.f("uq_notification_event_profile_sequence"),
        ),
        sa.UniqueConstraint(
            "source",
            "id",
            name=op.f("uq_notification_event_occurrence"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "uq_notification_event_logical_identity",
        "notification_event",
        ["source", "profile_id", "type", "dedup_key"],
        unique=True,
        schema=_SCHEMA,
        postgresql_where=sa.text("dedup_key IS NOT NULL"),
    )
    op.create_index(
        "ix_notification_event_profile_history",
        "notification_event",
        ["profile_id", "profile_sequence", "id"],
        schema=_SCHEMA,
    )

    op.create_table(
        "notification_policy_decision",
        sa.Column("event_row_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("matched_rule_id", sa.String(length=128), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("channel_instance_ids", postgresql.JSONB(), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("policy_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('ROUTED', 'SUPPRESSED')",
            name=op.f("ck_notification_policy_decision_state_allowed"),
        ),
        sa.CheckConstraint(
            "policy_version > 0",
            name=op.f("ck_notification_policy_decision_policy_version_positive"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_notification_policy_decision_reason_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(channel_instance_ids) = 'array' AND "
            "jsonb_array_length(channel_instance_ids) <= 32",
            name=op.f("ck_notification_policy_decision_channels_bounded"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_snapshot) = 'object'",
            name=op.f("ck_notification_policy_decision_snapshot_object"),
        ),
        sa.CheckConstraint(
            "policy_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_notification_policy_decision_snapshot_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["event_row_id"],
            ["azurpilot.notification_event.row_id"],
            ondelete="CASCADE",
            name=op.f("fk_notification_policy_decision_event"),
        ),
        sa.PrimaryKeyConstraint("event_row_id", name=op.f("pk_notification_policy_decision")),
        schema=_SCHEMA,
    )

    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_row_id", sa.Uuid(), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=128), nullable=False),
        sa.Column("channel_type", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("rendered_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'IN_FLIGHT', 'RETRY_WAIT', 'FAILED', "
            "'PROVIDER_ACCEPTED', 'AWAITING_AGENT_ACK', 'DELIVERED', 'SUPPRESSED')",
            name=op.f("ck_notification_delivery_state_allowed"),
        ),
        sa.CheckConstraint(
            "priority BETWEEN -100 AND 100",
            name=op.f("ck_notification_delivery_priority_range"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_notification_delivery_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rendered_snapshot) = 'object'",
            name=op.f("ck_notification_delivery_snapshot_object"),
        ),
        sa.CheckConstraint(
            "(state = 'IN_FLIGHT' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "state <> 'IN_FLIGHT'",
            name=op.f("ck_notification_delivery_in_flight_lease_consistent"),
        ),
        sa.CheckConstraint(
            "state <> 'AWAITING_AGENT_ACK' OR "
            "(lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name=op.f("ck_notification_delivery_awaiting_ack_lease_consistent"),
        ),
        sa.CheckConstraint(
            "last_safe_error_code IS NULL OR "
            "last_safe_error_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'",
            name=op.f("ck_notification_delivery_last_safe_error_code_format"),
        ),
        sa.ForeignKeyConstraint(
            ["event_row_id"],
            ["azurpilot.notification_event.row_id"],
            ondelete="CASCADE",
            name=op.f("fk_notification_delivery_event"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "event_row_id",
            "channel_instance_id",
            name=op.f("uq_notification_delivery_event_channel"),
        ),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_notification_delivery_idempotency")
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_notification_delivery_claim_due",
        "notification_delivery",
        [sa.text("priority DESC"), "next_attempt_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("state IN ('PENDING', 'RETRY_WAIT')"),
    )
    op.create_index(
        "ix_notification_delivery_deadline_expiry",
        "notification_delivery",
        ["deadline_at", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text(
            "state IN ('PENDING', 'RETRY_WAIT') AND deadline_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_notification_delivery_event",
        "notification_delivery",
        ["event_row_id", "id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_notification_delivery_lease_expiry",
        "notification_delivery",
        ["lease_until", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("lease_until IS NOT NULL"),
    )

    op.create_table(
        "notification_delivery_attempt",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_ordinal", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_class", sa.String(length=32), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("safe_error_summary", sa.String(length=256), nullable=True),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("span_id", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "attempt_ordinal > 0",
            name=op.f("ck_notification_delivery_attempt_attempt_ordinal_positive"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_notification_delivery_attempt_finished_time_ordered"),
        ),
        sa.CheckConstraint(
            "result_class IS NULL OR result_class IN ('DELIVERED', 'PROVIDER_ACCEPTED', "
            "'TRANSIENT_FAILURE', 'PERMANENT_FAILURE', 'UNAVAILABLE', 'SUPPRESSED')",
            name=op.f("ck_notification_delivery_attempt_result_class_allowed"),
        ),
        sa.CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds BETWEEN 0 AND 3600",
            name=op.f("ck_notification_delivery_attempt_retry_after_range"),
        ),
        sa.CheckConstraint(
            "safe_error_code IS NULL OR "
            "safe_error_code ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'",
            name=op.f("ck_notification_delivery_attempt_safe_error_code_format"),
        ),
        sa.CheckConstraint(
            "safe_error_summary IS NULL OR safe_error_summary !~ '[[:cntrl:]]'",
            name=op.f("ck_notification_delivery_attempt_safe_error_summary_no_control"),
        ),
        sa.CheckConstraint(
            "trace_id IS NULL OR trace_id ~ '^[0-9a-f]{16}$|^[0-9a-f]{32}$'",
            name=op.f("ck_notification_delivery_attempt_trace_id_format"),
        ),
        sa.CheckConstraint(
            "span_id IS NULL OR span_id ~ '^[0-9a-f]{16}$'",
            name=op.f("ck_notification_delivery_attempt_span_id_format"),
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["azurpilot.notification_delivery.id"],
            ondelete="CASCADE",
            name=op.f("fk_notification_delivery_attempt_delivery"),
        ),
        sa.PrimaryKeyConstraint(
            "delivery_id",
            "attempt_ordinal",
            name=op.f("pk_notification_delivery_attempt"),
        ),
        schema=_SCHEMA,
    )

    op.create_table(
        "notification_profile_sequence",
        sa.Column("profile_id", sa.String(length=128), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "btrim(profile_id) <> ''",
            name=op.f("ck_notification_profile_sequence_profile_not_blank"),
        ),
        sa.CheckConstraint(
            "next_sequence > 0",
            name=op.f("ck_notification_profile_sequence_next_sequence_positive"),
        ),
        sa.PrimaryKeyConstraint("profile_id", name=op.f("pk_notification_profile_sequence")),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("notification_delivery_attempt", schema=_SCHEMA)
    op.drop_table("notification_delivery", schema=_SCHEMA)
    op.drop_table("notification_policy_decision", schema=_SCHEMA)
    op.drop_table("notification_profile_sequence", schema=_SCHEMA)
    op.drop_table("notification_event", schema=_SCHEMA)
