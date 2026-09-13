"""Dispatcher claim/lease/attempt и telemetry contracts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from module.application.notifications import (
    DeliveryResult,
    DeliveryState,
    DeliveryUpdate,
    NotificationChannelCatalog,
    NotificationDispatcher,
    NotificationPublisher,
    RetryPolicy,
)
from module.application.notifications.telemetry import safe_telemetry_span
from tests.support.notifications import (
    NOW,
    _event,
    _FailingApplyRepository,
    _FailingTelemetry,
    _FakeChannel,
    _FlakyCapabilitiesChannel,
    _MemoryRepository,
    _MemoryUow,
    _policy,
    _publish,
)


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.retry_scheduled: list[bool] = []

    def record_attempt(
        self,
        *,
        claimed: object,
        result: DeliveryResult,
        retry_scheduled: bool,
    ) -> None:
        del claimed, result
        self.retry_scheduled.append(retry_scheduled)

    def record_latency(self, **_kwargs: object) -> None:
        return None


@pytest.mark.parametrize("worker_id", ("", "worker id", "x" * 129))
def test_dispatcher_rejects_invalid_worker_id(worker_id: str) -> None:
    with pytest.raises(ValueError, match="worker id"):
        NotificationDispatcher(lambda: _MemoryUow(_MemoryRepository()), worker_id=worker_id)


def test_memory_recovery_fences_stale_lease_token() -> None:
    repository = _MemoryRepository()
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.unavailable()),)
        ),
        clock=lambda: NOW,
    )
    result = _publish(publisher, _event())
    claimed = repository.claim_due(
        now=NOW,
        worker_id="worker-claim",
        batch_size=1,
        lease_seconds=5,
    )[0]
    stale_token = claimed.lease_token

    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        worker_id="worker-recovery",
        clock=lambda: NOW + timedelta(seconds=6),
        retry_policy=RetryPolicy(),
    )
    assert dispatcher.recover_expired() == 1
    assert (
        repository.deliveries[next(iter(repository.deliveries))].state
        is DeliveryState.RETRY_WAIT
    )
    assert not repository.apply_update(
        delivery_id=claimed.delivery.id,
        lease_token=stale_token,
        delivery_update=DeliveryUpdate(
            state=DeliveryState.DELIVERED,
            result=DeliveryResult.delivered(),
            next_attempt_at=NOW + timedelta(seconds=6),
            completed_at=NOW + timedelta(seconds=6),
        ),
    )
    assert ("runtime", result.event_id) in repository.events


def test_publisher_and_dispatcher_keep_provider_acceptance_intermediate() -> None:
    repository = _MemoryRepository()
    channel = _FakeChannel(
        DeliveryResult.provider_accepted(provider_message_id="provider-1")
    )
    channels = NotificationChannelCatalog((channel,))
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=channels,
        clock=lambda: NOW,
    )
    result = _publish(publisher, _event())
    assert result.status.value == "persisted"
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
    assert channel.sent[0].idempotency_key.startswith("notification-delivery-v1:")


def test_memory_ack_timeout_keeps_real_attempt_budget() -> None:
    repository = _MemoryRepository()
    channel = _FakeChannel(
        DeliveryResult.provider_accepted(provider_message_id="provider-ack")
    )
    retry_policy = RetryPolicy(max_attempts=2, agent_ack_timeout_seconds=5)
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        clock=lambda: NOW,
    )
    result = _publish(publisher, _event(operation_id="operation-memory-ack"))
    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-memory-ack",
        retry_policy=retry_policy,
        clock=lambda: NOW,
    )
    assert dispatcher.dispatch_once().updated == 1
    first = next(iter(repository.deliveries.values()))
    first_token = first.lease_token
    assert first.state is DeliveryState.AWAITING_AGENT_ACK
    assert first.attempt_count == 1
    assert first_token is not None

    recovery = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-memory-recovery",
        retry_policy=retry_policy,
        clock=lambda: NOW + timedelta(seconds=6),
    )
    assert recovery.recover_expired() == 1
    recovered = repository.deliveries[first.id]
    assert recovered.state is DeliveryState.RETRY_WAIT
    assert recovered.attempt_count == 1
    assert recovered.lease_token is None
    assert len(repository.attempts[first.id]) == 1

    second = repository.claim_due(
        now=NOW + timedelta(seconds=10),
        worker_id="worker-memory-retry",
        batch_size=1,
        lease_seconds=30,
    )[0]
    assert second.lease_token != first_token
    assert second.attempt_ordinal == 2
    assert second.delivery.attempt_count == 2
    assert len(repository.attempts[first.id]) == 2
    assert result.event_id == second.event.event.id


def test_dispatcher_isolates_channel_capability_failure_within_batch() -> None:
    repository = _MemoryRepository()
    flaky = _FlakyCapabilitiesChannel()
    healthy = _FakeChannel(DeliveryResult.delivered())
    healthy.instance_id = "agent-healthy"
    channels = NotificationChannelCatalog((flaky, healthy))
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(channel_instance_ids=(flaky.instance_id, healthy.instance_id)),
        channel_catalog=channels,
        clock=lambda: NOW,
    )
    _publish(publisher, _event(operation_id="operation-capabilities"))
    flaky.fail_capabilities = True

    report = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=channels,
        clock=lambda: NOW,
        worker_id="worker-capabilities",
        batch_size=2,
    ).dispatch_once()

    assert report.claimed == 2
    assert report.updated == 2
    assert report.failed == 1
    assert flaky.sent == []
    assert len(healthy.sent) == 1


def test_dispatcher_claims_each_delivery_with_its_own_lease_window() -> None:
    repository = _MemoryRepository()
    channel = _FakeChannel(DeliveryResult.delivered())
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog((channel,)),
        clock=lambda: NOW,
    )
    _publish(publisher, _event(operation_id="operation-lease-first"))
    _publish(publisher, _event(operation_id="operation-lease-second"))

    report = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        clock=lambda: NOW,
        worker_id="worker-lease-window",
        batch_size=2,
        lease_seconds=5,
    ).dispatch_once()

    assert report.processed == 2
    assert repository.claim_batch_sizes == [1, 1]


def test_dispatcher_continues_after_storage_update_failure() -> None:
    repository = _FailingApplyRepository()
    channel = _FakeChannel(DeliveryResult.delivered())
    channels = NotificationChannelCatalog((channel,))
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=channels,
        clock=lambda: NOW,
    )
    _publish(publisher, _event(operation_id="operation-storage-first"))
    _publish(publisher, _event(operation_id="operation-storage-second"))

    report = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=channels,
        clock=lambda: NOW,
        worker_id="worker-storage",
        batch_size=2,
    ).dispatch_once()

    assert report.claimed == 2
    assert report.updated == 1
    assert report.failed == 1
    assert len(channel.sent) == 2


def test_retry_metric_follows_durable_retry_transition() -> None:
    repository = _MemoryRepository()
    telemetry = _RecordingTelemetry()
    channel = _FakeChannel(DeliveryResult.transient_failure("temporary"))
    retry_policy = RetryPolicy(max_attempts=2, jitter_ratio=0)
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        clock=lambda: NOW,
    )
    _publish(publisher, _event(operation_id="operation-retry-telemetry"))

    first_dispatch = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        worker_id="worker-retry-first",
        clock=lambda: NOW,
        telemetry=telemetry,
    )
    assert first_dispatch.dispatch_once().updated == 1

    second_dispatch = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        worker_id="worker-retry-second",
        clock=lambda: NOW + timedelta(seconds=10),
        telemetry=telemetry,
    )
    assert second_dispatch.dispatch_once().updated == 1

    assert telemetry.retry_scheduled == [True, False]


def test_retry_metric_does_not_count_deadline_exhaustion() -> None:
    repository = _MemoryRepository()
    telemetry = _RecordingTelemetry()
    channel = _FakeChannel(DeliveryResult.transient_failure("temporary"))
    retry_policy = RetryPolicy(max_attempts=5, base_delay_seconds=2, jitter_ratio=0)
    publisher = NotificationPublisher(
        lambda: _MemoryUow(repository),
        policy=_policy(),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        clock=lambda: NOW,
    )
    _publish(publisher, _event(operation_id="operation-deadline-telemetry"))

    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog((channel,)),
        retry_policy=retry_policy,
        worker_id="worker-deadline",
        clock=lambda: NOW + timedelta(seconds=29),
        telemetry=telemetry,
    )

    assert dispatcher.dispatch_once().updated == 1
    assert telemetry.retry_scheduled == [False]


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
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.unavailable()),)
        ),
        clock=lambda: NOW,
        telemetry=object(),
    ).publish_for_handover(_event(), NOW + timedelta(seconds=30))
    dispatcher = NotificationDispatcher(
        lambda: _MemoryUow(repository),
        channel_catalog=NotificationChannelCatalog(
            (_FakeChannel(DeliveryResult.delivered()),)
        ),
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
    with pytest.raises(RuntimeError, match="notification failed"), safe_telemetry_span(
        _FailingTelemetry(exit_failure=True), "notification.test"
    ):
        raise RuntimeError("notification failed")
