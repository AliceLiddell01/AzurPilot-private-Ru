"""Unit-контракты typed notification policy и dispatcher state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Self
from uuid import UUID, uuid4

import pytest

from module.application.notifications import (
    ChannelCapabilities,
    DeferredNotificationPayload,
    DeliveryResult,
    DeliveryState,
    HandoverNotificationOutcome,
    HandoverPreemptionPayload,
    NotificationChannelCatalog,
    NotificationDispatcher,
    NotificationEvent,
    NotificationPolicy,
    NotificationPolicyResolver,
    NotificationPublisher,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    NotificationValidationError,
    PolicyAction,
    PolicyState,
    PublishStatus,
    ReceiptStrength,
    RetryPolicy,
    build_default_registry,
)
from module.application.notifications.encoding import canonical_json, event_payload_digest
from module.application.notifications.models import (
    ClaimedDelivery,
    DeliveryUpdate,
    NotificationDeliveryPlan,
    NotificationEventProjection,
    NotificationPersistenceResult,
    NotificationStoredAttempt,
    NotificationStoredDelivery,
    NotificationStoredEvent,
    PreparedDelivery,
)
from module.application.notifications.telemetry import safe_telemetry_span

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


def _policy(channel: str = "agent") -> NotificationPolicy:
    return NotificationPolicy(
        version=4,
        rules=(
            NotificationRule(
                rule_id="handover",
                priority=1,
                matcher=NotificationRuleMatcher(
                    exact_type="runtime.handover.preemption_requested"
                ),
                action=PolicyAction(channel_instance_ids=(channel,), locale="ru-RU"),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )


class _FakeChannel:
    instance_id = "agent"
    channel_type = "test"
    capabilities = ChannelCapabilities(receipt_strength=ReceiptStrength.AGENT_ACK)

    def __init__(self, result: DeliveryResult) -> None:
        self.result = result
        self.sent: list[PreparedDelivery] = []

    def send(self, prepared: PreparedDelivery) -> DeliveryResult:
        self.sent.append(prepared)
        return self.result


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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _MemoryRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self.events: dict[UUID, NotificationStoredEvent] = {}
        self.decisions: dict[UUID, object] = {}
        self.deliveries: dict[UUID, NotificationStoredDelivery] = {}
        self.attempts: dict[UUID, list[NotificationStoredAttempt]] = {}
        self._sequence = 0

    def publish(
        self,
        event: NotificationEvent,
        *,
        payload_document: dict[str, object],
        payload_digest: str,
        decision: object,
        deliveries: tuple[NotificationDeliveryPlan, ...],
    ) -> NotificationPersistenceResult:
        with self._lock:
            existing = self.events.get(event.id)
            if existing is not None:
                status = (
                    PublishStatus.DUPLICATE
                    if existing.event.payload_digest == payload_digest
                    else PublishStatus.IDENTITY_CONFLICT
                )
                return NotificationPersistenceResult(
                    status=status,
                    event=existing,
                    decision=self.decisions[event.id],
                    deliveries=tuple(
                        item for item in self.deliveries.values() if item.event_id == event.id
                    ),
                    reason=None if status is PublishStatus.DUPLICATE else "immutable_identity_mismatch",
                )
            self._sequence += 1
            stored_event = event.with_persistence(
                persisted_at=NOW,
                profile_sequence=self._sequence,
                payload_digest=payload_digest,
            )
            stored = NotificationStoredEvent(stored_event, payload_document)
            self.events[event.id] = stored
            self.decisions[event.id] = decision
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
            claimed: list[ClaimedDelivery] = []
            for delivery in tuple(self.deliveries.values()):
                if len(claimed) >= batch_size or delivery.state not in {
                    DeliveryState.PENDING,
                    DeliveryState.RETRY_WAIT,
                } or delivery.next_attempt_at > now:
                    continue
                token = uuid4()
                ordinal = delivery.attempt_count + 1
                updated = replace(
                    delivery,
                    state=DeliveryState.IN_FLIGHT,
                    attempt_count=ordinal,
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
                self.deliveries[delivery.id] = updated
                self.attempts[delivery.id].append(
                    NotificationStoredAttempt(delivery.id, ordinal, now, None, None, None, None, None, None, token)
                )
                event = self.events[delivery.event_id]
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
                            timeout_seconds=30,
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
                lease_token=None,
                lease_until=delivery_update.lease_until,
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

    def recover_expired(self, **_kwargs: object) -> int:
        return 0


def test_registry_rejects_naive_and_canonicalizes_handover_payload() -> None:
    registry = build_default_registry()
    event = _event()
    _, document, digest = registry.validate(event)
    assert document["deadline_at"] == "2026-09-10T12:00:30+00:00"
    assert digest == event_payload_digest(event, document)

    invalid = replace(event, occurred_at=datetime(2026, 9, 10, 12, 0))  # noqa: DTZ001 - проверка rejection naive time.
    with pytest.raises(NotificationValidationError) as error:
        registry.validate(invalid)
    assert error.value.reason_code == "occurred_at_not_aware"


def test_policy_is_first_matching_rule_and_snapshot_is_stable() -> None:
    event = _event()
    resolver = NotificationPolicyResolver(_policy())
    decision = resolver.resolve(event)
    assert decision.state is PolicyState.ROUTED
    assert decision.matched_rule_id == "handover"
    assert resolver.resolve(event).snapshot_hash == decision.snapshot_hash


def test_policy_severity_matcher_is_typed_and_safe() -> None:
    policy = NotificationPolicy(
        version=1,
        rules=(
            NotificationRule(
                rule_id="critical-only",
                priority=1,
                matcher=NotificationRuleMatcher(
                    minimum_severity=NotificationSeverity.ERROR
                ),
                action=PolicyAction(channel_instance_ids=("agent",)),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )
    resolver = NotificationPolicyResolver(policy)

    assert resolver.resolve(_event()).matched_rule_id == "critical-only"
    assert not NotificationRuleMatcher(
        minimum_severity=NotificationSeverity.ERROR
    ).matches(replace(_event(), severity="CRITICAL"))


def test_canonical_payload_rejects_non_finite_decimal_and_deep_nesting() -> None:
    with pytest.raises(NotificationValidationError) as decimal_error:
        canonical_json(Decimal("NaN"))
    assert decimal_error.value.reason_code == "payload_non_finite_number"

    nested: object = "leaf"
    for _ in range(17):
        nested = {"value": nested}
    with pytest.raises(NotificationValidationError) as depth_error:
        canonical_json(nested)
    assert depth_error.value.reason_code == "payload_too_deep"


def test_provider_acceptance_never_becomes_delivered() -> None:
    from module.application.notifications.state import transition_for_result

    accepted = transition_for_result(
        DeliveryResult.provider_accepted(provider_message_id="provider-1"),
        capabilities=ChannelCapabilities(receipt_strength=ReceiptStrength.AGENT_ACK),
        attempt_count=1,
        now=NOW,
        deadline_at=NOW + timedelta(seconds=30),
        retry_policy=RetryPolicy(max_attempts=2),
        idempotency_key="delivery-1",
    )
    assert accepted.state is DeliveryState.AWAITING_AGENT_ACK
    assert accepted.state is not DeliveryState.DELIVERED


def test_delivery_result_rejects_unsafe_provider_data() -> None:
    assert not DeliveryResult.transient_failure(
        "channel_failed", summary="password=redacted"
    ).is_valid()
    assert not DeliveryResult.provider_accepted(
        provider_message_id="https://provider.example/message"
    ).is_valid()


def test_publisher_and_dispatcher_keep_provider_acceptance_intermediate() -> None:
    repository = _MemoryRepository()
    channel = _FakeChannel(DeliveryResult.provider_accepted(provider_message_id="provider-1"))
    channels = NotificationChannelCatalog((channel,))
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        clock=lambda: NOW,
    )
    result = publisher.publish(_event())
    assert result.status is PublishStatus.PERSISTED
    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=channels,
        clock=lambda: NOW,
        worker_id="worker-1",
    )
    report = dispatcher.dispatch_once()
    assert report.updated == 1
    delivery = next(iter(repository.deliveries.values()))
    assert delivery.state is DeliveryState.AWAITING_AGENT_ACK
    assert channel.sent[0].idempotency_key.startswith(str(result.event_id))


@pytest.mark.parametrize(
    ("enter_failure", "exit_failure"),
    ((True, False), (False, True)),
)
def test_dispatcher_telemetry_lifecycle_is_fail_open(
    enter_failure: bool, exit_failure: bool
) -> None:
    repository = _MemoryRepository()
    NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        clock=lambda: NOW,
        telemetry=object(),
    ).publish(_event())
    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((_FakeChannel(DeliveryResult.delivered()),)),
        clock=lambda: NOW,
        worker_id="worker-telemetry",
        telemetry=_FailingTelemetry(
            enter_failure=enter_failure,
            exit_failure=exit_failure,
        ),
    )

    report = dispatcher.dispatch_once()

    assert report.updated == 1


def test_telemetry_exit_failure_does_not_hide_notification_error() -> None:
    with pytest.raises(RuntimeError, match="notification failed"):
        with safe_telemetry_span(
            _FailingTelemetry(exit_failure=True), "notification.test"
        ):
            raise RuntimeError("notification failed")


def test_handover_result_accepted_is_not_delivery_proof() -> None:
    repository = _MemoryRepository()
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog((_FakeChannel(DeliveryResult.unavailable()),)),
        clock=lambda: NOW,
    )
    result = publisher.publish_for_handover(_event(), NOW + timedelta(seconds=30))
    assert result.outcome is HandoverNotificationOutcome.ACCEPTED
    assert not result.is_proof


def test_deferred_taxonomy_descriptor_rejects_publish_without_generic_payload() -> None:
    registry = build_default_registry()
    event = replace(
        _event(),
        type="task.completed",
        severity=NotificationSeverity.INFO,
        data=DeferredNotificationPayload(),
        dedup_key=None,
    )

    with pytest.raises(NotificationValidationError) as error:
        registry.validate(event)

    assert error.value.reason_code == "producer_schema_not_migrated"


def test_global_suppression_is_durable_without_delivery() -> None:
    repository = _MemoryRepository()
    policy = NotificationPolicy(
        version=2,
        rules=(),
        default_action=PolicyAction(channel_instance_ids=("agent",)),
        global_enabled=False,
    )
    result = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=policy,
        clock=lambda: NOW,
    ).publish(_event())

    assert result.status is PublishStatus.SUPPRESSED
    assert result.decision is not None
    assert result.decision.state is PolicyState.SUPPRESSED
    assert result.decision.reason == "global_disabled"
    assert result.deliveries == ()
    assert repository.events[result.event_id].event.profile_sequence == 1


def test_rule_suppression_is_durable_without_delivery() -> None:
    repository = _MemoryRepository()
    policy = NotificationPolicy(
        version=3,
        rules=(
            NotificationRule(
                rule_id="suppress-handover",
                priority=1,
                matcher=NotificationRuleMatcher(
                    exact_type="runtime.handover.preemption_requested"
                ),
                action=PolicyAction(suppression_reason="maintenance"),
            ),
        ),
        default_action=PolicyAction(channel_instance_ids=("agent",)),
    )
    result = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=policy,
        clock=lambda: NOW,
    ).publish(_event())

    assert result.status is PublishStatus.SUPPRESSED
    assert result.decision is not None
    assert result.decision.reason == "maintenance"
    assert result.deliveries == ()
