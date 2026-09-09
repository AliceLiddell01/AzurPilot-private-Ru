"""Delivery state machine, retry budget и lease recovery semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from module.application.errors import StorageInvariantViolationError
from module.application.notifications.models import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryResultClass,
    DeliveryState,
    DeliveryUpdate,
    ReceiptStrength,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: int = 2
    max_delay_seconds: int = 120
    jitter_ratio: float = 0.1
    absolute_deadline_seconds: int = 300

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("Retry max_attempts вне bounded диапазона.")
        if not 1 <= self.base_delay_seconds <= self.max_delay_seconds <= 3600:
            raise ValueError("Retry delay вне bounded диапазона.")
        if not 0 <= self.jitter_ratio <= 0.5:
            raise ValueError("Retry jitter вне bounded диапазона.")
        if not 1 <= self.absolute_deadline_seconds <= 86400:
            raise ValueError("Retry absolute deadline вне bounded диапазона.")

    def delay_seconds(self, attempt_ordinal: int, *, stable_key: str = "") -> float:
        exponent = max(0, attempt_ordinal - 1)
        delay = min(self.max_delay_seconds, self.base_delay_seconds * (2**exponent))
        digest = sha256(stable_key.encode("utf-8")).digest()[0] / 255 if stable_key else 0.5
        jitter = 1 + ((digest * 2) - 1) * self.jitter_ratio
        return max(1.0, min(float(self.max_delay_seconds), delay * jitter))


def _bounded_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("State machine требует timezone-aware datetime.")
    return value.astimezone(UTC)


def transition_for_result(
    result: DeliveryResult,
    *,
    capabilities: ChannelCapabilities,
    attempt_count: int,
    now: datetime,
    deadline_at: datetime | None,
    retry_policy: RetryPolicy,
    idempotency_key: str,
) -> DeliveryUpdate:
    """Получить единственный переход; provider acceptance не становится delivery."""
    now = _bounded_now(now)
    if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count < 1:
        raise StorageInvariantViolationError("Delivery attempt count должен быть положительным.")
    if not isinstance(result, DeliveryResult) or not result.is_valid():
        raise StorageInvariantViolationError("DeliveryResult не прошёл bounded validation.")
    if not isinstance(capabilities, ChannelCapabilities) or not capabilities.is_valid():
        raise StorageInvariantViolationError("Channel capabilities нарушают invariant.")
    result_class = result.result_class
    if result_class is DeliveryResultClass.DELIVERED:
        if capabilities.receipt_strength is not ReceiptStrength.AGENT_ACK:
            raise StorageInvariantViolationError(
                "DELIVERED требует проверенный Agent ACK capability."
            )
        return DeliveryUpdate(DeliveryState.DELIVERED, result, now, completed_at=now)
    if result_class is DeliveryResultClass.PROVIDER_ACCEPTED:
        if capabilities.receipt_strength is ReceiptStrength.NONE:
            raise StorageInvariantViolationError(
                "PROVIDER_ACCEPTED требует provider receipt capability."
            )
        if capabilities.receipt_strength is ReceiptStrength.AGENT_ACK:
            ack_deadline = now + timedelta(seconds=30)
            if deadline_at is not None:
                ack_deadline = min(ack_deadline, _bounded_now(deadline_at))
            return DeliveryUpdate(
                DeliveryState.AWAITING_AGENT_ACK,
                result,
                now,
                lease_until=ack_deadline,
                completed_at=now,
            )
        return DeliveryUpdate(
            DeliveryState.PROVIDER_ACCEPTED, result, now, completed_at=now
        )
    if result_class is DeliveryResultClass.SUPPRESSED:
        return DeliveryUpdate(DeliveryState.SUPPRESSED, result, now, completed_at=now)
    if result_class is DeliveryResultClass.PERMANENT_FAILURE:
        return DeliveryUpdate(DeliveryState.FAILED, result, now, completed_at=now)
    if result_class not in {
        DeliveryResultClass.TRANSIENT_FAILURE,
        DeliveryResultClass.UNAVAILABLE,
    }:
        raise StorageInvariantViolationError("Неизвестный DeliveryResult class.")
    if attempt_count >= retry_policy.max_attempts or (
        deadline_at is not None and now >= _bounded_now(deadline_at)
    ):
        return DeliveryUpdate(DeliveryState.FAILED, result, now, completed_at=now)
    retry_after = result.retry_after_seconds
    delay = (
        max(1, min(retry_policy.max_delay_seconds, retry_after))
        if retry_after is not None
        else retry_policy.delay_seconds(attempt_count, stable_key=idempotency_key)
    )
    next_attempt = now + timedelta(seconds=delay)
    if deadline_at is not None:
        deadline = _bounded_now(deadline_at)
        if next_attempt >= deadline:
            return DeliveryUpdate(
                DeliveryState.FAILED, result, deadline, completed_at=now
            )
        next_attempt = min(next_attempt, deadline)
    return DeliveryUpdate(
        DeliveryState.RETRY_WAIT, result, next_attempt, completed_at=now
    )


def expired_lease_update(
    *,
    state: DeliveryState,
    now: datetime,
    attempt_count: int,
    deadline_at: datetime | None,
    retry_policy: RetryPolicy,
    idempotency_key: str,
) -> DeliveryUpdate:
    """Сделать lease recovery обычным bounded retry, не сохраняя stale token."""
    if state not in {DeliveryState.IN_FLIGHT, DeliveryState.AWAITING_AGENT_ACK}:
        raise StorageInvariantViolationError("Lease recovery применён к неактивной delivery.")
    result = DeliveryResult.transient_failure(
        "agent_ack_timeout" if state is DeliveryState.AWAITING_AGENT_ACK else "lease_expired"
    )
    return transition_for_result(
        result,
        capabilities=ChannelCapabilities(receipt_strength=ReceiptStrength.PROVIDER_ACCEPTANCE),
        attempt_count=max(1, attempt_count),
        now=now,
        deadline_at=deadline_at,
        retry_policy=retry_policy,
        idempotency_key=idempotency_key,
    )


__all__ = ["RetryPolicy", "expired_lease_update", "transition_for_result"]
