"""Общие in-memory fixtures для notification unit tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Self
from uuid import UUID, uuid4

from module.application.errors import StorageInvariantViolationError
from module.application.notifications import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryResultClass,
    DeliveryState,
    HandoverPreemptionPayload,
    NotificationEvent,
    NotificationPolicy,
    NotificationPublisher,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    PolicyAction,
    PolicyState,
    PublishStatus,
    ReceiptStrength,
    RetryPolicy,
)
from module.application.notifications.encoding import event_payload_digest
from module.application.notifications.models import (
    ClaimedDelivery,
    DeliveryUpdate,
    NotificationAgentAck,
    NotificationAgentAckResult,
    NotificationAgentAckStatus,
    NotificationAgentDelivery,
    NotificationDeliveryPlan,
    NotificationEventProjection,
    NotificationPersistenceResult,
    NotificationStoredAttempt,
    NotificationStoredDelivery,
    NotificationStoredEvent,
    PreparedDelivery,
)
from module.application.notifications.state import expired_lease_update

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _event(*, event_id: UUID | None = None, operation_id: str = "op-1") -> NotificationEvent:
    return NotificationEvent(
        id=event_id or uuid4(),
        source="runtime",
        type="runtime.handover.preemption_requested",
        schema_version=1,
        profile_id="profile-1",
        severity=NotificationSeverity.CRITICAL,
        occurred_at=NOW,
        data=HandoverPreemptionPayload(
            operation_id=operation_id,
            source_profile_id="profile-1",
            owner_epoch=3,
            reason_code="busy_handover",
            deadline_at=NOW + timedelta(seconds=30),
        ),
        dedup_key=operation_id,
    )


def _policy(
    channel: str = "agent",
    *,
    channel_instance_ids: tuple[str, ...] | None = None,
) -> NotificationPolicy:
    return NotificationPolicy(
        version=4,
        rules=(
            NotificationRule(
                rule_id="handover",
                priority=1,
                matcher=NotificationRuleMatcher(
                    exact_type="runtime.handover.preemption_requested"
                ),
                action=PolicyAction(
                    channel_instance_ids=channel_instance_ids or (channel,),
                    locale="ru-RU",
                ),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )


def _publish(publisher: NotificationPublisher, event: NotificationEvent):
    return publisher.publish_for_handover(
        event, NOW + timedelta(seconds=30)
    ).publish_result


class _FakeChannel:
    instance_id = "agent"
    channel_type = "test"
    capabilities = ChannelCapabilities(
        receipt_strength=ReceiptStrength.AGENT_ACK,
        policy_capabilities=frozenset({"handover_receipt"}),
    )

    def __init__(self, result: DeliveryResult) -> None:
        self.result = result
        self.sent: list[PreparedDelivery] = []

    def send(self, prepared: PreparedDelivery) -> DeliveryResult:
        self.sent.append(prepared)
        return self.result


class _FlakyCapabilitiesChannel:
    instance_id = "agent-flaky"
    channel_type = "test"

    def __init__(self) -> None:
        self._capabilities = ChannelCapabilities(
            receipt_strength=ReceiptStrength.AGENT_ACK,
            policy_capabilities=frozenset({"handover_receipt"}),
        )
        self.fail_capabilities = False
        self.sent: list[PreparedDelivery] = []

    @property
    def capabilities(self) -> ChannelCapabilities:
        if self.fail_capabilities:
            raise RuntimeError("Не удалось прочитать channel capabilities.")
        return self._capabilities

    def send(self, prepared: PreparedDelivery) -> DeliveryResult:
        self.sent.append(prepared)
        return DeliveryResult.delivered()


class _FailingSpan:
    def __init__(self, *, enter_failure: bool, exit_failure: bool) -> None:
        self.enter_failure = enter_failure
        self.exit_failure = exit_failure

    def __enter__(self) -> Self:
        if self.enter_failure:
            raise RuntimeError("telemetry enter failed")
        return self

    def __exit__(self, *_args: object) -> None:
        if self.exit_failure:
            raise RuntimeError("telemetry exit failed")


class _FailingTelemetry:
    def __init__(self, *, enter_failure: bool = False, exit_failure: bool = False) -> None:
        self.enter_failure = enter_failure
        self.exit_failure = exit_failure

    def span(self, *_args: object, **_kwargs: object) -> _FailingSpan:
        return _FailingSpan(
            enter_failure=self.enter_failure,
            exit_failure=self.exit_failure,
        )


class _MemoryUow:
    def __init__(self, repository: _MemoryRepository) -> None:
        self.notifications = repository
        self.commit_count = 0
        self.rollback_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class _MemoryRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self.events: dict[tuple[str, UUID], NotificationStoredEvent] = {}
        self.decisions: dict[tuple[str, UUID], object] = {}
        self.deliveries: dict[UUID, NotificationStoredDelivery] = {}
        self.attempts: dict[UUID, list[NotificationStoredAttempt]] = {}
        self.agent_acks: dict[tuple[UUID, int], NotificationAgentAck] = {}
        self.claim_batch_sizes: list[int] = []
        self._sequence = 0

    def find_existing(
        self, event: NotificationEvent, *, payload_digest: str
    ) -> NotificationPersistenceResult | None:
        with self._lock:
            existing = self.events.get((event.source, event.id))
            if existing is None and event.dedup_key is not None:
                existing = next(
                    (
                        item
                        for item in self.events.values()
                        if item.event.source == event.source
                        and item.event.profile_id == event.profile_id
                        and item.event.type == event.type
                        and item.event.dedup_key == event.dedup_key
                    ),
                    None,
                )
            if existing is None:
                return None
            status = (
                PublishStatus.DUPLICATE
                if existing.event.payload_digest == payload_digest
                else PublishStatus.IDENTITY_CONFLICT
            )
            return NotificationPersistenceResult(
                status=status,
                event=existing,
                decision=self.decisions[(existing.event.source, existing.event.id)],
                deliveries=tuple(
                    item
                    for item in self.deliveries.values()
                    if item.event_source == existing.event.source
                    and item.event_id == existing.event.id
                ),
                reason=None
                if status is PublishStatus.DUPLICATE
                else "immutable_identity_mismatch",
            )

    def publish(
        self,
        event: NotificationEvent,
        *,
        payload_document: dict[str, object],
        decision: object,
        deliveries: tuple[NotificationDeliveryPlan, ...],
    ) -> NotificationPersistenceResult:
        payload_digest = event_payload_digest(event, payload_document)
        with self._lock:
            existing = self.find_existing(event, payload_digest=payload_digest)
            if existing is not None:
                return existing
            self._sequence += 1
            stored_event = event.with_persistence(
                persisted_at=NOW,
                profile_sequence=self._sequence,
                payload_digest=payload_digest,
            )
            stored = NotificationStoredEvent(stored_event, payload_document)
            event_key = (event.source, event.id)
            self.events[event_key] = stored
            self.decisions[event_key] = decision
            for plan in deliveries:
                self.deliveries[plan.id] = NotificationStoredDelivery(
                    id=plan.id,
                    event_id=event.id,
                    channel_instance_id=plan.channel_instance_id,
                    channel_type=plan.channel_type,
                    state=DeliveryState.PENDING,
                    priority=plan.priority,
                    created_at=NOW,
                    next_attempt_at=plan.next_attempt_at,
                    deadline_at=plan.deadline_at,
                    attempt_count=0,
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    last_safe_error_code=None,
                    rendered_snapshot=plan.rendered_snapshot,
                    idempotency_key=plan.idempotency_key,
                    updated_at=NOW,
                    event_source=event.source,
                )
                self.attempts[plan.id] = []
            return NotificationPersistenceResult(
                status=(
                    PublishStatus.SUPPRESSED
                    if getattr(decision, "state", None) is PolicyState.SUPPRESSED
                    else PublishStatus.PERSISTED
                ),
                event=stored,
                decision=decision,
                deliveries=tuple(self.deliveries[item.id] for item in deliveries),
            )

    def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
    ) -> tuple[ClaimedDelivery, ...]:
        with self._lock:
            self.claim_batch_sizes.append(batch_size)
            claimed: list[ClaimedDelivery] = []
            ordered = sorted(
                self.deliveries.values(),
                key=lambda item: (
                    -item.priority,
                    item.next_attempt_at,
                    item.created_at,
                    item.id.hex,
                ),
            )
            for delivery in ordered:
                if len(claimed) >= batch_size:
                    break
                if delivery.state not in {DeliveryState.PENDING, DeliveryState.RETRY_WAIT}:
                    continue
                if delivery.deadline_at is not None and delivery.deadline_at <= now:
                    result = DeliveryResult.permanent_failure("delivery_deadline_expired")
                    self.deliveries[delivery.id] = replace(
                        delivery,
                        state=DeliveryState.FAILED,
                        next_attempt_at=now,
                        last_safe_error_code=result.safe_error_code,
                        updated_at=now,
                    )
                    continue
                if delivery.next_attempt_at > now:
                    continue
                token = uuid4()
                ordinal = delivery.attempt_count + 1
                lease_until = now + timedelta(seconds=lease_seconds)
                if delivery.deadline_at is not None:
                    lease_until = min(lease_until, delivery.deadline_at)
                updated = replace(
                    delivery,
                    state=DeliveryState.IN_FLIGHT,
                    attempt_count=ordinal,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_until=lease_until,
                    updated_at=now,
                )
                self.deliveries[delivery.id] = updated
                self.attempts[delivery.id].append(
                    NotificationStoredAttempt(
                        delivery_id=delivery.id,
                        attempt_ordinal=ordinal,
                        started_at=now,
                        finished_at=None,
                        result_class=None,
                        safe_error_code=None,
                        safe_error_summary=None,
                        retry_after_seconds=None,
                        provider_message_id=None,
                        lease_token=token,
                    )
                )
                event = self.events[(delivery.event_source, delivery.event_id)]
                claimed.append(
                    ClaimedDelivery(
                        delivery=updated,
                        event=event,
                        attempt_ordinal=ordinal,
                        lease_token=token,
                        prepared=PreparedDelivery(
                            delivery_id=delivery.id,
                            event=NotificationEventProjection(
                                id=event.event.id,
                                source=event.event.source,
                                type=event.event.type,
                                schema_version=event.event.schema_version,
                                profile_id=event.event.profile_id,
                                severity=event.event.severity,
                                occurred_at=event.event.occurred_at,
                                data=event.event.data,
                                subject=event.event.subject,
                                sensitivity=event.event.sensitivity,
                            ),
                            rendered_snapshot=delivery.rendered_snapshot,
                            idempotency_key=delivery.idempotency_key,
                            timeout_seconds=(lease_until - now).total_seconds(),
                        ),
                    )
                )
            return tuple(claimed)

    def apply_update(
        self,
        *,
        delivery_id: UUID,
        lease_token: UUID,
        delivery_update: DeliveryUpdate,
    ) -> bool:
        with self._lock:
            delivery = self.deliveries[delivery_id]
            if delivery.lease_token != lease_token or delivery.state is not DeliveryState.IN_FLIGHT:
                return False
            self.deliveries[delivery_id] = replace(
                delivery,
                state=delivery_update.state,
                next_attempt_at=delivery_update.next_attempt_at,
                lease_owner=None,
                lease_until=delivery_update.lease_until,
                lease_token=(
                    lease_token
                    if delivery_update.state is DeliveryState.AWAITING_AGENT_ACK
                    else None
                ),
                last_safe_error_code=delivery_update.result.safe_error_code,
                updated_at=delivery_update.completed_at or delivery_update.next_attempt_at,
            )
            current = self.attempts[delivery_id][-1]
            self.attempts[delivery_id][-1] = replace(
                current,
                finished_at=delivery_update.completed_at or delivery_update.next_attempt_at,
                result_class=delivery_update.result.result_class,
                safe_error_code=delivery_update.result.safe_error_code,
            )
            return True

    def recover_expired(
        self,
        *,
        now: datetime,
        batch_size: int,
        worker_id: str,
        retry_policy: RetryPolicy,
    ) -> int:
        del worker_id
        with self._lock:
            expired = tuple(
                delivery
                for delivery in self.deliveries.values()
                if delivery.state
                in {DeliveryState.IN_FLIGHT, DeliveryState.AWAITING_AGENT_ACK}
                and delivery.lease_until is not None
                and delivery.lease_until <= now
            )[:batch_size]
            recovered = 0
            for delivery in expired:
                previous_state = delivery.state
                transition = expired_lease_update(
                    state=previous_state,
                    now=now,
                    attempt_count=delivery.attempt_count,
                    deadline_at=delivery.deadline_at,
                    retry_policy=retry_policy,
                    idempotency_key=delivery.idempotency_key,
                )
                if previous_state is DeliveryState.AWAITING_AGENT_ACK:
                    if delivery.lease_token is None:
                        raise AssertionError("ACK wait must retain active token")
                else:
                    current = self.attempts[delivery.id][-1]
                    self.attempts[delivery.id][-1] = replace(
                        current,
                        finished_at=now,
                        result_class=transition.result.result_class,
                        safe_error_code=transition.result.safe_error_code,
                    )
                self.deliveries[delivery.id] = replace(
                    delivery,
                    state=transition.state,
                    next_attempt_at=transition.next_attempt_at,
                    attempt_count=delivery.attempt_count,
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    last_safe_error_code=transition.result.safe_error_code,
                    updated_at=now,
                )
                recovered += 1
            return recovered

    def list_deliveries(
        self, *, source: str, event_id: UUID
    ) -> tuple[NotificationStoredDelivery, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        delivery
                        for delivery in self.deliveries.values()
                        if delivery.event_source == source
                        and delivery.event_id == event_id
                    ),
                    key=lambda item: item.id.hex,
                )
            )

    def list_agent_deliveries(
        self,
        *,
        profile_id: str,
        channel_instance_id: str,
        after_sequence: int = 0,
        after_event_id: UUID | None = None,
        limit: int = 32,
    ) -> tuple[NotificationAgentDelivery, ...]:
        with self._lock:
            result: list[NotificationAgentDelivery] = []
            for delivery in sorted(
                self.deliveries.values(),
                key=lambda item: (
                    self.events[(item.event_source, item.event_id)].event.profile_sequence or 0,
                    item.event_id.hex,
                ),
            ):
                event = self.events[(delivery.event_source, delivery.event_id)]
                sequence = event.event.profile_sequence or 0
                if (
                    event.event.profile_id != profile_id
                    or delivery.channel_instance_id != channel_instance_id
                    or delivery.channel_type != "desktop-agent"
                    or delivery.state is not DeliveryState.AWAITING_AGENT_ACK
                    or delivery.lease_token is None
                    or delivery.lease_until is None
                    or (
                        (
                            sequence <= after_sequence
                            if after_event_id is None
                            else sequence < after_sequence
                        )
                        or (
                            after_event_id is not None
                            and sequence == after_sequence
                            and event.event.id <= after_event_id
                        )
                    )
                ):
                    continue
                attempt = self.attempts[delivery.id][-1]
                if (
                    attempt.attempt_ordinal != delivery.attempt_count
                    or attempt.lease_token != delivery.lease_token
                    or attempt.result_class is not DeliveryResultClass.PROVIDER_ACCEPTED
                ):
                    continue
                result.append(NotificationAgentDelivery(event, delivery, attempt))
                if len(result) >= limit:
                    break
            return tuple(result)

    def acknowledge_agent_delivery(
        self, ack: NotificationAgentAck, *, now: datetime
    ) -> NotificationAgentAckResult:
        with self._lock:
            if ack.session_epoch != ack.lease_token:
                raise StorageInvariantViolationError(
                    "Agent ACK session epoch не совпадает с lease."
                )
            key = (ack.delivery_id, ack.attempt_ordinal)
            previous = self.agent_acks.get(key)
            if previous is not None:
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.DUPLICATE
                    if previous == ack
                    else NotificationAgentAckStatus.REJECTED,
                    None if previous == ack else "ack_identity_conflict",
                )
            delivery = self.deliveries.get(ack.delivery_id)
            if delivery is None:
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.REJECTED, "delivery_not_found"
                )
            if delivery.state is DeliveryState.DELIVERED:
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.REJECTED, "delivery_already_completed"
                )
            event = self.events[(delivery.event_source, delivery.event_id)].event
            identity_checks = (
                (ack.event_id != event.id, "event_identity_mismatch"),
                (ack.event_source != event.source, "event_identity_mismatch"),
                (ack.profile_id != event.profile_id, "profile_identity_mismatch"),
                (delivery.channel_type != "desktop-agent", "channel_identity_mismatch"),
                (ack.attempt_ordinal != delivery.attempt_count, "attempt_identity_mismatch"),
                (ack.lease_token != delivery.lease_token, "lease_identity_mismatch"),
                (ack.payload_digest != event.payload_digest, "payload_digest_mismatch"),
            )
            for mismatch, reason in identity_checks:
                if mismatch:
                    return NotificationAgentAckResult(
                        NotificationAgentAckStatus.REJECTED, reason
                    )
            if delivery.state is not DeliveryState.AWAITING_AGENT_ACK:
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.REJECTED, "delivery_not_awaiting_ack"
                )
            if (
                delivery.lease_until is None
                or delivery.lease_until <= now
                or (
                    delivery.deadline_at is not None
                    and delivery.deadline_at <= now
                )
            ):
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.REJECTED, "ack_expired"
                )
            attempt = self.attempts[delivery.id][-1]
            if (
                attempt.attempt_ordinal != ack.attempt_ordinal
                or attempt.lease_token != ack.lease_token
                or attempt.result_class is not DeliveryResultClass.PROVIDER_ACCEPTED
            ):
                return NotificationAgentAckResult(
                    NotificationAgentAckStatus.REJECTED, "attempt_not_awaiting_ack"
                )
            self.agent_acks[key] = ack
            self.deliveries[ack.delivery_id] = replace(
                delivery,
                state=DeliveryState.DELIVERED,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                last_safe_error_code=None,
                next_attempt_at=now,
                updated_at=now,
            )
            return NotificationAgentAckResult(NotificationAgentAckStatus.ACKNOWLEDGED)


class _FailingApplyRepository(_MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_update = True

    def apply_update(
        self,
        *,
        delivery_id: UUID,
        lease_token: UUID,
        delivery_update: DeliveryUpdate,
    ) -> bool:
        if self.fail_next_update:
            self.fail_next_update = False
            raise RuntimeError("Ошибка применения notification update.")
        return super().apply_update(
            delivery_id=delivery_id,
            lease_token=lease_token,
            delivery_update=delivery_update,
        )


__all__ = [
    "NOW",
    "_FailingApplyRepository",
    "_FailingTelemetry",
    "_FakeChannel",
    "_FlakyCapabilitiesChannel",
    "_MemoryRepository",
    "_MemoryUow",
    "_event",
    "_policy",
    "_publish",
]
