"""Durable PostgreSQL dispatcher с lease fencing и at-least-once attempts."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from module.application.errors import StorageInvariantViolationError
from module.application.notifications.channels import NotificationChannelCatalog
from module.application.notifications.models import (
    ChannelCapabilities,
    ClaimedDelivery,
    DeliveryResult,
    DispatchReport,
)
from module.application.notifications.ports import NotificationUnitOfWork
from module.application.notifications.state import RetryPolicy, transition_for_result
from module.application.notifications.telemetry import safe_telemetry_span


def _default_clock() -> datetime:
    return datetime.now(UTC)


class NotificationDispatcher:
    """Claim/commit и provider send разделены, поэтому row lock не держится в сети."""

    def __init__(
        self,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        channel_catalog: NotificationChannelCatalog | None = None,
        retry_policy: RetryPolicy | None = None,
        worker_id: str | None = None,
        batch_size: int = 32,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] = _default_clock,
        telemetry: object | None = None,
    ) -> None:
        if not 1 <= batch_size <= 500:
            raise ValueError("Dispatcher batch size вне bounded диапазона.")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("Dispatcher lease вне bounded диапазона.")
        self._uow_factory = uow_factory
        self._channels = channel_catalog or NotificationChannelCatalog()
        self._retry_policy = retry_policy or RetryPolicy()
        self._worker_id = worker_id or f"dispatcher-{os.getpid()}-{uuid4().hex}"
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._telemetry = telemetry if telemetry is not None else _build_telemetry()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def dispatch_once(self) -> DispatchReport:
        now = _utc(self._clock())
        with (
            safe_telemetry_span(self._telemetry, "notification.dispatch.claim"),
            self._uow_factory() as uow,
        ):
            claimed = uow.notifications.claim_due(
                now=now,
                worker_id=self._worker_id,
                batch_size=self._batch_size,
                lease_seconds=self._lease_seconds,
            )
            uow.commit()
        updated = 0
        stale = 0
        failed = 0
        for item in claimed:
            channel = self._channels.get(item.delivery.channel_instance_id)
            started = time.perf_counter()
            item_failed = False
            capabilities = _fallback_capabilities()
            result = DeliveryResult.unavailable("channel_unavailable")
            with safe_telemetry_span(
                self._telemetry,
                "notification.channel.send",
                attributes={"channel_type": item.delivery.channel_type},
            ):
                if channel is None:
                    result = DeliveryResult.unavailable("channel_unregistered")
                else:
                    try:
                        capabilities = channel.capabilities
                        if not isinstance(capabilities, ChannelCapabilities) or not capabilities.is_valid():
                            raise ValueError("Некорректные channel capabilities.")
                    except Exception:  # noqa: BLE001 - ошибка contract переводится в safe retry.
                        capabilities = _fallback_capabilities()
                        result = DeliveryResult.unavailable("channel_result_invalid")
                        item_failed = True
                    else:
                        try:
                            result = channel.send(item.prepared)
                            if not isinstance(result, DeliveryResult) or not result.is_valid():
                                result = DeliveryResult.unavailable("channel_result_invalid")
                                item_failed = True
                        except Exception:  # noqa: BLE001 - ошибка provider переводится в safe unavailable.
                            result = DeliveryResult.unavailable("channel_send_failed")
                            item_failed = True
            elapsed = time.perf_counter() - started
            try:
                transition = transition_for_result(
                    result,
                    capabilities=capabilities,
                    attempt_count=item.attempt_ordinal,
                    now=_utc(self._clock()),
                    deadline_at=item.delivery.deadline_at,
                    retry_policy=self._retry_policy,
                    idempotency_key=item.delivery.idempotency_key,
                )
            except Exception:  # noqa: BLE001 - некорректный channel result остаётся fail-closed.
                item_failed = True
                capabilities = _fallback_capabilities()
                result = DeliveryResult.unavailable("channel_result_invalid")
                try:
                    transition = transition_for_result(
                        result,
                        capabilities=capabilities,
                        attempt_count=item.attempt_ordinal,
                        now=_utc(self._clock()),
                        deadline_at=item.delivery.deadline_at,
                        retry_policy=self._retry_policy,
                        idempotency_key=item.delivery.idempotency_key,
                    )
                except Exception:  # noqa: BLE001 - lease остаётся для bounded recovery.
                    self._record_attempt(item, result)
                    self._record_latency(item, result, elapsed)
                    failed += 1
                    continue
            try:
                with self._uow_factory() as uow:
                    if uow.notifications.apply_update(
                        delivery_id=item.delivery.id,
                        lease_token=item.lease_token,
                        delivery_update=transition,
                    ):
                        uow.commit()
                        updated += 1
                    else:
                        uow.rollback()
                        stale += 1
            except Exception:  # noqa: BLE001 - ошибка storage не останавливает batch.
                item_failed = True
            self._record_attempt(item, result)
            self._record_latency(item, result, elapsed)
            failed += int(item_failed)
        return DispatchReport(
            claimed=len(claimed),
            processed=len(claimed),
            updated=updated,
            stale_updates=stale,
            failed=failed,
        )

    def recover_expired(self) -> int:
        with self._uow_factory() as uow:
            recovered = uow.notifications.recover_expired(
                now=_utc(self._clock()),
                batch_size=self._batch_size,
                worker_id=self._worker_id,
                retry_policy=self._retry_policy,
            )
            uow.commit()
        return recovered

    def _record_attempt(self, claimed: ClaimedDelivery, result: DeliveryResult) -> None:
        method = getattr(self._telemetry, "record_attempt", None)
        if method is None:
            return
        try:
            method(claimed=claimed, result=result)
        except Exception:  # noqa: BLE001 - telemetry не меняет durable state.
            return

    def _record_latency(
        self, claimed: ClaimedDelivery, result: DeliveryResult, seconds: float
    ) -> None:
        method = getattr(self._telemetry, "record_latency", None)
        if method is None:
            return
        try:
            method(
                channel_type=claimed.delivery.channel_type,
                result_class=result.result_class.value,
                seconds=seconds,
            )
        except Exception:  # noqa: BLE001 - telemetry не меняет durable state.
            return


def _fallback_capabilities() -> ChannelCapabilities:
    return ChannelCapabilities()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageInvariantViolationError("Dispatcher clock должен быть timezone-aware.")
    return value.astimezone(UTC)


def _build_telemetry() -> object | None:
    try:
        from module.observability.notifications import NotificationTelemetry

        return NotificationTelemetry()
    except Exception:  # noqa: BLE001 - observability fail-open при создании dispatcher.
        return None


__all__ = ["NotificationDispatcher"]
