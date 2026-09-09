"""Integration- и concurrency-контракты PostgreSQL notification foundation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from module.application.notifications import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryResultClass,
    DeliveryState,
    DeliveryUpdate,
    HandoverPreemptionPayload,
    NotificationChannelCatalog,
    NotificationDispatcher,
    NotificationEvent,
    NotificationPolicy,
    NotificationPublisher,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    PolicyAction,
    PublishStatus,
    ReceiptStrength,
    RetryPolicy,
)
from module.persistence import DatabaseSettings, LazyEngine, PostgresUnitOfWork
from module.persistence.schema import SCHEMA_NAME, metadata

REQUIRED_ENV = (
    "AZURPILOT_POSTGRES_HOST",
    "AZURPILOT_POSTGRES_DATABASE",
    "AZURPILOT_POSTGRES_USER",
)
pytestmark = pytest.mark.skipif(
    any(not os.environ.get(name) for name in REQUIRED_ENV)
    or os.environ.get("AZURPILOT_POSTGRES_DISPOSABLE") != "1",
    reason="требуется явно настроенная disposable PostgreSQL database",
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class _Channel:
    instance_id = "agent"
    channel_type = "test"
    capabilities = ChannelCapabilities(receipt_strength=ReceiptStrength.AGENT_ACK)

    def __init__(self, result: DeliveryResult | None = None) -> None:
        self.result = result or DeliveryResult.provider_accepted(provider_message_id="p1")
        self.calls = 0

    def send(self, _prepared: object) -> DeliveryResult:
        self.calls += 1
        return self.result


@pytest.fixture
def database() -> Iterator[LazyEngine]:
    lazy = LazyEngine(DatabaseSettings.from_environment())
    with lazy.get().begin() as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
    yield lazy
    lazy.dispose()


def _event(
    *,
    event_id: UUID | None = None,
    profile_id: str = "profile-1",
    operation_id: str = "operation-1",
    owner_epoch: int = 1,
) -> NotificationEvent:
    return NotificationEvent(
        id=event_id or uuid4(),
        source="runtime",
        type="runtime.handover.preemption_requested",
        schema_version=1,
        profile_id=profile_id,
        severity=NotificationSeverity.CRITICAL,
        occurred_at=NOW,
        data=HandoverPreemptionPayload(
            operation_id=operation_id,
            source_profile_id=profile_id,
            owner_epoch=owner_epoch,
            reason_code="busy_handover",
            deadline_at=NOW + timedelta(seconds=120),
        ),
        dedup_key=operation_id,
    )


def _publisher(database: LazyEngine, channel: _Channel) -> NotificationPublisher:
    policy = NotificationPolicy(
        version=1,
        rules=(
            NotificationRule(
                rule_id="handover-test",
                priority=1,
                matcher=NotificationRuleMatcher(
                    exact_type="runtime.handover.preemption_requested"
                ),
                action=PolicyAction(channel_instance_ids=("agent",)),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )
    return NotificationPublisher(
        lambda: PostgresUnitOfWork(database),
        policy=policy,
        channel_catalog=NotificationChannelCatalog((channel,)),
        clock=lambda: NOW,
        telemetry=object(),
    )


def test_application_role_has_dml_but_not_schema_ddl(database: LazyEngine) -> None:
    probe_table = f"notification_ddl_probe_{uuid4().hex}"
    with pytest.raises(DBAPIError, match="permission denied"), database.get().begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {SCHEMA_NAME}.{probe_table} "
                "(id integer NOT NULL)"
            )
        )


def test_publish_duplicate_conflict_and_profile_sequence_are_durable(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    publisher = _publisher(database, channel)
    event = _event()

    first = publisher.publish(event)
    duplicate = publisher.publish(event)
    conflict = publisher.publish(
        _event(event_id=uuid4(), operation_id=event.dedup_key or "operation-1", owner_epoch=2)
    )

    assert first.status is PublishStatus.PERSISTED
    assert first.profile_sequence == 1
    assert duplicate.status is PublishStatus.DUPLICATE
    assert duplicate.event_id == first.event_id
    assert conflict.status is PublishStatus.IDENTITY_CONFLICT
    with PostgresUnitOfWork(database) as uow:
        history = uow.notifications.list_profile_history(profile_id="profile-1")
        assert [item.event.profile_sequence for item in history] == [1]
        assert len(uow.notifications.list_deliveries(event.id)) == 1


def test_profile_sequence_allocator_serializes_concurrent_publishers(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    events = tuple(
        _event(
            profile_id="profile-concurrent",
            operation_id=f"operation-{index}",
        )
        for index in range(2)
    )

    def publish(event: NotificationEvent) -> PublishStatus:
        return _publisher(database, channel).publish(event).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = tuple(executor.map(publish, events))

    assert statuses == (PublishStatus.PERSISTED, PublishStatus.PERSISTED)
    with PostgresUnitOfWork(database) as uow:
        history = uow.notifications.list_profile_history(
            profile_id="profile-concurrent"
        )
        assert [item.event.profile_sequence for item in history] == [1, 2]


def test_concurrent_logical_duplicates_keep_one_event_and_one_delivery(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    events = tuple(
        _event(
            event_id=uuid4(),
            operation_id="operation-concurrent-duplicate",
            owner_epoch=epoch,
        )
        for epoch in (1, 1)
    )

    def publish(event: NotificationEvent) -> PublishStatus:
        return _publisher(database, channel).publish(event).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = tuple(executor.map(publish, events))

    assert sorted(status.value for status in statuses) == sorted(
        (PublishStatus.PERSISTED.value, PublishStatus.DUPLICATE.value)
    )
    with PostgresUnitOfWork(database) as uow:
        history = uow.notifications.list_profile_history(profile_id="profile-1")
        assert len(history) == 1
        assert len(uow.notifications.list_deliveries(history[0].event.id)) == 1


def test_two_dispatchers_claim_one_delivery_and_provider_acceptance_is_intermediate(
    database: LazyEngine,
) -> None:
    publisher_channel = _Channel()
    result = _publisher(database, publisher_channel).publish(_event())
    assert result.status is PublishStatus.PERSISTED
    first_channel = _Channel()
    second_channel = _Channel()

    def dispatch(worker_id: str, channel: _Channel):
        return NotificationDispatcher(
            lambda: PostgresUnitOfWork(database),
            channel_catalog=NotificationChannelCatalog((channel,)),
            worker_id=worker_id,
            clock=lambda: NOW,
            telemetry=object(),
        ).dispatch_once()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = tuple(
            executor.map(
                lambda args: dispatch(*args),
                (("worker-a", first_channel), ("worker-b", second_channel)),
            )
        )

    assert sum(report.claimed for report in reports) == 1
    assert first_channel.calls + second_channel.calls == 1
    with PostgresUnitOfWork(database) as uow:
        delivery = uow.notifications.list_deliveries(result.event_id)[0]
        assert delivery.state is DeliveryState.AWAITING_AGENT_ACK
        assert len(uow.notifications.list_attempts(delivery.id)) == 1


def test_expired_pending_delivery_fails_without_provider_call(
    database: LazyEngine,
) -> None:
    publisher_channel = _Channel()
    result = _publisher(database, publisher_channel).publish(
        _event(operation_id="operation-expired")
    )
    assert result.status is PublishStatus.PERSISTED

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_id = :event_id"
            ),
            {"deadline_at": NOW - timedelta(seconds=1), "event_id": result.event_id},
        )

    channel = _Channel()
    report = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-expired",
        clock=lambda: NOW,
        telemetry=object(),
    ).dispatch_once()

    assert report.claimed == 0
    assert channel.calls == 0
    with PostgresUnitOfWork(database) as uow:
        delivery = uow.notifications.list_deliveries(result.event_id)[0]
        assert delivery.state is DeliveryState.FAILED
        attempts = uow.notifications.list_attempts(delivery.id)
        assert len(attempts) == 1
        assert attempts[0].result_class is DeliveryResultClass.PERMANENT_FAILURE
        assert attempts[0].safe_error_code == "delivery_deadline_expired"


def test_expired_pending_delivery_does_not_starve_due_delivery(
    database: LazyEngine,
) -> None:
    expired_result = _publisher(database, _Channel()).publish(
        _event(operation_id="operation-expired-first")
    )
    active_result = _publisher(database, _Channel()).publish(
        _event(operation_id="operation-active-second")
    )
    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_id = :event_id"
            ),
            {"deadline_at": NOW - timedelta(seconds=1), "event_id": expired_result.event_id},
        )

    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW,
            worker_id="worker-no-starvation",
            batch_size=1,
            lease_seconds=30,
        )
        assert len(claimed) == 1
        assert claimed[0].delivery.event_id == active_result.event_id
        uow.commit()


def test_claim_bounds_lease_and_timeout_by_remaining_deadline(
    database: LazyEngine,
) -> None:
    result = _publisher(database, _Channel()).publish(
        _event(operation_id="operation-short-deadline")
    )
    assert result.status is PublishStatus.PERSISTED

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_id = :event_id"
            ),
            {"deadline_at": NOW + timedelta(seconds=5), "event_id": result.event_id},
        )

    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW,
            worker_id="worker-short-deadline",
            batch_size=1,
            lease_seconds=30,
        )
        assert len(claimed) == 1
        assert claimed[0].delivery.lease_until == NOW + timedelta(seconds=5)
        assert claimed[0].prepared.timeout_seconds == pytest.approx(5.0)
        token = claimed[0].lease_token
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        assert uow.notifications.apply_update(
            delivery_id=claimed[0].delivery.id,
            lease_token=token,
            delivery_update=DeliveryUpdate(
                state=DeliveryState.FAILED,
                result=DeliveryResult.permanent_failure("test_cleanup"),
                next_attempt_at=NOW + timedelta(seconds=5),
                completed_at=NOW + timedelta(seconds=5),
            ),
        )
        uow.commit()


def test_expired_lease_recovers_and_stale_token_cannot_update(
    database: LazyEngine,
) -> None:
    channel = _Channel(result=DeliveryResult.unavailable())
    result = _publisher(database, channel).publish(_event(operation_id="operation-recovery"))
    assert result.status is PublishStatus.PERSISTED
    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW,
            worker_id="worker-crashed",
            batch_size=1,
            lease_seconds=30,
        )
        assert len(claimed) == 1
        token = claimed[0].lease_token
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.notifications.recover_expired(
                now=NOW + timedelta(seconds=31),
                batch_size=1,
                worker_id="worker-recovery",
                retry_policy=RetryPolicy(max_attempts=3),
            )
            == 1
        )
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        delivery = uow.notifications.list_deliveries(result.event_id)[0]
        assert delivery.state is DeliveryState.RETRY_WAIT
        stale = uow.notifications.apply_update(
            delivery_id=delivery.id,
            lease_token=token,
            delivery_update=DeliveryUpdate(
                state=DeliveryState.FAILED,
                result=DeliveryResult.permanent_failure("stale_worker"),
                next_attempt_at=NOW + timedelta(seconds=31),
                completed_at=NOW + timedelta(seconds=31),
            ),
        )
        assert not stale


def test_overlapping_attempt_keeps_stable_idempotency_and_fences_old_worker(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    result = _publisher(database, channel).publish(
        _event(operation_id="operation-overlap")
    )
    assert result.status is PublishStatus.PERSISTED
    with PostgresUnitOfWork(database) as uow:
        first = uow.notifications.claim_due(
            now=NOW,
            worker_id="worker-a",
            batch_size=1,
            lease_seconds=30,
        )[0]
        first_key = first.prepared.idempotency_key
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.notifications.recover_expired(
                now=NOW + timedelta(seconds=31),
                batch_size=1,
                worker_id="worker-recovery",
                retry_policy=RetryPolicy(max_attempts=3),
            )
            == 1
        )
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        second = uow.notifications.claim_due(
            now=NOW + timedelta(seconds=40),
            worker_id="worker-b",
            batch_size=1,
            lease_seconds=30,
        )[0]
        assert second.prepared.idempotency_key == first_key
        channel.send(first.prepared)
        stale = uow.notifications.apply_update(
            delivery_id=first.delivery.id,
            lease_token=first.lease_token,
            delivery_update=DeliveryUpdate(
                state=DeliveryState.PROVIDER_ACCEPTED,
                result=DeliveryResult.provider_accepted(provider_message_id="late-a"),
                next_attempt_at=NOW + timedelta(seconds=40),
                completed_at=NOW + timedelta(seconds=40),
            ),
        )
        assert not stale
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        delivery = uow.notifications.list_deliveries(result.event_id)[0]
        attempts = uow.notifications.list_attempts(delivery.id)
        assert delivery.state is DeliveryState.IN_FLIGHT
        assert delivery.lease_owner == "worker-b"
        assert len(attempts) == 2
        assert attempts[0].lease_token == first.lease_token
        assert attempts[1].lease_token == second.lease_token


def test_dispatcher_crash_after_channel_result_is_recoverable(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    result = _publisher(database, channel).publish(
        _event(operation_id="operation-crash-after-send")
    )
    assert result.status is PublishStatus.PERSISTED
    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW,
            worker_id="worker-crashed",
            batch_size=1,
            lease_seconds=30,
        )[0]
        first_key = claimed.prepared.idempotency_key
        uow.commit()

    channel.send(claimed.prepared)

    with PostgresUnitOfWork(database) as uow:
        assert (
            uow.notifications.recover_expired(
                now=NOW + timedelta(seconds=31),
                batch_size=1,
                worker_id="worker-recovery",
                retry_policy=RetryPolicy(max_attempts=3),
            )
            == 1
        )
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        retried = uow.notifications.claim_due(
            now=NOW + timedelta(seconds=40),
            worker_id="worker-retry",
            batch_size=1,
            lease_seconds=30,
        )[0]
        assert retried.prepared.idempotency_key == first_key
        attempts = uow.notifications.list_attempts(retried.delivery.id)
        assert len(attempts) == 2
        assert attempts[0].result_class is DeliveryResultClass.TRANSIENT_FAILURE


def test_publisher_rollback_does_not_consume_profile_sequence(database: LazyEngine) -> None:
    channel = _Channel()
    publisher = _publisher(database, channel)
    event = _event(operation_id="operation-rollback")
    descriptor, payload, _ = publisher.registry.validate(event)
    decision = publisher._policy_resolver.resolve(event)
    plans = publisher._build_plans(event, descriptor, decision)
    with PostgresUnitOfWork(database) as uow:
        uow.notifications.publish(
            event,
            payload_document=payload,
            decision=decision,
            deliveries=plans,
        )
        uow.rollback()

    result = publisher.publish(event)
    assert result.status is PublishStatus.PERSISTED
    assert result.profile_sequence == 1
