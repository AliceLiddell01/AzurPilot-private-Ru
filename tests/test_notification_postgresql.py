"""Integration- и concurrency-контракты PostgreSQL notification foundation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from module.application.errors import StorageInvariantViolationError
from module.application.notifications import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryResultClass,
    DeliveryState,
    DeliveryUpdate,
    HandoverPreemptionPayload,
    NotificationAgentAckStatus,
    NotificationChannelCatalog,
    NotificationDeliveryPlan,
    NotificationDispatcher,
    NotificationEvent,
    NotificationPolicy,
    NotificationPolicyResolver,
    NotificationPublisher,
    NotificationRendererCatalog,
    NotificationRule,
    NotificationRuleMatcher,
    NotificationSeverity,
    PolicyAction,
    PublishStatus,
    ReceiptStrength,
    RenderedSnapshot,
    RetryPolicy,
)
from module.application.notifications.agent import (
    DesktopAgentAuthenticator,
    DesktopAgentCredential,
    notification_agent_document,
    parse_agent_ack_document,
)
from module.application.notifications.encoding import (
    event_payload_digest,
    notification_delivery_idempotency_key,
)
from module.application.notifications.rendering import HandoverPreemptionRenderer
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
    capabilities = ChannelCapabilities(
        receipt_strength=ReceiptStrength.AGENT_ACK,
        policy_capabilities=frozenset({"handover_receipt"}),
    )

    def __init__(
        self,
        result: DeliveryResult | None = None,
        *,
        channel_type: str = "test",
    ) -> None:
        self.channel_type = channel_type
        self.result = result or DeliveryResult.provider_accepted(provider_message_id="p1")
        self.calls = 0

    def send(self, _prepared: object) -> DeliveryResult:
        self.calls += 1
        return self.result


class _CountingRenderer:
    renderer_id = "handover.preemption"
    renderer_version = "v1"

    def __init__(self) -> None:
        self.calls = 0
        self._renderer = HandoverPreemptionRenderer()

    def render(self, *args: object, **kwargs: object):
        self.calls += 1
        return self._renderer.render(*args, **kwargs)


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
    source: str = "runtime",
    profile_id: str = "profile-1",
    operation_id: str = "operation-1",
    owner_epoch: int = 1,
) -> NotificationEvent:
    return NotificationEvent(
        id=event_id or uuid4(),
        source=source,
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


def _policy(channel_instance_id: str = "agent") -> NotificationPolicy:
    return NotificationPolicy(
        version=1,
        rules=(
            NotificationRule(
                rule_id="handover-test",
                priority=1,
                matcher=NotificationRuleMatcher(
                    exact_type="runtime.handover.preemption_requested"
                ),
                action=PolicyAction(channel_instance_ids=(channel_instance_id,)),
            ),
        ),
        default_action=PolicyAction(suppression_reason="default_suppressed"),
    )


def _publisher(
    database: LazyEngine,
    channel: _Channel,
    *,
    policy: NotificationPolicy | None = None,
    renderer_catalog: NotificationRendererCatalog | None = None,
    clock=lambda: NOW,
) -> NotificationPublisher:
    return NotificationPublisher(
        lambda: PostgresUnitOfWork(database),
        policy=policy or _policy(),
        channel_catalog=NotificationChannelCatalog((channel,)),
        renderer_catalog=renderer_catalog,
        clock=clock,
        telemetry=object(),
    )


def _publish(publisher: NotificationPublisher, event: NotificationEvent):
    return publisher.publish_for_handover(
        event, NOW + timedelta(seconds=120)
    ).publish_result


def _agent_ack(frame, *, agent_id: str = "desktop-agent-1"):
    return parse_agent_ack_document(
        {
            key: frame.document[key]
            for key in (
                "delivery_id",
                "event_id",
                "event_source",
                "profile_id",
                "attempt_ordinal",
                "lease_token",
                "session_epoch",
                "payload_digest",
            )
        },
        agent_id=agent_id,
    )


def _agent_principal():
    credential = DesktopAgentCredential(
        agent_id="desktop-agent-1",
        profiles=frozenset({"profile-1"}),
        token="agent-token-0123456789abcdef",
    )
    principal = DesktopAgentAuthenticator(credential).authenticate(
        {"Authorization": f"Bearer {credential.token}"}
    )
    assert principal is not None
    return principal


def test_application_role_has_dml_but_not_schema_ddl(database: LazyEngine) -> None:
    probe_table = f"notification_ddl_probe_{uuid4().hex}"
    with pytest.raises(DBAPIError, match="permission denied"), database.get().begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {SCHEMA_NAME}.{probe_table} "
                "(id integer NOT NULL)"
            )
        )

    row_id = uuid4()
    event_id = uuid4()
    with database.get().begin() as connection:
        connection.execute(
            text(
                f"""
                INSERT INTO {SCHEMA_NAME}.notification_event
                    (row_id, id, source, type, schema_version, profile_id,
                     severity, occurred_at, profile_sequence, payload,
                     payload_digest, sensitivity)
                VALUES
                    (:row_id, :event_id, 'test-role', 'notification.test.requested',
                     1, 'profile-role', 'INFO', :occurred_at, 1,
                     CAST(:payload AS jsonb), :payload_digest, 'NORMAL')
                """
            ),
            {
                "row_id": row_id,
                "event_id": event_id,
                "occurred_at": NOW,
                "payload": '{"value": 1}',
                "payload_digest": "0" * 64,
            },
        )
        updated = connection.execute(
            text(
                f"""
                UPDATE {SCHEMA_NAME}.notification_event
                SET payload = CAST(:payload AS jsonb)
                WHERE row_id = :row_id
                """
            ),
            {"row_id": row_id, "payload": '{"value": 2}'},
        )
        deleted = connection.execute(
            text(
                f"DELETE FROM {SCHEMA_NAME}.notification_event WHERE row_id = :row_id"
            ),
            {"row_id": row_id},
        )

    assert updated.rowcount == 1
    assert deleted.rowcount == 1


def test_publish_duplicate_conflict_and_profile_sequence_are_durable(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    publisher = _publisher(database, channel)
    event = _event()

    first = _publish(publisher, event)
    duplicate = _publish(publisher, event)
    conflict = _publish(
        publisher,
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
        assert len(uow.notifications.list_deliveries(source="runtime", event_id=event.id)) == 1


def test_publisher_crash_after_commit_returns_durable_bundle_on_retry(
    database: LazyEngine,
) -> None:
    channel = _Channel()
    renderer = _CountingRenderer()
    event = _event(operation_id="operation-durable-retry")
    first = _publish(
        _publisher(
            database,
            channel,
            renderer_catalog=NotificationRendererCatalog((renderer,)),
        ),
        event,
    )
    assert first.status is PublishStatus.PERSISTED
    assert renderer.calls == 1
    # Потеря ответа publisher имитирует crash сразу после commit.
    with PostgresUnitOfWork(database) as uow:
        assert uow.notifications.get_event(source="runtime", event_id=event.id) is not None
        assert uow.notifications.get_decision(source="runtime", event_id=event.id) is not None

    drifted_publisher = _publisher(
        database,
        channel,
        policy=_policy("missing-channel"),
        renderer_catalog=NotificationRendererCatalog((renderer,)),
        clock=lambda: NOW + timedelta(seconds=121),
    )
    retry = _publish(drifted_publisher, event)

    assert retry.status is PublishStatus.DUPLICATE
    assert retry.event_id == first.event_id
    assert retry.profile_sequence == first.profile_sequence
    assert tuple(item.id for item in retry.deliveries) == tuple(
        item.id for item in first.deliveries
    )
    assert renderer.calls == 1
    with PostgresUnitOfWork(database) as uow:
        assert len(uow.notifications.list_profile_history(profile_id="profile-1")) == 1


def test_same_uuid_different_sources_are_distinct_occurrences(
    database: LazyEngine,
) -> None:
    event_id = uuid4()
    runtime_event = _event(event_id=event_id, operation_id="operation-runtime")
    scheduler_event = _event(
        event_id=event_id,
        source="scheduler",
        operation_id="operation-scheduler",
    )
    first = _publish(_publisher(database, _Channel()), runtime_event)
    second = _publish(_publisher(database, _Channel()), scheduler_event)

    assert first.status is PublishStatus.PERSISTED
    assert second.status is PublishStatus.PERSISTED
    assert first.event_id == second.event_id
    with PostgresUnitOfWork(database) as uow:
        assert uow.notifications.get_event(source="runtime", event_id=event_id) is not None
        assert uow.notifications.get_event(source="scheduler", event_id=event_id) is not None
        history = uow.notifications.list_profile_history(profile_id="profile-1")
        assert len(history) == 2


def test_structured_idempotency_keys_keep_delimiter_candidates_distinct(
    database: LazyEngine,
) -> None:
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    first_channel = _Channel()
    first_channel.instance_id = f"x:{second_id}:y"
    second_channel = _Channel()
    second_channel.instance_id = "y"

    first = _publish(
        _publisher(
            database,
            first_channel,
            policy=_policy(first_channel.instance_id),
        ),
        _event(
            event_id=first_id,
            source="a",
            operation_id="operation-collision-a",
        ),
    )
    second = _publish(
        _publisher(
            database,
            second_channel,
            policy=_policy(second_channel.instance_id),
        ),
        _event(
            event_id=second_id,
            source=f"a:{first_id}:x",
            operation_id="operation-collision-b",
        ),
    )

    assert first.status is PublishStatus.PERSISTED
    assert second.status is PublishStatus.PERSISTED
    with PostgresUnitOfWork(database) as uow:
        first_delivery = uow.notifications.list_deliveries(
            source="a", event_id=first_id
        )[0]
        second_delivery = uow.notifications.list_deliveries(
            source=f"a:{first_id}:x", event_id=second_id
        )[0]

    assert first_delivery.idempotency_key != second_delivery.idempotency_key
    assert first_delivery.idempotency_key.startswith("notification-delivery-v1:")


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
        return _publish(_publisher(database, channel), event).status

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
        return _publish(_publisher(database, channel), event).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = tuple(executor.map(publish, events))

    assert sorted(status.value for status in statuses) == sorted(
        (PublishStatus.PERSISTED.value, PublishStatus.DUPLICATE.value)
    )
    with PostgresUnitOfWork(database) as uow:
        history = uow.notifications.list_profile_history(profile_id="profile-1")
        assert len(history) == 1
        assert len(
            uow.notifications.list_deliveries(
                source=history[0].event.source, event_id=history[0].event.id
            )
        ) == 1


def test_two_dispatchers_claim_one_delivery_and_provider_acceptance_is_intermediate(
    database: LazyEngine,
) -> None:
    publisher_channel = _Channel()
    result = _publish(_publisher(database, publisher_channel), _event())
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
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert delivery.state is DeliveryState.AWAITING_AGENT_ACK
        assert len(uow.notifications.list_attempts(delivery.id)) == 1


def test_ack_timeout_releases_token_without_synthetic_attempt(
    database: LazyEngine,
) -> None:
    channel = _Channel(result=DeliveryResult.provider_accepted(provider_message_id="ack-1"))
    publisher = _publisher(database, channel)
    result = _publish(publisher, _event(operation_id="operation-ack-timeout"))
    assert result.status is PublishStatus.PERSISTED
    retry_policy = RetryPolicy(max_attempts=2, agent_ack_timeout_seconds=5)
    dispatcher = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-ack",
        clock=lambda: NOW,
        retry_policy=retry_policy,
        lease_seconds=30,
        telemetry=object(),
    )
    assert dispatcher.dispatch_once().updated == 1

    with PostgresUnitOfWork(database) as uow:
        waiting = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        first_token = waiting.lease_token
        assert waiting.state is DeliveryState.AWAITING_AGENT_ACK
        assert first_token is not None
        assert waiting.attempt_count == 1
        assert len(uow.notifications.list_attempts(waiting.id)) == 1

    recovery = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-ack-recovery",
        clock=lambda: NOW + timedelta(seconds=6),
        retry_policy=retry_policy,
        telemetry=object(),
    )
    assert recovery.recover_expired() == 1

    with PostgresUnitOfWork(database) as uow:
        recovered = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert recovered.state is DeliveryState.RETRY_WAIT
        assert recovered.attempt_count == 1
        assert recovered.lease_token is None
        attempts = uow.notifications.list_attempts(recovered.id)
        assert len(attempts) == 1
        assert attempts[0].result_class is DeliveryResultClass.PROVIDER_ACCEPTED

    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW + timedelta(seconds=60),
            worker_id="worker-ack-retry",
            batch_size=1,
            lease_seconds=30,
        )[0]
        assert claimed.lease_token != first_token
        assert claimed.attempt_ordinal == 2
        assert claimed.delivery.attempt_count == 2
        assert len(uow.notifications.list_attempts(claimed.delivery.id)) == 2
        uow.commit()


def test_agent_history_and_ack_are_durable_and_idempotent(
    database: LazyEngine,
) -> None:
    channel = _Channel(channel_type="desktop-agent")
    result = _publish(
        _publisher(database, channel), _event(operation_id="operation-agent-ack")
    )
    assert result.status is PublishStatus.PERSISTED
    assert NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-agent-history",
        clock=lambda: NOW,
        telemetry=object(),
    ).dispatch_once().updated == 1

    with PostgresUnitOfWork(database) as uow:
        first = uow.notifications.list_agent_deliveries(
            profile_id="profile-1", channel_instance_id="agent", limit=1
        )
        second = uow.notifications.list_agent_deliveries(
            profile_id="profile-1", channel_instance_id="agent", limit=1
        )
        assert len(first) == 1
        assert [item.delivery.id for item in first] == [item.delivery.id for item in second]
        frame = notification_agent_document(first[0])
        ack = _agent_ack(frame)
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        acknowledged = uow.notifications.acknowledge_agent_delivery(ack, now=NOW)
        assert acknowledged.status is NotificationAgentAckStatus.ACKNOWLEDGED
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        duplicate = uow.notifications.acknowledge_agent_delivery(ack, now=NOW)
        assert duplicate.status is NotificationAgentAckStatus.DUPLICATE
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert delivery.state is DeliveryState.DELIVERED
        uow.commit()
    with database.get().begin() as connection:
        receipt_count = connection.execute(
            text(
                f"SELECT count(*) FROM {SCHEMA_NAME}.notification_agent_ack "
                "WHERE delivery_id = :delivery_id"
            ),
            {"delivery_id": delivery.id},
        ).scalar_one()
    assert receipt_count == 1


def test_concurrent_exact_agent_ack_has_one_mutation_and_one_duplicate(
    database: LazyEngine,
) -> None:
    channel = _Channel(channel_type="desktop-agent")
    result = _publish(
        _publisher(database, channel), _event(operation_id="operation-agent-race")
    )
    assert result.status is PublishStatus.PERSISTED
    dispatcher = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-agent-race",
        clock=lambda: NOW,
        telemetry=object(),
    )
    assert dispatcher.dispatch_once().updated == 1
    with PostgresUnitOfWork(database) as uow:
        frame = notification_agent_document(
            uow.notifications.list_agent_deliveries(
                profile_id="profile-1", channel_instance_id="agent", limit=1
            )[0]
        )
        ack = _agent_ack(frame)
        uow.commit()

    def acknowledge() -> NotificationAgentAckStatus:
        with PostgresUnitOfWork(database) as uow:
            ack_result = uow.notifications.acknowledge_agent_delivery(ack, now=NOW)
            if ack_result.status in {
                NotificationAgentAckStatus.ACKNOWLEDGED,
                NotificationAgentAckStatus.DUPLICATE,
            }:
                uow.commit()
            else:
                uow.rollback()
            return ack_result.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = tuple(executor.map(lambda _item: acknowledge(), (1, 2)))

    assert sorted(status.value for status in statuses) == [
        NotificationAgentAckStatus.ACKNOWLEDGED.value,
        NotificationAgentAckStatus.DUPLICATE.value,
    ]
    with PostgresUnitOfWork(database) as uow:
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert delivery.state is DeliveryState.DELIVERED
        uow.commit()
    with database.get().begin() as connection:
        receipt_count = connection.execute(
            text(
                f"SELECT count(*) FROM {SCHEMA_NAME}.notification_agent_ack "
                "WHERE delivery_id = :delivery_id"
            ),
            {"delivery_id": delivery.id},
        ).scalar_one()
    assert receipt_count == 1


def test_stale_agent_ack_after_lease_recovery_cannot_complete_new_attempt(
    database: LazyEngine,
) -> None:
    channel = _Channel(channel_type="desktop-agent")
    event = _event(operation_id="operation-agent-stale")
    result = _publish(_publisher(database, channel), event)
    assert result.status is PublishStatus.PERSISTED
    retry_policy = RetryPolicy(max_attempts=3, agent_ack_timeout_seconds=5)
    first_dispatcher = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-agent-stale-first",
        clock=lambda: NOW,
        retry_policy=retry_policy,
        telemetry=object(),
    )
    assert first_dispatcher.dispatch_once().updated == 1
    with PostgresUnitOfWork(database) as uow:
        frame = notification_agent_document(
            uow.notifications.list_agent_deliveries(
                profile_id="profile-1", channel_instance_id="agent", limit=1
            )[0]
        )
        stale_ack = _agent_ack(frame)
        uow.commit()

    recovery = NotificationDispatcher(
        lambda: PostgresUnitOfWork(database),
        channel_catalog=NotificationChannelCatalog((channel,)),
        worker_id="worker-agent-stale-recovery",
        clock=lambda: NOW + timedelta(seconds=6),
        retry_policy=retry_policy,
        telemetry=object(),
    )
    assert recovery.recover_expired() == 1
    with PostgresUnitOfWork(database) as uow:
        claimed = uow.notifications.claim_due(
            now=NOW + timedelta(seconds=10),
            worker_id="worker-agent-stale-second",
            batch_size=1,
            lease_seconds=30,
        )[0]
        uow.commit()

    with PostgresUnitOfWork(database) as uow:
        rejected = uow.notifications.acknowledge_agent_delivery(stale_ack, now=NOW + timedelta(seconds=10))
        assert rejected.status is NotificationAgentAckStatus.REJECTED
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert delivery.state is DeliveryState.IN_FLIGHT
        assert delivery.lease_token == claimed.lease_token
        uow.rollback()


def test_expired_pending_delivery_fails_without_provider_call(
    database: LazyEngine,
) -> None:
    publisher_channel = _Channel()
    result = _publish(_publisher(database, publisher_channel),
        _event(operation_id="operation-expired")
    )
    assert result.status is PublishStatus.PERSISTED

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_row_id = (SELECT row_id FROM "
                f"{SCHEMA_NAME}.notification_event "
                "WHERE source = 'runtime' AND id = :event_id)"
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
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
        assert delivery.state is DeliveryState.FAILED
        attempts = uow.notifications.list_attempts(delivery.id)
        assert attempts == ()


def test_expired_pending_delivery_does_not_starve_due_delivery(
    database: LazyEngine,
) -> None:
    expired_result = _publish(_publisher(database, _Channel()),
        _event(operation_id="operation-expired-first")
    )
    active_result = _publish(_publisher(database, _Channel()),
        _event(operation_id="operation-active-second")
    )
    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_row_id = (SELECT row_id FROM "
                f"{SCHEMA_NAME}.notification_event "
                "WHERE source = 'runtime' AND id = :event_id)"
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

    with PostgresUnitOfWork(database) as uow:
        expired_delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=expired_result.event_id
        )[0]
        assert expired_delivery.state is DeliveryState.FAILED
        assert uow.notifications.list_attempts(expired_delivery.id) == ()


def test_claim_bounds_lease_and_timeout_by_remaining_deadline(
    database: LazyEngine,
) -> None:
    result = _publish(_publisher(database, _Channel()),
        _event(operation_id="operation-short-deadline")
    )
    assert result.status is PublishStatus.PERSISTED

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_delivery "
                "SET deadline_at = :deadline_at "
                "WHERE event_row_id = (SELECT row_id FROM "
                f"{SCHEMA_NAME}.notification_event "
                "WHERE source = 'runtime' AND id = :event_id)"
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
    result = _publish(
        _publisher(database, channel), _event(operation_id="operation-recovery")
    )
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
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
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
    result = _publish(_publisher(database, channel),
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
        delivery = uow.notifications.list_deliveries(
            source="runtime", event_id=result.event_id
        )[0]
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
    result = _publish(_publisher(database, channel),
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
    _, payload, _ = publisher.registry.validate(event)
    decision = NotificationPolicyResolver(_policy()).resolve(event)
    plan = NotificationDeliveryPlan(
        id=uuid4(),
        event_id=event.id,
        event_source=event.source,
        channel_instance_id="agent",
        channel_type="test",
        priority=30,
        next_attempt_at=NOW,
        deadline_at=NOW + timedelta(seconds=30),
        rendered_snapshot=RenderedSnapshot(
            locale="ru-RU",
            renderer_id="handover.preemption",
            renderer_version="v1",
            title="Тест",
            body="Тестовое уведомление",
        ),
        idempotency_key=notification_delivery_idempotency_key(
            source=event.source,
            event_id=event.id,
            channel_instance_id="agent",
        ),
        timeout_seconds=30.0,
    )
    with PostgresUnitOfWork(database) as uow:
        uow.notifications.publish(
            event,
            payload_document=payload,
            decision=decision,
            deliveries=(plan,),
        )
        uow.rollback()

    result = _publish(publisher, event)
    assert result.status is PublishStatus.PERSISTED
    assert result.profile_sequence == 1


def test_unknown_stored_schema_fails_closed(database: LazyEngine) -> None:
    event = _event(operation_id="operation-unknown-schema")
    result = _publish(_publisher(database, _Channel()), event)
    assert result.status is PublishStatus.PERSISTED

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_event "
                "SET type = 'unknown.stored.schema' "
                "WHERE source = 'runtime' AND id = :event_id"
            ),
            {"event_id": result.event_id},
        )

    with (
        PostgresUnitOfWork(database) as uow,
        pytest.raises(StorageInvariantViolationError, match="schema не зарегистрирована"),
    ):
        uow.notifications.get_event(source="runtime", event_id=result.event_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_epoch", "broken"),
        ("source_profile_id", "other-profile"),
        ("operation_id", "different-operation"),
    ),
)
def test_semantically_corrupted_stored_payload_fails_closed_with_matching_digest(
    database: LazyEngine, field: str, value: object
) -> None:
    event = _event(operation_id=f"operation-corrupt-{field}")
    publisher = _publisher(database, _Channel())
    result = _publish(publisher, event)
    assert result.status is PublishStatus.PERSISTED
    _, payload, _ = publisher.registry.validate(event)
    corrupted = dict(payload)
    corrupted[field] = value
    digest = event_payload_digest(event, corrupted)

    with database.get().begin() as connection:
        connection.execute(
            text(
                f"UPDATE {SCHEMA_NAME}.notification_event "
                "SET payload = CAST(:payload AS jsonb), payload_digest = :digest "
                "WHERE source = :source AND id = :event_id"
            ),
            {
                "payload": json.dumps(corrupted),
                "digest": digest,
                "source": event.source,
                "event_id": event.id,
            },
        )

    with (
        PostgresUnitOfWork(database) as uow,
        pytest.raises(StorageInvariantViolationError, match="descriptor validation"),
    ):
        uow.notifications.get_event(source=event.source, event_id=event.id)
