"""Нейтральные порты будущей доставки и persistence уведомлений."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from module.application.notifications.models import (
    Delivery,
    DeliveryIdentity,
    NotificationEvent,
    NotificationSensitivity,
    NotificationSeverity,
    SubjectRef,
    require_utc,
)
from module.application.notifications.policy import PolicyDecision

MAX_CHANNEL_TIMEOUT_SECONDS = 120.0
MAX_RENDERED_TITLE_LENGTH = 1024
MAX_RENDERED_BODY_LENGTH = 16_384
MAX_SAFE_SUMMARY_LENGTH = 512
MAX_PROVIDER_EXTERNAL_ID_LENGTH = 256
MAX_ATTRIBUTES = 32


def _require_bounded_text(
    value: str | None,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field} должен быть строкой")
    if not allow_empty and not value:
        raise ValueError(f"{field} не должен быть пустым")
    if len(value) > maximum:
        raise ValueError(f"{field} превышает допустимую длину {maximum}")


class MarkupMode(StrEnum):
    PLAIN = "plain"
    MARKDOWN = "markdown"


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    max_title_length: int
    max_body_length: int
    markup_mode: MarkupMode
    supports_idempotency_key: bool

    def __post_init__(self) -> None:
        for field, value, maximum in (
            ("max_title_length", self.max_title_length, MAX_RENDERED_TITLE_LENGTH),
            ("max_body_length", self.max_body_length, MAX_RENDERED_BODY_LENGTH),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{field} находится вне допустимого диапазона")
        if not isinstance(self.markup_mode, MarkupMode):
            raise ValueError("markup_mode содержит неизвестное значение")
        if not isinstance(self.supports_idempotency_key, bool):
            raise ValueError("supports_idempotency_key должен быть bool")


type ChannelAttributeValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ChannelAttribute:
    key: str
    value: ChannelAttributeValue

    def __post_init__(self) -> None:
        _require_bounded_text(self.key, field="attribute.key", maximum=64)
        if isinstance(self.value, str):
            _require_bounded_text(
                self.value,
                field="attribute.value",
                maximum=512,
                allow_empty=True,
            )
        elif self.value is not None and not isinstance(
            self.value,
            (int, float, bool),
        ):
            raise ValueError("attribute.value содержит неподдерживаемый тип")


@dataclass(frozen=True, slots=True)
class SafeEventProjection:
    event_id: UUID
    event_type: str
    severity: NotificationSeverity
    sensitivity: NotificationSensitivity
    occurred_at: datetime
    subject: SubjectRef | None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise ValueError("projection.event_id должен быть UUID")
        _require_bounded_text(
            self.event_type,
            field="projection.event_type",
            maximum=128,
        )
        if not isinstance(self.severity, NotificationSeverity):
            raise ValueError("projection.severity некорректна")
        if not isinstance(self.sensitivity, NotificationSensitivity):
            raise ValueError("projection.sensitivity некорректна")
        require_utc(self.occurred_at, field="projection.occurred_at")
        if self.subject is not None and not isinstance(self.subject, SubjectRef):
            raise ValueError("projection.subject должен быть SubjectRef или None")


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    delivery_id: UUID
    event: SafeEventProjection
    rendered_title: str
    rendered_body: str
    idempotency_key: str
    timeout_seconds: float
    attributes: tuple[ChannelAttribute, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, UUID):
            raise ValueError("delivery_id должен быть UUID")
        if not isinstance(self.event, SafeEventProjection):
            raise ValueError("event должен быть SafeEventProjection")
        _require_bounded_text(
            self.rendered_title,
            field="rendered_title",
            maximum=MAX_RENDERED_TITLE_LENGTH,
            allow_empty=True,
        )
        _require_bounded_text(
            self.rendered_body,
            field="rendered_body",
            maximum=MAX_RENDERED_BODY_LENGTH,
            allow_empty=True,
        )
        _require_bounded_text(
            self.idempotency_key,
            field="idempotency_key",
            maximum=256,
        )
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > MAX_CHANNEL_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds находится вне допустимого диапазона")
        if not isinstance(self.attributes, tuple):
            raise ValueError("attributes должен быть tuple")
        if len(self.attributes) > MAX_ATTRIBUTES:
            raise ValueError("attributes содержит слишком много элементов")
        if any(not isinstance(item, ChannelAttribute) for item in self.attributes):
            raise ValueError("attributes должен содержать ChannelAttribute")
        keys = [attribute.key for attribute in self.attributes]
        if len(set(keys)) != len(keys):
            raise ValueError("attributes содержит дублирующиеся ключи")


class DeliveryResultOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outcome: DeliveryResultOutcome
    provider_external_id: str | None = None
    retry_after_seconds: int | None = None
    error_code: str | None = None
    safe_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, DeliveryResultOutcome):
            raise ValueError("Неизвестный DeliveryResult outcome")
        _require_bounded_text(
            self.provider_external_id,
            field="provider_external_id",
            maximum=MAX_PROVIDER_EXTERNAL_ID_LENGTH,
        )
        _require_bounded_text(self.error_code, field="error_code", maximum=64)
        _require_bounded_text(
            self.safe_summary,
            field="safe_summary",
            maximum=MAX_SAFE_SUMMARY_LENGTH,
        )
        if self.retry_after_seconds is not None:
            if (
                not isinstance(self.retry_after_seconds, int)
                or isinstance(self.retry_after_seconds, bool)
                or self.retry_after_seconds < 0
                or self.retry_after_seconds > 24 * 60 * 60
            ):
                raise ValueError(
                    "retry_after_seconds находится вне допустимого диапазона"
                )


class NotificationChannel(Protocol):
    @property
    def channel_instance_id(self) -> str: ...

    @property
    def capabilities(self) -> ChannelCapabilities: ...

    def send(self, delivery: PreparedDelivery) -> DeliveryResult: ...


class NotificationPersistencePort(Protocol):
    """Этап 3 реализует атомарную запись event/decision/delivery."""

    def persist(
        self,
        event: NotificationEvent,
        decision: PolicyDecision,
        deliveries: tuple[Delivery, ...],
    ) -> None: ...


class NotificationPublisher(Protocol):
    """Узкая application-граница для будущего перевода producers."""

    def publish(self, event: NotificationEvent) -> PolicyDecision: ...


def ensure_unique_delivery_identities(deliveries: tuple[Delivery, ...]) -> None:
    identities: set[DeliveryIdentity] = set()
    for delivery in deliveries:
        if delivery.identity in identities:
            raise ValueError(
                "Повторяется delivery identity "
                f"({delivery.identity.event_id}, {delivery.identity.channel_instance_id})"
            )
        identities.add(delivery.identity)
