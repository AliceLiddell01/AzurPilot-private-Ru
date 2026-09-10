"""Контракты publisher boundary, storage mapping и handover publication."""

from __future__ import annotations

from datetime import timedelta

import pytest

from module.application.errors import (
    IncompatibleSchemaError,
    StorageAuthenticationError,
    StorageConfigurationError,
    StorageConflictError,
    StorageInvalidDataError,
    StorageUnavailableError,
)
from module.application.notifications import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryState,
    HandoverNotificationOutcome,
    NotificationChannelCatalog,
    NotificationPersistenceResult,
    NotificationPolicy,
    NotificationPublisher,
    NotificationRule,
    NotificationRuleMatcher,
    PolicyAction,
    PolicyState,
    PublishStatus,
    ReceiptStrength,
    RetryPolicy,
)
from module.application.notifications.state import transition_for_result
from tests.notification_test_support import (
    NOW,
    _event,
    _FakeChannel,
    _MemoryRepository,
    _MemoryUow,
    _policy,
    _publish,
)


def test_publisher_rejects_expired_handover_deadline() -> None:
    repository = _MemoryRepository()
    event = _event()
    from dataclasses import replace

    event = replace(
        event,
        occurred_at=NOW - timedelta(seconds=60),
        data=replace(event.data, deadline_at=NOW - timedelta(seconds=1)),
    )
    result = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        clock=lambda: NOW,
    ).publish_for_handover(event, NOW + timedelta(seconds=30)).publish_result

    assert result.status is PublishStatus.VALIDATION_FAILED
    assert result.reason == "handover_deadline_expired"
    assert repository.events == {}


def test_publisher_commits_durable_bundle_before_return() -> None:
    repository = _MemoryRepository()
    uow = _MemoryUow(repository)
    publisher = NotificationPublisher(
        lambda: uow,
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.unavailable()),)
        ),
        clock=lambda: NOW,
    )

    result = _publish(publisher, _event(operation_id="operation-commit-boundary"))

    assert result.status is PublishStatus.PERSISTED
    assert uow.commit_count == 1
    assert uow.rollback_count == 0


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_reason"),
    (
        (StorageUnavailableError(), PublishStatus.UNAVAILABLE, "storage_unavailable"),
        (
            StorageAuthenticationError(),
            PublishStatus.VALIDATION_FAILED,
            "storage_authentication_failed",
        ),
        (
            StorageConfigurationError(),
            PublishStatus.VALIDATION_FAILED,
            "storage_configuration_invalid",
        ),
        (
            IncompatibleSchemaError(),
            PublishStatus.VALIDATION_FAILED,
            "storage_schema_incompatible",
        ),
        (StorageConflictError(), PublishStatus.VALIDATION_FAILED, "storage_conflict"),
        (
            StorageInvalidDataError(),
            PublishStatus.VALIDATION_FAILED,
            "storage_invalid_data",
        ),
    ),
)
def test_storage_failures_map_to_bounded_publish_result(
    error: Exception, expected_status: PublishStatus, expected_reason: str
) -> None:
    class _FailingRepository(_MemoryRepository):
        def find_existing(
            self, event: object, *, payload_digest: str
        ) -> NotificationPersistenceResult | None:
            del event, payload_digest
            raise error

    result = _publish(
        NotificationPublisher(
            lambda: _MemoryUow(_FailingRepository()),
            policy=_policy(),
            channel_catalog=NotificationChannelCatalog(
                (_FakeChannel(DeliveryResult.delivered()),)
            ),
            clock=lambda: NOW,
        ),
        _event(),
    )

    assert result.status is expected_status
    assert result.reason == expected_reason


def test_generic_publish_cannot_bypass_handover_context() -> None:
    repository = _MemoryRepository()
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.delivered()),)
        ),
        clock=lambda: NOW,
    )

    result = publisher.publish(_event())

    assert result.status is PublishStatus.VALIDATION_FAILED
    assert result.reason == "publish_context_required"
    assert repository.events == {}


def test_handover_capability_is_required_at_publish_boundary() -> None:
    class _NoHandoverCapabilityChannel(_FakeChannel):
        capabilities = ChannelCapabilities(receipt_strength=ReceiptStrength.AGENT_ACK)

    repository = _MemoryRepository()
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog(
            (_NoHandoverCapabilityChannel(DeliveryResult.delivered()),)
        ),
        clock=lambda: NOW,
    )

    result = _publish(publisher, _event())

    assert result.status is PublishStatus.VALIDATION_FAILED
    assert result.reason == "channel_capability_missing"
    assert repository.events == {}


def test_provider_acceptance_never_becomes_delivered() -> None:
    accepted = transition_for_result(
        DeliveryResult.provider_accepted(provider_message_id="provider-1"),
        capabilities=ChannelCapabilities(receipt_strength=ReceiptStrength.AGENT_ACK),
        attempt_count=1,
        now=NOW,
        deadline_at=NOW + timedelta(seconds=30),
        retry_policy=RetryPolicy(max_attempts=2, agent_ack_timeout_seconds=12),
        idempotency_key="delivery-1",
    )
    assert accepted.state is DeliveryState.AWAITING_AGENT_ACK
    assert accepted.state is not DeliveryState.DELIVERED
    assert accepted.lease_until == NOW + timedelta(seconds=12)


def test_retry_policy_rejects_invalid_agent_ack_timeout() -> None:
    with pytest.raises(ValueError, match="agent ACK timeout"):
        RetryPolicy(agent_ack_timeout_seconds=0)


def test_handover_result_accepted_is_not_delivery_proof() -> None:
    repository = _MemoryRepository()
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.unavailable()),)
        ),
        clock=lambda: NOW,
    )
    result = publisher.publish_for_handover(_event(), NOW + timedelta(seconds=30))
    assert result.outcome is HandoverNotificationOutcome.ACCEPTED
    assert not result.is_proof


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
    ).publish_for_handover(_event(), NOW + timedelta(seconds=30)).publish_result

    assert result.status is PublishStatus.SUPPRESSED
    assert result.decision is not None
    assert result.decision.state is PolicyState.SUPPRESSED
    assert result.decision.reason == "global_disabled"
    assert result.deliveries == ()
    assert repository.events[("runtime", result.event_id)].event.profile_sequence == 1


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
    ).publish_for_handover(_event(), NOW + timedelta(seconds=30)).publish_result

    assert result.status is PublishStatus.SUPPRESSED
    assert result.decision is not None
    assert result.decision.reason == "maintenance"
    assert result.deliveries == ()
