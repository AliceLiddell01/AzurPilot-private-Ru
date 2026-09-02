"""Реестр семантических схем событий платформы уведомлений."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from module.application.notifications.models import (
    CampaignConfigurationFailurePayload,
    CampaignStopConditionPayload,
    CommissionRewardPayload,
    NotificationEvent,
    NotificationPayload,
    NotificationSensitivity,
    NotificationSeverity,
    NotificationTestRequestedPayload,
    OpsiActionPointChangedPayload,
    OpsiActionPointLowPayload,
    OpsiFleetAutoChangePayload,
    OpsiResourcesInsufficientPayload,
    OpsiSchedulerCoinTaskExecutedPayload,
    OpsiSchedulerConfigurationInvalidPayload,
    OpsiShipExpCheckPayload,
    OpsiShipExpTargetReachedPayload,
    RuntimeEventPayload,
    SubjectRef,
    TaskEventPayload,
    TaskFailureLimitPayload,
)


class NotificationValidationError(ValueError):
    """Событие не соответствует зарегистрированному семантическому контракту."""


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    event_type: str
    schema_version: int
    payload_type: type[NotificationPayload]
    default_severity: NotificationSeverity
    allowed_severity_overrides: frozenset[NotificationSeverity] = frozenset()

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type descriptor не задан")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version descriptor должен быть положительным")
        if not isinstance(self.payload_type, type) or self.payload_type is dict:
            raise ValueError("payload_type descriptor должен быть конкретным типом")
        if not isinstance(self.default_severity, NotificationSeverity):
            raise ValueError("default_severity descriptor некорректен")
        if any(
            not isinstance(value, NotificationSeverity)
            for value in self.allowed_severity_overrides
        ):
            raise ValueError("allowed_severity_overrides содержит неизвестную severity")
        if self.default_severity in self.allowed_severity_overrides:
            raise ValueError("default severity не должна дублироваться в overrides")


class EventRegistry:
    """Реестр, полностью собранный до запуска runtime."""

    def __init__(self, descriptors: Iterable[EventDescriptor]):
        descriptor_map: dict[tuple[str, int], EventDescriptor] = {}
        for descriptor in descriptors:
            if not isinstance(descriptor, EventDescriptor):
                raise ValueError("Registry принимает только EventDescriptor")
            key = (descriptor.event_type, descriptor.schema_version)
            if key in descriptor_map:
                raise ValueError(
                    "Дублируется descriptor события "
                    f"{descriptor.event_type} v{descriptor.schema_version}"
                )
            descriptor_map[key] = descriptor
        self._descriptors = descriptor_map

    @property
    def event_types(self) -> tuple[str, ...]:
        return tuple(
            sorted({event_type for event_type, _version in self._descriptors})
        )

    def descriptor(self, event_type: str, schema_version: int) -> EventDescriptor:
        try:
            return self._descriptors[(event_type, schema_version)]
        except KeyError as exc:
            versions = sorted(
                version
                for registered_type, version in self._descriptors
                if registered_type == event_type
            )
            if versions:
                raise NotificationValidationError(
                    f"Неизвестная версия события {event_type}: {schema_version}"
                ) from exc
            raise NotificationValidationError(
                f"Незарегистрированный тип события: {event_type}"
            ) from exc

    def validate(self, event: NotificationEvent) -> EventDescriptor:
        if not isinstance(event, NotificationEvent):
            raise NotificationValidationError("Ожидается NotificationEvent")
        descriptor = self.descriptor(event.type, event.schema_version)
        if type(event.data) is not descriptor.payload_type:
            raise NotificationValidationError(
                f"Payload {event.type} v{event.schema_version} должен иметь тип "
                f"{descriptor.payload_type.__name__}"
            )
        if (
            event.severity is not descriptor.default_severity
            and event.severity not in descriptor.allowed_severity_overrides
        ):
            raise NotificationValidationError(
                f"Severity {event.severity.name} запрещён для {event.type}"
            )
        return descriptor

    def build_event(
        self,
        *,
        id: UUID,
        event_type: str,
        schema_version: int,
        source: str,
        profile_id: str,
        occurred_at: datetime,
        data: NotificationPayload,
        runtime_instance_id: str | None = None,
        subject: SubjectRef | None = None,
        severity: NotificationSeverity | None = None,
        dedup_key: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        sensitivity: NotificationSensitivity = NotificationSensitivity.NORMAL,
    ) -> NotificationEvent:
        descriptor = self.descriptor(event_type, schema_version)
        resolved_severity = severity or descriptor.default_severity
        event = NotificationEvent(
            id=id,
            type=event_type,
            schema_version=schema_version,
            source=source,
            profile_id=profile_id,
            runtime_instance_id=runtime_instance_id,
            subject=subject,
            severity=resolved_severity,
            occurred_at=occurred_at,
            data=data,
            dedup_key=dedup_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
            sensitivity=sensitivity,
        )
        self.validate(event)
        return event


DEFAULT_EVENT_DESCRIPTORS = (
    EventDescriptor("task.completed", 1, TaskEventPayload, NotificationSeverity.INFO),
    EventDescriptor("task.recovered", 1, TaskEventPayload, NotificationSeverity.INFO),
    EventDescriptor(
        "task.failed",
        1,
        TaskEventPayload,
        NotificationSeverity.ERROR,
        frozenset({NotificationSeverity.CRITICAL}),
    ),
    EventDescriptor(
        "task.failure_limit.reached",
        1,
        TaskFailureLimitPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.game.unavailable",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.game.stuck",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.game.error",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.game.page_unknown",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.emulator.unavailable",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "runtime.emulator.recovered",
        1,
        RuntimeEventPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "runtime.recovery.succeeded",
        1,
        RuntimeEventPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "runtime.recovery.failed",
        1,
        RuntimeEventPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "campaign.stop_condition.reached",
        1,
        CampaignStopConditionPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "campaign.auto_search.configuration_failed",
        1,
        CampaignConfigurationFailurePayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "commission.reward.received",
        1,
        CommissionRewardPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.action_point.changed",
        1,
        OpsiActionPointChangedPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.action_point.low",
        1,
        OpsiActionPointLowPayload,
        NotificationSeverity.WARNING,
    ),
    EventDescriptor(
        "opsi.resources.insufficient",
        1,
        OpsiResourcesInsufficientPayload,
        NotificationSeverity.WARNING,
    ),
    EventDescriptor(
        "opsi.scheduler.configuration.invalid",
        1,
        OpsiSchedulerConfigurationInvalidPayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "opsi.scheduler.coin_task.executed",
        1,
        OpsiSchedulerCoinTaskExecutedPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.ship_exp.check_failed",
        1,
        OpsiShipExpCheckPayload,
        NotificationSeverity.WARNING,
    ),
    EventDescriptor(
        "opsi.ship_exp.check_completed",
        1,
        OpsiShipExpCheckPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.ship_exp.target_reached",
        1,
        OpsiShipExpTargetReachedPayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.fleet.auto_change.completed",
        1,
        OpsiFleetAutoChangePayload,
        NotificationSeverity.INFO,
    ),
    EventDescriptor(
        "opsi.fleet.auto_change.failed",
        1,
        OpsiFleetAutoChangePayload,
        NotificationSeverity.ERROR,
    ),
    EventDescriptor(
        "notification.test.requested",
        1,
        NotificationTestRequestedPayload,
        NotificationSeverity.INFO,
    ),
)

DEFAULT_EVENT_REGISTRY = EventRegistry(DEFAULT_EVENT_DESCRIPTORS)
