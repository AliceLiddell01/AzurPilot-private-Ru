"""PostgreSQL repository для typed notification event и dispatcher state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from re import fullmatch
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Connection, Table, and_, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import (
    StorageError,
    StorageInvariantViolationError,
)
from module.application.notifications.encoding import (
    canonical_digest,
    correlation_document,
    event_payload_digest,
    policy_snapshot_document,
    rendered_snapshot_document,
)
from module.application.notifications.models import (
    ClaimedDelivery,
    DeliveryResult,
    DeliveryResultClass,
    DeliveryState,
    DeliveryUpdate,
    NotificationAttribute,
    NotificationCorrelation,
    NotificationDeliveryPlan,
    NotificationEvent,
    NotificationEventProjection,
    NotificationPersistenceResult,
    NotificationPolicySnapshot,
    NotificationSensitivity,
    NotificationSeverity,
    NotificationStoredAttempt,
    NotificationStoredDelivery,
    NotificationStoredEvent,
    NotificationSubject,
    PolicyAction,
    PolicyDecision,
    PolicyState,
    PreparedDelivery,
    PublishStatus,
    RenderedSnapshot,
)
from module.application.notifications.registry import (
    NotificationRegistry,
    default_registry,
)
from module.application.notifications.state import RetryPolicy, expired_lease_update
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    notification_delivery,
    notification_delivery_attempt,
    notification_event,
    notification_policy_decision,
    notification_profile_sequence,
)

_SAFE_TOKEN_RE = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}"
_UNSAFE_TEXT_MARKERS = (
    "password",
    "secret",
    "token",
    "credential",
    "authorization",
    "bearer",
    "http://",
    "https://",
)


class PostgresNotificationRepository:
    """Repository выполняет только короткие DB операции и не знает о transport."""

    def __init__(
        self, connection: Connection, *, registry: NotificationRegistry | None = None
    ) -> None:
        self._connection = connection
        self._registry = registry if registry is not None else default_registry()

    def publish(
        self,
        event: NotificationEvent,
        *,
        payload_document: dict[str, object],
        payload_digest: str,
        decision: PolicyDecision,
        deliveries: tuple[NotificationDeliveryPlan, ...],
    ) -> NotificationPersistenceResult:
        try:
            savepoint = self._connection.begin_nested()
            try:
                profile_sequence = self._allocate_profile_sequence(event.profile_id)
                inserted_id = self._insert_event(
                    event,
                    payload_document=payload_document,
                    payload_digest=payload_digest,
                    profile_sequence=profile_sequence,
                )
                if inserted_id is None:
                    savepoint.rollback()
                    existing = self._find_existing(event)
                    return self._existing_result(existing, event, payload_digest)
                self._insert_decision(event.id, decision)
                for delivery in deliveries:
                    self._insert_delivery(delivery)
                savepoint.commit()
            except BaseException:
                if savepoint.is_active:
                    savepoint.rollback()
                raise
            stored = self._load_event_bundle(event.id)
            if stored is None:
                raise StorageInvariantViolationError(
                    "После insert notification event не читается в той же transaction."
                )
            return NotificationPersistenceResult(
                status=(
                    PublishStatus.SUPPRESSED
                    if decision.state is PolicyState.SUPPRESSED
                    else PublishStatus.PERSISTED
                ),
                event=stored[0],
                decision=stored[1],
                deliveries=stored[2],
            )
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
    ) -> tuple[ClaimedDelivery, ...]:
        now = _utc(now)
        _bounded_worker(worker_id)
        if not 1 <= batch_size <= 500:
            raise ValueError("Dispatcher batch size вне bounded диапазона.")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("Dispatcher lease вне bounded диапазона.")
        try:
            expired_rows = self._connection.execute(
                select(
                    notification_delivery.c.id,
                    notification_delivery.c.attempt_count,
                )
                .where(
                    notification_delivery.c.state.in_(
                        (
                            DeliveryState.PENDING.value,
                            DeliveryState.RETRY_WAIT.value,
                        )
                    ),
                    notification_delivery.c.deadline_at.is_not(None),
                    notification_delivery.c.deadline_at <= now,
                )
                .order_by(
                    notification_delivery.c.deadline_at,
                    notification_delivery.c.id,
                )
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            ).mappings().all()
            for row in expired_rows:
                delivery_id = cast(UUID, row["id"])
                attempt_ordinal = int(row["attempt_count"]) + 1
                result = DeliveryResult.permanent_failure("handover_deadline_expired")
                self._connection.execute(
                    notification_delivery_attempt.insert().values(
                        delivery_id=delivery_id,
                        attempt_ordinal=attempt_ordinal,
                        started_at=now,
                        finished_at=now,
                        result_class=result.result_class.value,
                        safe_error_code=result.safe_error_code,
                    )
                )
                changed = self._connection.execute(
                    update(notification_delivery)
                    .where(
                        notification_delivery.c.id == delivery_id,
                        notification_delivery.c.state.in_(
                            (
                                DeliveryState.PENDING.value,
                                DeliveryState.RETRY_WAIT.value,
                            )
                        ),
                    )
                    .values(
                        state=DeliveryState.FAILED.value,
                        next_attempt_at=now,
                        attempt_count=attempt_ordinal,
                        lease_owner=None,
                        lease_token=None,
                        lease_until=None,
                        last_safe_error_code=result.safe_error_code,
                        updated_at=now,
                    )
                )
                if changed.rowcount != 1:
                    raise StorageInvariantViolationError(
                        "Просроченная notification delivery не перешла в FAILED."
                    )
            if len(expired_rows) >= batch_size:
                return ()
            due = or_(
                and_(
                    notification_delivery.c.state == DeliveryState.PENDING.value,
                    notification_delivery.c.next_attempt_at <= now,
                ),
                and_(
                    notification_delivery.c.state == DeliveryState.RETRY_WAIT.value,
                    notification_delivery.c.next_attempt_at <= now,
                ),
            )
            rows = self._connection.execute(
                select(notification_delivery, notification_event)
                .join(
                    notification_event,
                    notification_event.c.id == notification_delivery.c.event_id,
                )
                .where(
                    due,
                    or_(
                        notification_delivery.c.deadline_at.is_(None),
                        notification_delivery.c.deadline_at > now,
                    ),
                )
                .order_by(
                    notification_delivery.c.priority.desc(),
                    notification_delivery.c.next_attempt_at,
                    notification_event.c.profile_sequence,
                    notification_delivery.c.id,
                )
                .with_for_update(skip_locked=True, of=notification_delivery)
                .limit(batch_size - len(expired_rows))
            ).all()
            claimed: list[ClaimedDelivery] = []
            for row in rows:
                delivery_row = _column_mapping(row, notification_delivery)
                event_row = _column_mapping(row, notification_event)
                delivery = self._stored_delivery(delivery_row)
                stored_event = self._stored_event(event_row)
                lease_token = uuid4()
                attempt_ordinal = delivery.attempt_count + 1
                lease_until = now + timedelta(seconds=lease_seconds)
                self._connection.execute(
                    update(notification_delivery)
                    .where(
                        notification_delivery.c.id == delivery.id,
                        notification_delivery.c.state.in_(
                            (DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)
                        ),
                    )
                    .values(
                        state=DeliveryState.IN_FLIGHT.value,
                        lease_owner=worker_id,
                        lease_token=lease_token,
                        lease_until=lease_until,
                        attempt_count=attempt_ordinal,
                        updated_at=now,
                    )
                )
                self._connection.execute(
                    notification_delivery_attempt.insert().values(
                        delivery_id=delivery.id,
                        attempt_ordinal=attempt_ordinal,
                        started_at=now,
                        lease_token=lease_token,
                        trace_id=stored_event.event.correlation.trace_id
                        if stored_event.event.correlation is not None
                        else None,
                        span_id=stored_event.event.correlation.span_id
                        if stored_event.event.correlation is not None
                        else None,
                    )
                )
                claimed_delivery = NotificationStoredDelivery(
                    id=delivery.id,
                    event_id=delivery.event_id,
                    channel_instance_id=delivery.channel_instance_id,
                    channel_type=delivery.channel_type,
                    state=DeliveryState.IN_FLIGHT,
                    priority=delivery.priority,
                    created_at=delivery.created_at,
                    next_attempt_at=delivery.next_attempt_at,
                    deadline_at=delivery.deadline_at,
                    attempt_count=attempt_ordinal,
                    lease_owner=worker_id,
                    lease_token=lease_token,
                    lease_until=lease_until,
                    last_safe_error_code=delivery.last_safe_error_code,
                    rendered_snapshot=delivery.rendered_snapshot,
                    idempotency_key=delivery.idempotency_key,
                    updated_at=now,
                )
                prepared = PreparedDelivery(
                    delivery_id=delivery.id,
                    event=NotificationEventProjection(
                        id=stored_event.event.id,
                        source=stored_event.event.source,
                        type=stored_event.event.type,
                        schema_version=stored_event.event.schema_version,
                        profile_id=stored_event.event.profile_id,
                        severity=stored_event.event.severity,
                        occurred_at=stored_event.event.occurred_at,
                        data=stored_event.event.data,
                        subject=stored_event.event.subject,
                        sensitivity=stored_event.event.sensitivity,
                    ),
                    rendered_snapshot=delivery.rendered_snapshot,
                    idempotency_key=delivery.idempotency_key,
                    timeout_seconds=float(min(lease_seconds, 300)),
                    attributes=(
                        NotificationAttribute("profile_id", stored_event.event.profile_id),
                        NotificationAttribute("event_type", stored_event.event.type),
                    ),
                )
                claimed.append(
                    ClaimedDelivery(
                        delivery=claimed_delivery,
                        event=stored_event,
                        attempt_ordinal=attempt_ordinal,
                        lease_token=lease_token,
                        prepared=prepared,
                    )
                )
            return tuple(claimed)
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def apply_update(
        self,
        *,
        delivery_id: UUID,
        lease_token: UUID,
        delivery_update: DeliveryUpdate,
    ) -> bool:
        now = _utc(delivery_update.completed_at or delivery_update.next_attempt_at)
        _validate_update(delivery_update)
        try:
            current = self._connection.execute(
                select(
                    notification_delivery.c.attempt_count,
                    notification_delivery.c.state,
                ).where(
                    notification_delivery.c.id == delivery_id,
                    notification_delivery.c.lease_token == lease_token,
                    notification_delivery.c.state == DeliveryState.IN_FLIGHT.value,
                )
            ).one_or_none()
            if current is None:
                return False
            values: dict[str, object] = {
                "state": delivery_update.state.value,
                "next_attempt_at": delivery_update.next_attempt_at,
                "lease_owner": None,
                "lease_token": None,
                "lease_until": delivery_update.lease_until
                if delivery_update.state is DeliveryState.AWAITING_AGENT_ACK
                else None,
                "last_safe_error_code": delivery_update.result.safe_error_code,
                "updated_at": now,
            }
            changed = self._connection.execute(
                update(notification_delivery)
                .where(
                    notification_delivery.c.id == delivery_id,
                    notification_delivery.c.lease_token == lease_token,
                    notification_delivery.c.state == DeliveryState.IN_FLIGHT.value,
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                return False
            attempt_ordinal = int(current.attempt_count)
            attempt_changed = self._connection.execute(
                update(notification_delivery_attempt)
                .where(
                    notification_delivery_attempt.c.delivery_id == delivery_id,
                    notification_delivery_attempt.c.attempt_ordinal == attempt_ordinal,
                    notification_delivery_attempt.c.lease_token == lease_token,
                )
                .values(
                    finished_at=now,
                    result_class=delivery_update.result.result_class.value,
                    safe_error_code=delivery_update.result.safe_error_code,
                    safe_error_summary=delivery_update.result.safe_error_summary,
                    retry_after_seconds=delivery_update.result.retry_after_seconds,
                    provider_message_id=delivery_update.result.provider_message_id,
                )
            )
            if attempt_changed.rowcount != 1:
                raise StorageInvariantViolationError(
                    "Delivery update не нашёл соответствующую append-only attempt."
                )
            return True
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def recover_expired(
        self,
        *,
        now: datetime,
        batch_size: int,
        worker_id: str,
        retry_policy: RetryPolicy,
    ) -> int:
        now = _utc(now)
        _bounded_worker(worker_id)
        if not 1 <= batch_size <= 500:
            raise ValueError("Recovery batch size вне bounded диапазона.")
        try:
            rows = self._connection.execute(
                select(notification_delivery)
                .where(
                    notification_delivery.c.state.in_(
                        (
                            DeliveryState.IN_FLIGHT.value,
                            DeliveryState.AWAITING_AGENT_ACK.value,
                        )
                    ),
                    notification_delivery.c.lease_until.is_not(None),
                    notification_delivery.c.lease_until <= now,
                )
                .order_by(notification_delivery.c.lease_until, notification_delivery.c.id)
                .with_for_update(skip_locked=True)
                .limit(batch_size)
            ).mappings().all()
            recovered = 0
            for row in rows:
                delivery = self._stored_delivery(row)
                previous_state = delivery.state
                old_token = delivery.lease_token
                next_attempt_count = delivery.attempt_count
                if previous_state is DeliveryState.AWAITING_AGENT_ACK:
                    next_attempt_count += 1
                transition = expired_lease_update(
                    state=previous_state,
                    now=now,
                    attempt_count=max(1, next_attempt_count),
                    deadline_at=delivery.deadline_at,
                    retry_policy=retry_policy,
                    idempotency_key=delivery.idempotency_key,
                )
                if previous_state is DeliveryState.AWAITING_AGENT_ACK:
                    self._connection.execute(
                        notification_delivery_attempt.insert().values(
                            delivery_id=delivery.id,
                            attempt_ordinal=next_attempt_count,
                            started_at=now,
                            finished_at=now,
                            result_class=transition.result.result_class.value,
                            safe_error_code=transition.result.safe_error_code,
                            trace_id=None,
                            span_id=None,
                        )
                    )
                else:
                    if old_token is None:
                        raise StorageInvariantViolationError(
                            "IN_FLIGHT delivery не содержит lease token при recovery."
                        )
                    attempt_changed = self._connection.execute(
                        update(notification_delivery_attempt)
                        .where(
                            notification_delivery_attempt.c.delivery_id == delivery.id,
                            notification_delivery_attempt.c.attempt_ordinal
                            == delivery.attempt_count,
                            notification_delivery_attempt.c.lease_token == old_token,
                        )
                        .values(
                            finished_at=now,
                            result_class=transition.result.result_class.value,
                            safe_error_code=transition.result.safe_error_code,
                        )
                    )
                    if attempt_changed.rowcount != 1:
                        raise StorageInvariantViolationError(
                            "Expired IN_FLIGHT delivery не имеет текущей attempt."
                        )
                self._connection.execute(
                    update(notification_delivery)
                    .where(notification_delivery.c.id == delivery.id)
                    .values(
                        state=transition.state.value,
                        next_attempt_at=transition.next_attempt_at,
                        attempt_count=next_attempt_count,
                        lease_owner=None,
                        lease_token=None,
                        lease_until=None,
                        last_safe_error_code=transition.result.safe_error_code,
                        updated_at=now,
                    )
                )
                recovered += 1
            return recovered
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def get_event(self, event_id: UUID) -> NotificationStoredEvent | None:
        try:
            row = self._connection.execute(
                select(notification_event).where(notification_event.c.id == event_id)
            ).mappings().one_or_none()
            return self._stored_event(row) if row is not None else None
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def get_decision(self, event_id: UUID) -> PolicyDecision | None:
        try:
            row = self._connection.execute(
                select(notification_policy_decision).where(
                    notification_policy_decision.c.event_id == event_id
                )
            ).mappings().one_or_none()
            return self._stored_decision(row) if row is not None else None
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def list_deliveries(self, event_id: UUID) -> tuple[NotificationStoredDelivery, ...]:
        try:
            rows = self._connection.execute(
                select(notification_delivery)
                .where(notification_delivery.c.event_id == event_id)
                .order_by(notification_delivery.c.id)
            ).mappings().all()
            return tuple(self._stored_delivery(row) for row in rows)
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def list_attempts(self, delivery_id: UUID) -> tuple[NotificationStoredAttempt, ...]:
        try:
            rows = self._connection.execute(
                select(notification_delivery_attempt)
                .where(notification_delivery_attempt.c.delivery_id == delivery_id)
                .order_by(notification_delivery_attempt.c.attempt_ordinal)
            ).mappings().all()
            return tuple(self._stored_attempt(row) for row in rows)
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def list_profile_history(
        self, *, profile_id: str, after_sequence: int = 0, limit: int = 100
    ) -> tuple[NotificationStoredEvent, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("History limit вне bounded диапазона.")
        try:
            rows = self._connection.execute(
                select(notification_event)
                .where(
                    notification_event.c.profile_id == profile_id,
                    notification_event.c.profile_sequence > after_sequence,
                )
                .order_by(notification_event.c.profile_sequence, notification_event.c.id)
                .limit(limit)
            ).mappings().all()
            return tuple(self._stored_event(row) for row in rows)
        except StorageError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def _allocate_profile_sequence(self, profile_id: str) -> int:
        row = self._connection.execute(
            pg_insert(notification_profile_sequence)
            .values(profile_id=profile_id, next_sequence=1)
            .on_conflict_do_update(
                index_elements=["profile_id"],
                set_={
                    "next_sequence": notification_profile_sequence.c.next_sequence
                },
            )
            .returning(notification_profile_sequence.c.next_sequence)
        ).one_or_none()
        if row is None:
            raise StorageInvariantViolationError("Profile sequence allocator не создал строку.")
        sequence = int(row.next_sequence)
        self._connection.execute(
            update(notification_profile_sequence)
            .where(notification_profile_sequence.c.profile_id == profile_id)
            .values(next_sequence=sequence + 1)
        )
        return sequence

    def _insert_event(
        self,
        event: NotificationEvent,
        *,
        payload_document: dict[str, object],
        payload_digest: str,
        profile_sequence: int,
    ) -> UUID | None:
        statement = (
            pg_insert(notification_event)
            .values(
                id=event.id,
                source=event.source,
                type=event.type,
                schema_version=event.schema_version,
                profile_id=event.profile_id,
                runtime_instance_id=event.runtime_instance_id,
                subject_kind=event.subject_kind,
                subject_id=event.subject_id,
                severity=event.severity.value,
                occurred_at=_utc(event.occurred_at),
                profile_sequence=profile_sequence,
                payload=payload_document,
                payload_digest=payload_digest,
                dedup_key=event.dedup_key,
                correlation=correlation_document(event.correlation)
                if event.correlation is not None
                else null(),
                sensitivity=event.sensitivity.value,
            )
            .on_conflict_do_nothing()
            .returning(notification_event.c.id)
        )
        return self._connection.execute(statement).scalar_one_or_none()

    def _insert_decision(self, event_id: UUID, decision: PolicyDecision) -> None:
        snapshot = policy_snapshot_document(decision.snapshot)
        self._connection.execute(
            notification_policy_decision.insert().values(
                event_id=event_id,
                state=decision.state.value,
                matched_rule_id=decision.matched_rule_id,
                policy_version=decision.policy_version,
                reason=decision.reason,
                channel_instance_ids=list(decision.channel_instance_ids),
                policy_snapshot=snapshot,
                policy_snapshot_hash=decision.snapshot_hash,
            )
        )

    def _insert_delivery(self, delivery: NotificationDeliveryPlan) -> None:
        self._connection.execute(
            notification_delivery.insert().values(
                id=delivery.id,
                event_id=delivery.event_id,
                channel_instance_id=delivery.channel_instance_id,
                channel_type=delivery.channel_type,
                state=DeliveryState.PENDING.value,
                priority=delivery.priority,
                next_attempt_at=_utc(delivery.next_attempt_at),
                deadline_at=_utc(delivery.deadline_at) if delivery.deadline_at else None,
                rendered_snapshot=rendered_snapshot_document(delivery.rendered_snapshot),
                idempotency_key=delivery.idempotency_key,
            )
        )

    def _find_existing(self, event: NotificationEvent) -> Mapping[str, object] | None:
        row = self._connection.execute(
            select(notification_event)
            .where(notification_event.c.id == event.id)
        ).mappings().one_or_none()
        if row is not None:
            return row
        if event.dedup_key is None:
            return None
        return self._connection.execute(
            select(notification_event)
            .where(
                notification_event.c.source == event.source,
                notification_event.c.profile_id == event.profile_id,
                notification_event.c.type == event.type,
                notification_event.c.dedup_key == event.dedup_key,
            )
        ).mappings().one_or_none()

    def _existing_result(
        self,
        row: Mapping[str, object] | None,
        event: NotificationEvent,
        payload_digest: str,
    ) -> NotificationPersistenceResult:
        if row is None:
            raise StorageInvariantViolationError(
                "Conflict insert не вернул существующий notification event."
            )
        same = _same_immutable_event(row, event, payload_digest)
        bundle = self._load_event_bundle(cast(UUID, row["id"]))
        if bundle is None:
            raise StorageInvariantViolationError("Существующий notification event не читается.")
        if not same:
            return NotificationPersistenceResult(
                status=PublishStatus.IDENTITY_CONFLICT,
                event=bundle[0],
                decision=bundle[1],
                deliveries=bundle[2],
                reason="immutable_identity_mismatch",
            )
        return NotificationPersistenceResult(
            status=PublishStatus.DUPLICATE,
            event=bundle[0],
            decision=bundle[1],
            deliveries=bundle[2],
        )

    def _load_event_bundle(
        self, event_id: UUID
    ) -> tuple[
        NotificationStoredEvent,
        PolicyDecision,
        tuple[NotificationStoredDelivery, ...],
    ] | None:
        event_row = self._connection.execute(
            select(notification_event).where(notification_event.c.id == event_id)
        ).mappings().one_or_none()
        if event_row is None:
            return None
        decision_row = self._connection.execute(
            select(notification_policy_decision).where(
                notification_policy_decision.c.event_id == event_id
            )
        ).mappings().one_or_none()
        if decision_row is None:
            raise StorageInvariantViolationError("Notification event не имеет policy decision.")
        deliveries = self._connection.execute(
            select(notification_delivery)
            .where(notification_delivery.c.event_id == event_id)
            .order_by(notification_delivery.c.id)
        ).mappings().all()
        return (
            self._stored_event(event_row),
            self._stored_decision(decision_row),
            tuple(self._stored_delivery(row) for row in deliveries),
        )

    def _stored_event(self, row: Mapping[object, object]) -> NotificationStoredEvent:
        try:
            return self._decode_stored_event(row)
        except StorageError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise StorageInvariantViolationError(
                "Stored notification event имеет повреждённую структуру."
            ) from None

    def _decode_stored_event(self, row: Mapping[object, object]) -> NotificationStoredEvent:
        payload = row["payload"]
        if not isinstance(payload, Mapping):
            raise StorageInvariantViolationError("Stored notification payload не является object.")
        subject = None
        if row["subject_kind"] is not None:
            subject = NotificationSubject(
                kind=cast(str, row["subject_kind"]), id=cast(str, row["subject_id"])
            )
            if not subject.is_valid():
                raise StorageInvariantViolationError(
                    "Stored notification subject не прошёл bounded validation."
                )
        correlation = _correlation_from_document(row["correlation"])
        event_type = cast(str, row["type"])
        schema_version = int(row["schema_version"])
        descriptor = self._registry.get(event_type, schema_version)
        try:
            data = descriptor.deserialize(payload) if descriptor else dict(payload)
        except Exception:  # noqa: BLE001 - corrupted durable payload is an invariant failure.
            raise StorageInvariantViolationError(
                "Stored notification payload не прошёл typed decoder."
            ) from None
        event = NotificationEvent(
            id=cast(UUID, row["id"]),
            source=cast(str, row["source"]),
            type=event_type,
            schema_version=schema_version,
            profile_id=cast(str, row["profile_id"]),
            runtime_instance_id=cast(str | None, row["runtime_instance_id"]),
            subject=subject,
            severity=NotificationSeverity(cast(str, row["severity"])),
            occurred_at=_utc(cast(datetime, row["occurred_at"])),
            data=data,
            dedup_key=cast(str | None, row["dedup_key"]),
            correlation=correlation,
            sensitivity=NotificationSensitivity(cast(str, row["sensitivity"])),
            persisted_at=_utc(cast(datetime, row["persisted_at"])),
            profile_sequence=int(row["profile_sequence"]),
            payload_digest=cast(str, row["payload_digest"]),
        )
        try:
            if event_payload_digest(event, payload) != event.payload_digest:
                raise StorageInvariantViolationError(
                    "Stored notification payload digest не совпадает с event."
                )
        except StorageError:
            raise
        except Exception:  # noqa: BLE001 - corrupt durable value becomes invariant failure.
            raise StorageInvariantViolationError(
                "Stored notification event не прошёл canonical digest check."
            ) from None
        return NotificationStoredEvent(event=event, payload_document=dict(payload))

    def _stored_decision(self, row: Mapping[object, object]) -> PolicyDecision:
        try:
            return self._decode_stored_decision(row)
        except StorageError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise StorageInvariantViolationError(
                "Stored policy decision имеет повреждённую структуру."
            ) from None

    def _decode_stored_decision(self, row: Mapping[object, object]) -> PolicyDecision:
        snapshot_doc = row["policy_snapshot"]
        if not isinstance(snapshot_doc, Mapping):
            raise StorageInvariantViolationError("Stored policy snapshot не является object.")
        channels = row["channel_instance_ids"]
        if not isinstance(channels, list) or not all(
            isinstance(item, str) for item in channels
        ):
            raise StorageInvariantViolationError(
                "Stored policy channels не являются bounded list."
            )
        action = PolicyAction(
            channel_instance_ids=tuple(channels),
            suppression_reason=cast(str | None, snapshot_doc.get("suppression_reason")),
            locale=cast(str, snapshot_doc.get("locale", "ru-RU")),
            presentation_profile=cast(
                str, snapshot_doc.get("presentation_profile", "default")
            ),
        )
        snapshot = NotificationPolicySnapshot(
            policy_version=int(snapshot_doc.get("policy_version", row["policy_version"])),
            rule_id=cast(str | None, snapshot_doc.get("rule_id")),
            action=action,
        )
        if canonical_digest(policy_snapshot_document(snapshot)) != row["policy_snapshot_hash"]:
            raise StorageInvariantViolationError(
                "Stored policy snapshot hash не совпадает с snapshot."
            )
        return PolicyDecision(
            state=PolicyState(cast(str, row["state"])),
            policy_version=int(row["policy_version"]),
            matched_rule_id=cast(str | None, row["matched_rule_id"]),
            reason=cast(str, row["reason"]),
            snapshot=snapshot,
            snapshot_hash=cast(str, row["policy_snapshot_hash"]),
        )

    @staticmethod
    def _stored_delivery(row: Mapping[object, object]) -> NotificationStoredDelivery:
        try:
            return PostgresNotificationRepository._decode_stored_delivery(row)
        except StorageError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise StorageInvariantViolationError(
                "Stored notification delivery имеет повреждённую структуру."
            ) from None

    @staticmethod
    def _decode_stored_delivery(row: Mapping[object, object]) -> NotificationStoredDelivery:
        snapshot = row["rendered_snapshot"]
        if not isinstance(snapshot, Mapping):
            raise StorageInvariantViolationError("Rendered snapshot не является object.")
        rendered = RenderedSnapshot(
            locale=cast(str, snapshot["locale"]),
            renderer_id=cast(str, snapshot["renderer_id"]),
            renderer_version=cast(str, snapshot["renderer_version"]),
            title=cast(str, snapshot["title"]),
            body=cast(str, snapshot["body"]),
        )
        if not rendered.is_valid(
            max_title=4096, max_body=8192, max_payload_bytes=8192
        ):
            raise StorageInvariantViolationError(
                "Stored rendered snapshot не прошёл bounded validation."
            )
        return NotificationStoredDelivery(
            id=cast(UUID, row["id"]),
            event_id=cast(UUID, row["event_id"]),
            channel_instance_id=cast(str, row["channel_instance_id"]),
            channel_type=cast(str, row["channel_type"]),
            state=DeliveryState(cast(str, row["state"])),
            priority=int(row["priority"]),
            created_at=_utc(cast(datetime, row["created_at"])),
            next_attempt_at=_utc(cast(datetime, row["next_attempt_at"])),
            deadline_at=_utc(cast(datetime, row["deadline_at"]))
            if row["deadline_at"] is not None
            else None,
            attempt_count=int(row["attempt_count"]),
            lease_owner=cast(str | None, row["lease_owner"]),
            lease_token=cast(UUID | None, row["lease_token"]),
            lease_until=_utc(cast(datetime, row["lease_until"]))
            if row["lease_until"] is not None
            else None,
            last_safe_error_code=cast(str | None, row["last_safe_error_code"]),
            rendered_snapshot=rendered,
            idempotency_key=cast(str, row["idempotency_key"]),
            updated_at=_utc(cast(datetime, row["updated_at"])),
        )

    @staticmethod
    def _stored_attempt(row: Mapping[object, object]) -> NotificationStoredAttempt:
        try:
            return PostgresNotificationRepository._decode_stored_attempt(row)
        except StorageError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise StorageInvariantViolationError(
                "Stored delivery attempt имеет повреждённую структуру."
            ) from None

    @staticmethod
    def _decode_stored_attempt(row: Mapping[object, object]) -> NotificationStoredAttempt:
        attempt = NotificationStoredAttempt(
            delivery_id=cast(UUID, row["delivery_id"]),
            attempt_ordinal=int(row["attempt_ordinal"]),
            started_at=_utc(cast(datetime, row["started_at"])),
            finished_at=_utc(cast(datetime, row["finished_at"]))
            if row["finished_at"] is not None
            else None,
            result_class=DeliveryResultClass(cast(str, row["result_class"]))
            if row["result_class"] is not None
            else None,
            safe_error_code=cast(str | None, row["safe_error_code"]),
            safe_error_summary=cast(str | None, row["safe_error_summary"]),
            retry_after_seconds=cast(int | None, row["retry_after_seconds"]),
            provider_message_id=cast(str | None, row["provider_message_id"]),
            lease_token=cast(UUID | None, row["lease_token"]),
            trace_id=cast(str | None, row["trace_id"]),
            span_id=cast(str | None, row["span_id"]),
        )
        if attempt.attempt_ordinal <= 0:
            raise StorageInvariantViolationError("Stored attempt ordinal не положителен.")
        if attempt.safe_error_code is not None and fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", attempt.safe_error_code
        ) is None:
            raise StorageInvariantViolationError("Stored attempt error code не bounded.")
        if attempt.safe_error_summary is not None and (
            not isinstance(attempt.safe_error_summary, str)
            or
            len(attempt.safe_error_summary) > 256
            or any(ord(character) < 32 for character in attempt.safe_error_summary)
            or any(
                marker in attempt.safe_error_summary.casefold()
                for marker in _UNSAFE_TEXT_MARKERS
            )
        ):
            raise StorageInvariantViolationError("Stored attempt summary не bounded.")
        if attempt.retry_after_seconds is not None and not 0 <= attempt.retry_after_seconds <= 3600:
            raise StorageInvariantViolationError("Stored attempt retry-after вне диапазона.")
        if attempt.provider_message_id is not None and (
            not isinstance(attempt.provider_message_id, str)
            or
            fullmatch(_SAFE_TOKEN_RE, attempt.provider_message_id) is None
            or any(
                marker in attempt.provider_message_id.casefold()
                for marker in _UNSAFE_TEXT_MARKERS
            )
        ):
            raise StorageInvariantViolationError("Stored provider message id не bounded.")
        if attempt.trace_id is not None and (
            not isinstance(attempt.trace_id, str)
            or
            fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", attempt.trace_id) is None
        ):
            raise StorageInvariantViolationError("Stored trace id не bounded.")
        if attempt.span_id is not None and (
            not isinstance(attempt.span_id, str)
            or fullmatch(r"[0-9a-f]{16}", attempt.span_id) is None
        ):
            raise StorageInvariantViolationError("Stored span id не bounded.")
        return attempt


def _column_mapping(row: object, table: Table) -> dict[str, object]:
    mapping = getattr(row, "_mapping", {})
    return {
        column.name: mapping[column]
        for column in table.c
    }


def _utc(value: datetime | None) -> datetime:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise StorageInvariantViolationError("Notification timestamp должен быть timezone-aware.")
    return value.astimezone(UTC)


def _bounded_worker(worker_id: str) -> None:
    if not isinstance(worker_id, str) or fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", worker_id) is None:
        raise ValueError("Dispatcher worker id имеет неверный формат.")


def _validate_result(result: DeliveryResult) -> None:
    if not isinstance(result, DeliveryResult) or not result.is_valid():
        raise StorageInvariantViolationError("DeliveryResult class не зарегистрирован.")
    if result.safe_error_summary is not None and (
        any(marker in result.safe_error_summary.casefold() for marker in _UNSAFE_TEXT_MARKERS)
    ):
        raise StorageInvariantViolationError("DeliveryResult summary не bounded.")
    if result.provider_message_id is not None and fullmatch(
        _SAFE_TOKEN_RE, result.provider_message_id
    ) is None:
        raise StorageInvariantViolationError("Provider message id не bounded.")
    if result.provider_message_id is not None and any(
        marker in result.provider_message_id.casefold() for marker in _UNSAFE_TEXT_MARKERS
    ):
        raise StorageInvariantViolationError("Provider message id содержит запрещённый marker.")


def _validate_update(update: DeliveryUpdate) -> None:
    if not isinstance(update, DeliveryUpdate):
        raise StorageInvariantViolationError("Delivery update имеет неверный тип.")
    allowed_states = {
        DeliveryState.RETRY_WAIT,
        DeliveryState.FAILED,
        DeliveryState.PROVIDER_ACCEPTED,
        DeliveryState.AWAITING_AGENT_ACK,
        DeliveryState.DELIVERED,
        DeliveryState.SUPPRESSED,
    }
    if update.state not in allowed_states:
        raise StorageInvariantViolationError(
            "Delivery update не может записать claim state напрямую."
        )
    if not isinstance(update.next_attempt_at, datetime):
        raise StorageInvariantViolationError("Delivery next_attempt_at имеет неверный тип.")
    _utc(update.next_attempt_at)
    if update.completed_at is not None:
        if not isinstance(update.completed_at, datetime):
            raise StorageInvariantViolationError("Delivery completed_at имеет неверный тип.")
        _utc(update.completed_at)
    if update.state is DeliveryState.AWAITING_AGENT_ACK:
        if update.lease_until is None:
            raise StorageInvariantViolationError(
                "AWAITING_AGENT_ACK требует bounded ack deadline."
            )
        _utc(update.lease_until)
    elif update.lease_until is not None:
        raise StorageInvariantViolationError(
            "Только AWAITING_AGENT_ACK может сохранять lease_until."
        )
    _validate_result(update.result)
    expected_classes = {
        DeliveryState.RETRY_WAIT: {
            DeliveryResultClass.TRANSIENT_FAILURE,
            DeliveryResultClass.UNAVAILABLE,
        },
        DeliveryState.FAILED: {
            DeliveryResultClass.PERMANENT_FAILURE,
            DeliveryResultClass.TRANSIENT_FAILURE,
            DeliveryResultClass.UNAVAILABLE,
        },
        DeliveryState.PROVIDER_ACCEPTED: {DeliveryResultClass.PROVIDER_ACCEPTED},
        DeliveryState.AWAITING_AGENT_ACK: {DeliveryResultClass.PROVIDER_ACCEPTED},
        DeliveryState.DELIVERED: {DeliveryResultClass.DELIVERED},
        DeliveryState.SUPPRESSED: {DeliveryResultClass.SUPPRESSED},
    }
    if update.result.result_class not in expected_classes[update.state]:
        raise StorageInvariantViolationError(
            "Delivery state и DeliveryResult class не согласованы."
        )


def _same_immutable_event(
    row: Mapping[object, object], event: NotificationEvent, payload_digest: str
) -> bool:
    return (
        row["source"] == event.source
        and row["type"] == event.type
        and int(row["schema_version"]) == event.schema_version
        and row["profile_id"] == event.profile_id
        and row["runtime_instance_id"] == event.runtime_instance_id
        and row["subject_kind"] == event.subject_kind
        and row["subject_id"] == event.subject_id
        and row["severity"] == event.severity.value
        and _utc(cast(datetime, row["occurred_at"])) == _utc(event.occurred_at)
        and row["payload_digest"] == payload_digest
        and row["dedup_key"] == event.dedup_key
        and row["sensitivity"] == event.sensitivity.value
    )


def _correlation_from_document(value: object) -> NotificationCorrelation | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StorageInvariantViolationError("Stored correlation не является object.")
    parent = value.get("parent_event_id")
    try:
        parent_id = UUID(str(parent)) if parent is not None else None
    except (TypeError, ValueError):
        raise StorageInvariantViolationError("Stored correlation parent id некорректен.") from None
    correlation = NotificationCorrelation(
        task_id=cast(str | None, value.get("task_id")),
        runtime_session_id=cast(str | None, value.get("runtime_session_id")),
        parent_event_id=parent_id,
        trace_id=cast(str | None, value.get("trace_id")),
        span_id=cast(str | None, value.get("span_id")),
    )
    if not correlation.is_valid():
        raise StorageInvariantViolationError(
            "Stored correlation не прошёл bounded validation."
        )
    return correlation


__all__ = ["PostgresNotificationRepository"]
