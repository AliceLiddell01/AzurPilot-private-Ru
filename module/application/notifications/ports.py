"""Application ports для durable notification persistence и dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from module.application.notifications.models import (
    ClaimedDelivery,
    DeliveryUpdate,
    NotificationAgentAck,
    NotificationAgentAckResult,
    NotificationAgentDelivery,
    NotificationDeliveryPlan,
    NotificationEvent,
    NotificationPersistenceResult,
    NotificationStoredAttempt,
    NotificationStoredDelivery,
    NotificationStoredEvent,
    PolicyDecision,
)
from module.application.notifications.state import RetryPolicy
from module.application.storage_ports import StorageUnitOfWork


class NotificationRepository(Protocol):
    def find_existing(
        self, event: NotificationEvent, *, payload_digest: str
    ) -> NotificationPersistenceResult | None: ...

    def publish(
        self,
        event: NotificationEvent,
        *,
        payload_document: Mapping[str, object],
        decision: PolicyDecision,
        deliveries: tuple[NotificationDeliveryPlan, ...],
    ) -> NotificationPersistenceResult: ...

    def claim_due(
        self,
        *,
        now: datetime,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
    ) -> tuple[ClaimedDelivery, ...]: ...

    def apply_update(
        self,
        *,
        delivery_id: UUID,
        lease_token: UUID,
        delivery_update: DeliveryUpdate,
    ) -> bool: ...

    def recover_expired(
        self,
        *,
        now: datetime,
        batch_size: int,
        worker_id: str,
        retry_policy: RetryPolicy,
    ) -> int: ...

    def get_event(
        self, *, source: str, event_id: UUID
    ) -> NotificationStoredEvent | None: ...

    def get_decision(
        self, *, source: str, event_id: UUID
    ) -> PolicyDecision | None: ...

    def list_deliveries(
        self, *, source: str, event_id: UUID
    ) -> tuple[NotificationStoredDelivery, ...]: ...

    def list_attempts(self, delivery_id: UUID) -> tuple[NotificationStoredAttempt, ...]: ...

    def list_profile_history(
        self, *, profile_id: str, after_sequence: int = 0, limit: int = 100
    ) -> tuple[NotificationStoredEvent, ...]: ...

    def list_agent_deliveries(
        self,
        *,
        profile_id: str,
        channel_instance_id: str,
        after_sequence: int = 0,
        after_event_id: UUID | None = None,
        limit: int = 32,
    ) -> tuple[NotificationAgentDelivery, ...]:
        """Вернуть строки строго по возрастанию `(profile_sequence, event_id)`."""
        ...

    def acknowledge_agent_delivery(
        self,
        ack: NotificationAgentAck,
        *,
        now: datetime,
    ) -> NotificationAgentAckResult: ...


class NotificationUnitOfWork(StorageUnitOfWork, Protocol):
    notifications: NotificationRepository


__all__ = ["NotificationRepository", "NotificationUnitOfWork"]
