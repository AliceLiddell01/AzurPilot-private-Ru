"""Application use cases для publish и bounded handover notification."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from re import fullmatch
from typing import Any
from uuid import UUID, uuid4

from module.application.errors import (
    NotificationValidationError,
    StorageError,
    StorageInvariantViolationError,
    StorageUnavailableError,
)
from module.application.notifications.channels import NotificationChannelCatalog
from module.application.notifications.encoding import notification_delivery_idempotency_key
from module.application.notifications.models import (
    ChannelCapabilities,
    HandoverNotificationOutcome,
    HandoverNotificationResult,
    HandoverPreemptionPayload,
    HandoverPublishContext,
    NotificationDeliveryPlan,
    NotificationEvent,
    NotificationPersistenceResult,
    NotificationPolicy,
    NotificationPublishContext,
    PolicyState,
    PublishResult,
    PublishStatus,
    RenderedSnapshot,
    ensure_aware_utc,
)
from module.application.notifications.policy import (
    NotificationPolicyResolver,
    default_notification_policy,
)
from module.application.notifications.ports import NotificationUnitOfWork
from module.application.notifications.registry import (
    NotificationDescriptor,
    NotificationRegistry,
    default_registry,
)
from module.application.notifications.rendering import (
    NotificationRendererCatalog,
    build_default_renderer_catalog,
)
from module.application.notifications.state import RetryPolicy
from module.application.notifications.telemetry import safe_telemetry_span


def _default_clock() -> datetime:
    return datetime.now(UTC)


class NotificationPublisher:
    """Сначала фиксирует durable intent, а внешнюю отправку оставляет dispatcher."""

    def __init__(
        self,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        registry: NotificationRegistry | None = None,
        policy: NotificationPolicy | None = None,
        policy_resolver: NotificationPolicyResolver | None = None,
        channel_catalog: NotificationChannelCatalog | None = None,
        renderer_catalog: NotificationRendererCatalog | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] = _default_clock,
        telemetry: Any | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry if registry is not None else default_registry()
        self._policy_resolver = policy_resolver or NotificationPolicyResolver(
            policy or default_notification_policy()
        )
        self._channels = channel_catalog or NotificationChannelCatalog()
        self._renderers = renderer_catalog or build_default_renderer_catalog()
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._clock = clock
        self._telemetry = telemetry if telemetry is not None else _build_telemetry()

    @property
    def registry(self) -> NotificationRegistry:
        return self._registry

    def publish(
        self,
        event: NotificationEvent,
        *,
        context: NotificationPublishContext | None = None,
    ) -> PublishResult:
        event_id = _event_id(event)
        try:
            descriptor, payload_document, payload_digest = self._registry.validate(event)
            self._validate_context(event, descriptor, context)
        except NotificationValidationError as exc:
            reason = _safe_reason(exc.reason_code)
            self._record("record_rejected", event, reason)
            return PublishResult(
                status=PublishStatus.VALIDATION_FAILED,
                event_id=event_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - contract boundary возвращает bounded failure.
            reason = "notification_contract_invalid"
            self._record("record_rejected", event, reason)
            return PublishResult(
                status=PublishStatus.VALIDATION_FAILED,
                event_id=event_id,
                reason=reason,
            )

        # Сначала читаем durable identity. Это исключает переоценку policy,
        # renderer и текущего deadline для уже committed occurrence.
        try:
            with self._uow_factory() as uow:
                existing = uow.notifications.find_existing(
                    event, payload_digest=payload_digest
                )
                if existing is not None:
                    uow.commit()
                    result = _publish_result(existing, event_id)
                    self._record("record_publish", event, result.status.value)
                    return result
        except StorageInvariantViolationError:
            raise
        except StorageError as exc:
            result = _storage_failure_result(event_id, exc)
            self._record("record_publish", event, result.status.value)
            return result

        try:
            with safe_telemetry_span(
                self._telemetry, "notification.policy.resolve"
            ):
                decision = self._policy_resolver.resolve(event)
            self._record_policy(decision)
            plans = self._build_plans(event, descriptor, decision)
        except NotificationValidationError as exc:
            reason = _safe_reason(exc.reason_code)
            self._record("record_rejected", event, reason)
            return PublishResult(
                status=PublishStatus.VALIDATION_FAILED,
                event_id=event_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - policy/render boundary возвращает bounded результат.
            reason = "notification_contract_invalid"
            self._record("record_rejected", event, reason)
            return PublishResult(
                status=PublishStatus.VALIDATION_FAILED,
                event_id=event_id,
                reason=reason,
            )
        try:
            with safe_telemetry_span(self._telemetry, "notification.publish"), self._uow_factory() as uow:
                persisted = uow.notifications.publish(
                    event,
                    payload_document=payload_document,
                    decision=decision,
                    deliveries=plans,
                )
                uow.commit()
        except StorageInvariantViolationError:
            raise
        except StorageError as exc:
            result = _storage_failure_result(event_id, exc)
            self._record("record_publish", event, result.status.value)
            return result
        self._record("record_publish", event, persisted.status.value)
        return _publish_result(persisted, event_id)

    def publish_for_handover(
        self, event: NotificationEvent, deadline: datetime
    ) -> HandoverNotificationResult:
        if not isinstance(deadline, datetime) or ensure_aware_utc(deadline) is None:
            result = PublishResult(
                PublishStatus.VALIDATION_FAILED,
                _event_id(event),
                reason="caller_deadline_not_aware",
            )
            return HandoverNotificationResult(HandoverNotificationOutcome.FAILED, result)
        if not isinstance(event, NotificationEvent) or not isinstance(
            event.data, HandoverPreemptionPayload
        ):
            result = PublishResult(
                PublishStatus.VALIDATION_FAILED,
                _event_id(event),
                reason="handover_payload_invalid",
            )
            return HandoverNotificationResult(HandoverNotificationOutcome.FAILED, result)
        event_deadline = ensure_aware_utc(event.data.deadline_at)
        caller_deadline = ensure_aware_utc(deadline)
        if event_deadline is None or caller_deadline is None or event_deadline > caller_deadline:
            result = PublishResult(
                PublishStatus.VALIDATION_FAILED,
                _event_id(event),
                reason="handover_deadline_exceeds_caller",
            )
            return HandoverNotificationResult(HandoverNotificationOutcome.FAILED, result)
        result = self.publish(
            event,
            context=HandoverPublishContext(caller_deadline=caller_deadline),
        )
        if result.status in {PublishStatus.PERSISTED, PublishStatus.DUPLICATE}:
            if result.deliveries:
                return HandoverNotificationResult(HandoverNotificationOutcome.ACCEPTED, result)
            return HandoverNotificationResult(HandoverNotificationOutcome.FAILED, result)
        if result.status is PublishStatus.UNAVAILABLE:
            return HandoverNotificationResult(HandoverNotificationOutcome.UNAVAILABLE, result)
        return HandoverNotificationResult(HandoverNotificationOutcome.FAILED, result)

    def _build_plans(
        self,
        event: NotificationEvent,
        descriptor: NotificationDescriptor,
        decision: Any,
    ) -> tuple[NotificationDeliveryPlan, ...]:
        if decision.state is PolicyState.SUPPRESSED:
            return ()
        now = _utc(self._clock())
        deadline_at = (
            event.data.deadline_at.astimezone(UTC)
            if isinstance(event.data, HandoverPreemptionPayload)
            else now + timedelta(seconds=self._retry_policy.absolute_deadline_seconds)
        )
        if deadline_at <= now:
            raise NotificationValidationError("handover_deadline_expired")
        timeout_seconds = min(
            float(self._retry_policy.absolute_deadline_seconds),
            (deadline_at - now).total_seconds(),
        )
        plans: list[NotificationDeliveryPlan] = []
        renderer = self._renderers.require(descriptor.renderer_id)
        for channel_id in decision.channel_instance_ids:
            channel = self._channels.get(channel_id)
            if channel is None:
                raise NotificationValidationError("channel_not_registered")
            capabilities = channel.capabilities
            if not isinstance(capabilities, ChannelCapabilities) or not capabilities.is_valid():
                raise NotificationValidationError("channel_capabilities_invalid")
            if not capabilities.supports_receipt_strength(
                descriptor.required_receipt_strength
            ):
                raise NotificationValidationError(
                    "channel_receipt_strength_insufficient"
                )
            missing_capabilities = set(descriptor.policy_capabilities).difference(
                capabilities.policy_capabilities
            )
            if missing_capabilities:
                raise NotificationValidationError("channel_capability_missing")
            snapshot = renderer.render(
                event,
                locale=decision.snapshot.action.locale,
                capabilities=capabilities,
                presentation_profile=decision.snapshot.action.presentation_profile,
            )
            if not isinstance(snapshot, RenderedSnapshot) or not snapshot.is_valid(
                max_title=capabilities.max_title_length,
                max_body=capabilities.max_body_length,
                max_payload_bytes=capabilities.max_payload_bytes,
            ):
                raise NotificationValidationError("rendered_snapshot_invalid")
            plans.append(
                NotificationDeliveryPlan(
                    id=uuid4(),
                    event_id=event.id,
                    event_source=event.source,
                    channel_instance_id=channel_id,
                    channel_type=channel.channel_type,
                    priority=_priority_for_event(event, descriptor, decision),
                    next_attempt_at=now,
                    deadline_at=deadline_at,
                    rendered_snapshot=snapshot,
                    idempotency_key=notification_delivery_idempotency_key(
                        source=event.source,
                        event_id=event.id,
                        channel_instance_id=channel_id,
                    ),
                    timeout_seconds=timeout_seconds,
                )
            )
        return tuple(plans)

    @staticmethod
    def _validate_context(
        event: NotificationEvent,
        descriptor: NotificationDescriptor,
        context: NotificationPublishContext | None,
    ) -> None:
        required_context = descriptor.required_context_type
        if required_context is None:
            return
        if context is None or not isinstance(context, required_context):
            raise NotificationValidationError("publish_context_required")
        if not isinstance(context.capabilities, frozenset) or any(
            not isinstance(item, str) for item in context.capabilities
        ):
            raise NotificationValidationError("publish_context_invalid")
        missing = set(descriptor.policy_capabilities).difference(context.capabilities)
        if missing:
            raise NotificationValidationError("publish_context_capability_missing")
        if not isinstance(context.caller_deadline, datetime):
            raise NotificationValidationError("caller_deadline_not_aware")
        caller_deadline = ensure_aware_utc(context.caller_deadline)
        if caller_deadline is None:
            raise NotificationValidationError("caller_deadline_not_aware")
        if isinstance(event.data, HandoverPreemptionPayload):
            event_deadline = ensure_aware_utc(event.data.deadline_at)
            if event_deadline is None or event_deadline > caller_deadline:
                raise NotificationValidationError("handover_deadline_exceeds_caller")

    def _record(self, method: str, event: object, value: str) -> None:
        telemetry_method = getattr(self._telemetry, method, None)
        if telemetry_method is None:
            return
        try:
            telemetry_method(event=event, value=value)
        except Exception:  # noqa: BLE001 - telemetry не должна менять результат операции.
            return

    def _record_policy(self, decision: object) -> None:
        method = getattr(self._telemetry, "record_policy", None)
        if method is None:
            return
        try:
            method(decision=decision)
        except Exception:  # noqa: BLE001 - telemetry не меняет policy result.
            return


def _priority_for_event(
    event: NotificationEvent,
    descriptor: NotificationDescriptor,
    decision: Any,
) -> int:
    del event
    configured = decision.snapshot.action.priority
    return descriptor.default_priority if configured is None else configured


def _event_id(event: object) -> UUID:
    if isinstance(event, NotificationEvent) and isinstance(event.id, UUID):
        return event.id
    return uuid4()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise NotificationValidationError("clock_not_aware")
    return value.astimezone(UTC)


def _safe_reason(value: object) -> str:
    if isinstance(value, str) and fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        return value
    return "validation_failed"


def _publish_result(
    persisted: NotificationPersistenceResult, event_id: UUID
) -> PublishResult:
    return PublishResult(
        status=persisted.status,
        event_id=persisted.event.event.id if persisted.event else event_id,
        profile_sequence=(
            persisted.event.event.profile_sequence if persisted.event else None
        ),
        decision=persisted.decision,
        deliveries=persisted.deliveries,
        reason=persisted.reason,
    )


def _storage_failure_result(event_id: UUID, error: StorageError) -> PublishResult:
    if isinstance(error, StorageUnavailableError):
        return PublishResult(
            status=PublishStatus.UNAVAILABLE,
            event_id=event_id,
            reason="storage_unavailable",
        )
    reason = getattr(error, "code", "storage_error")
    return PublishResult(
        status=PublishStatus.VALIDATION_FAILED,
        event_id=event_id,
        reason=_safe_reason(reason),
    )


def _build_telemetry() -> object | None:
    try:
        from module.observability.notifications import NotificationTelemetry

        return NotificationTelemetry()
    except Exception:  # noqa: BLE001 - observability fail-open при создании use case.
        return None


__all__ = ["NotificationPublisher"]
