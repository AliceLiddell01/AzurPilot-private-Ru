"""Application boundary для authenticated Desktop Agent notification channel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from module.application.errors import (
    NotificationValidationError,
    StorageError,
    StorageUnavailableError,
)
from module.application.notifications.channels import (
    DESKTOP_AGENT_CHANNEL_TYPE,
    NotificationChannelCatalog,
)
from module.application.notifications.dispatcher import NotificationDispatcher
from module.application.notifications.models import (
    ChannelCapabilities,
    DeliveryResult,
    DeliveryState,
    HandoverNotificationOutcome,
    HandoverNotificationResult,
    HandoverPreemptionPayload,
    NotificationAgentAck,
    NotificationAgentAckResult,
    NotificationAgentAckStatus,
    NotificationAgentDelivery,
    NotificationCorrelation,
    NotificationEvent,
    NotificationSensitivity,
    NotificationSeverity,
    NotificationStoredDelivery,
    NotificationSubject,
    ReceiptStrength,
    ensure_aware_utc,
)
from module.application.notifications.ports import NotificationUnitOfWork
from module.application.notifications.registry import default_registry
from module.application.notifications.rendering import build_default_renderer_catalog
from module.application.notifications.service import NotificationPublisher
from module.application.notifications.state import RetryPolicy
from module.application.notifications.telemetry import safe_telemetry_span
from module.application.runtime_handover import NotificationOutcome
from module.application.runtime_state import RuntimeStateSnapshot

DESKTOP_AGENT_CHANNEL_INSTANCE_ID = "desktop-agent"
DESKTOP_AGENT_STREAM_PATH = "/api/notification-agent/stream"
DESKTOP_AGENT_ACK_PATH = "/api/notification-agent/ack"

MAX_AGENT_TOKEN_LENGTH = 512
MAX_AGENT_ID_LENGTH = 128
MAX_AGENT_PROFILES = 64
MAX_AGENT_PROFILE_TEXT_LENGTH = MAX_AGENT_PROFILES * (MAX_AGENT_ID_LENGTH + 1)
MAX_AGENT_CURSOR_LENGTH = 256
MAX_AGENT_BATCH_SIZE = 128
MAX_AGENT_ACK_BODY_BYTES = 16 * 1024
MAX_AGENT_SSE_FRAME_BYTES = 256 * 1024
AGENT_STREAM_MAX_SECONDS = 180.0
AGENT_POLL_SECONDS = 0.25
AGENT_STORAGE_BACKOFF_MAX_SECONDS = 8.0
RECOVERABLE_AGENT_ACK_REASONS = frozenset(
    {
        "delivery_not_found",
        "delivery_already_completed",
        "delivery_not_awaiting_ack",
        "attempt_identity_mismatch",
        "attempt_not_awaiting_ack",
        "lease_identity_mismatch",
        "ack_expired",
        "stale_delivery",
        "session_identity_mismatch",
    }
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CURSOR_PREFIX = "na1."
_CURSOR_FIELDS = frozenset({"v", "p", "s", "e"})
_ACK_FIELDS = frozenset(
    {
        "delivery_id",
        "event_id",
        "event_source",
        "profile_id",
        "attempt_ordinal",
        "lease_token",
        "session_epoch",
        "payload_digest",
    }
)
_WIRE_FIELDS = frozenset(
    {
        "event_id",
        "event_source",
        "event_type",
        "schema_version",
        "profile_id",
        "severity",
        "occurred_at",
        "profile_sequence",
        "delivery_id",
        "attempt_ordinal",
        "lease_token",
        "session_epoch",
        "payload_digest",
        "title",
        "body",
        "locale",
        "sensitivity",
    }
)


class DesktopAgentError(RuntimeError):
    """Безопасная ошибка application boundary Desktop Agent."""

    code = "desktop_agent_error"


class DesktopAgentConfigurationError(DesktopAgentError):
    code = "desktop_agent_configuration_invalid"


class DesktopAgentRequestError(DesktopAgentError, ValueError):
    code = "desktop_agent_request_invalid"


class DesktopAgentAuthorizationError(DesktopAgentError):
    code = "desktop_agent_authorization_failed"


class DesktopAgentUnavailableError(DesktopAgentError):
    code = "desktop_agent_unavailable"


class NotificationCursorError(DesktopAgentRequestError):
    code = "notification_cursor_invalid"


@dataclass(frozen=True, slots=True)
class DesktopAgentCredential:
    """Scoped Agent credential; ``repr`` намеренно не раскрывает token."""

    agent_id: str
    profiles: frozenset[str]
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.agent_id) or len(self.agent_id) > MAX_AGENT_ID_LENGTH:
            raise DesktopAgentConfigurationError("Agent id имеет неверный формат.")
        if not isinstance(self.profiles, frozenset) or not 0 < len(self.profiles) <= MAX_AGENT_PROFILES:
            raise DesktopAgentConfigurationError("Agent profile scope имеет неверный размер.")
        if any(not _SAFE_PROFILE_RE.fullmatch(profile) for profile in self.profiles):
            raise DesktopAgentConfigurationError("Agent profile scope имеет неверный формат.")
        _validate_agent_token(self.token)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str | None] | None = None
    ) -> DesktopAgentCredential:
        source = os.environ if environment is None else environment
        agent_id = source.get("AZURPILOT_NOTIFICATION_AGENT_ID")
        profile_text = source.get("AZURPILOT_NOTIFICATION_AGENT_PROFILES")
        token = source.get("AZURPILOT_NOTIFICATION_AGENT_TOKEN")
        token_file = source.get("AZURPILOT_NOTIFICATION_AGENT_TOKEN_FILE")
        if not isinstance(agent_id, str) or not isinstance(profile_text, str):
            raise DesktopAgentConfigurationError("Не задана полная Agent identity configuration.")
        if len(profile_text) > MAX_AGENT_PROFILE_TEXT_LENGTH:
            raise DesktopAgentConfigurationError("Agent profile scope превысил bounded размер.")
        if token is not None and token_file is not None:
            raise DesktopAgentConfigurationError("Заданы два источника Agent token.")
        if token is None and token_file is not None:
            token = _read_token_file(token_file)
        if not isinstance(token, str):
            raise DesktopAgentConfigurationError("Agent token не задан.")
        profiles = frozenset(
            item.strip() for item in profile_text.split(",") if item.strip()
        )
        return cls(agent_id=agent_id, profiles=profiles, token=token)


@dataclass(frozen=True, slots=True)
class DesktopAgentPrincipal:
    agent_id: str
    profiles: frozenset[str]

    def can_access(self, profile_id: str) -> bool:
        return profile_id in self.profiles


class DesktopAgentAuthenticator:
    """Проверяет ровно Bearer credential без хранения raw token в логике запроса."""

    def __init__(self, credential: DesktopAgentCredential | None) -> None:
        self._credential = credential

    @property
    def enabled(self) -> bool:
        return self._credential is not None

    def authenticate(self, headers: Mapping[str, str]) -> DesktopAgentPrincipal | None:
        credential = self._credential
        if credential is None:
            return None
        authorization_values = [
            value for key, value in headers.items() if key.casefold() == "authorization"
        ]
        if len(authorization_values) != 1:
            return None
        value = authorization_values[0]
        prefix = "Bearer "
        if not isinstance(value, str) or not value.startswith(prefix):
            return None
        presented = value[len(prefix) :]
        if not presented or not hmac.compare_digest(presented, credential.token):
            return None
        return DesktopAgentPrincipal(credential.agent_id, credential.profiles)


@dataclass(frozen=True, slots=True)
class NotificationCursor:
    profile_id: str
    profile_sequence: int
    event_id: UUID

    def __post_init__(self) -> None:
        if not _SAFE_PROFILE_RE.fullmatch(self.profile_id):
            raise NotificationCursorError("Cursor profile имеет неверный формат.")
        if not isinstance(self.profile_sequence, int) or isinstance(
            self.profile_sequence, bool
        ) or self.profile_sequence <= 0:
            raise NotificationCursorError("Cursor sequence имеет неверный формат.")
        if not isinstance(self.event_id, UUID):
            raise NotificationCursorError("Cursor event id имеет неверный формат.")

    def encode(self) -> str:
        document = {
            "v": 1,
            "p": self.profile_id,
            "s": self.profile_sequence,
            "e": str(self.event_id),
        }
        raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        cursor = _CURSOR_PREFIX + encoded
        if len(cursor) > MAX_AGENT_CURSOR_LENGTH:
            raise NotificationCursorError("Cursor превысил bounded размер.")
        return cursor

    @classmethod
    def decode(
        cls, value: str | None, *, expected_profile_id: str
    ) -> NotificationCursor | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_AGENT_CURSOR_LENGTH:
            raise NotificationCursorError("Cursor имеет неверный размер.")
        if not _SAFE_PROFILE_RE.fullmatch(expected_profile_id):
            raise NotificationCursorError("Ожидаемый cursor profile имеет неверный формат.")
        if not value.startswith(_CURSOR_PREFIX):
            raise NotificationCursorError("Cursor имеет неизвестную версию.")
        encoded = value[len(_CURSOR_PREFIX) :]
        if not encoded or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
            raise NotificationCursorError("Cursor encoding имеет неверный формат.")
        try:
            padding = "=" * (-len(encoded) % 4)
            document = json.loads(
                base64.b64decode(
                    encoded + padding, altchars=b"-_", validate=True
                ).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise NotificationCursorError("Cursor невозможно декодировать.") from None
        if not isinstance(document, Mapping) or frozenset(document) != _CURSOR_FIELDS:
            raise NotificationCursorError("Cursor имеет неизвестные поля.")
        if document.get("v") != 1 or document.get("p") != expected_profile_id:
            raise NotificationCursorError("Cursor не принадлежит запрошенному profile.")
        sequence = document.get("s")
        event_id = document.get("e")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise NotificationCursorError("Cursor sequence имеет неверный формат.")
        try:
            parsed_event_id = UUID(str(event_id))
        except (TypeError, ValueError):
            raise NotificationCursorError("Cursor event id имеет неверный формат.") from None
        return cls(expected_profile_id, sequence, parsed_event_id)


@dataclass(frozen=True, slots=True)
class DesktopAgentDeliveryFrame:
    cursor: NotificationCursor
    document: dict[str, object]


def parse_agent_ack_document(
    document: object, *, agent_id: str
) -> NotificationAgentAck:
    """Преобразовать только exact ACK contract; identity Agent берётся из auth."""

    if not isinstance(document, Mapping) or frozenset(document) != _ACK_FIELDS:
        raise DesktopAgentRequestError("ACK имеет неизвестный набор полей.")
    if not _SAFE_ID_RE.fullmatch(agent_id):
        raise DesktopAgentAuthorizationError("Authenticated Agent identity некорректна.")
    try:
        delivery_id = UUID(str(document["delivery_id"]))
        event_id = UUID(str(document["event_id"]))
        lease_token = UUID(str(document["lease_token"]))
        session_epoch = UUID(str(document["session_epoch"]))
    except (TypeError, ValueError):
        raise DesktopAgentRequestError("ACK содержит некорректный UUID.") from None
    source = document["event_source"]
    profile = document["profile_id"]
    digest = document["payload_digest"]
    ordinal = document["attempt_ordinal"]
    if not isinstance(source, str) or not _SAFE_ID_RE.fullmatch(source):
        raise DesktopAgentRequestError("ACK event source имеет неверный формат.")
    if not isinstance(profile, str) or not _SAFE_PROFILE_RE.fullmatch(profile):
        raise DesktopAgentRequestError("ACK profile имеет неверный формат.")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= 1_000_000
    ):
        raise DesktopAgentRequestError("ACK attempt ordinal имеет неверный формат.")
    if not isinstance(digest, str) or _SAFE_DIGEST_RE.fullmatch(digest) is None:
        raise DesktopAgentRequestError("ACK payload digest имеет неверный формат.")
    return NotificationAgentAck(
        delivery_id=delivery_id,
        event_id=event_id,
        event_source=source,
        profile_id=profile,
        attempt_ordinal=ordinal,
        lease_token=lease_token,
        session_epoch=session_epoch,
        payload_digest=digest,
        agent_id=agent_id,
    )


def notification_agent_document(
    item: NotificationAgentDelivery,
    *,
    session_epoch: UUID,
) -> DesktopAgentDeliveryFrame:
    """Собрать безопасную projection; typed payload намеренно не выходит в wire."""

    event = item.event.event
    delivery = item.delivery
    attempt = item.attempt
    if event.profile_sequence is None or event.payload_digest is None:
        raise DesktopAgentUnavailableError("Stored event не имеет cursor identity.")
    if delivery.lease_token is None or attempt.lease_token is None:
        raise DesktopAgentUnavailableError("Stored delivery не имеет Agent lease identity.")
    if delivery.lease_token != attempt.lease_token:
        raise DesktopAgentUnavailableError("Stored attempt относится к устаревшему lease.")
    if not isinstance(session_epoch, UUID):
        raise DesktopAgentUnavailableError("Stored Agent session identity имеет неверный формат.")
    cursor = NotificationCursor(event.profile_id, event.profile_sequence, event.id)
    title = delivery.rendered_snapshot.title
    body = delivery.rendered_snapshot.body
    if event.sensitivity is NotificationSensitivity.SENSITIVE:
        if delivery.rendered_snapshot.locale.casefold().startswith("en"):
            title = "Restricted notification"
            body = "The notification content is available only in the local interface."
        else:
            title = "Уведомление с ограниченным содержимым"
            body = "Содержимое уведомления доступно только в локальном интерфейсе."
    document = {
        "event_id": str(event.id),
        "event_source": event.source,
        "event_type": event.type,
        "schema_version": event.schema_version,
        "profile_id": event.profile_id,
        "severity": event.severity.value,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
        "profile_sequence": event.profile_sequence,
        "delivery_id": str(delivery.id),
        "attempt_ordinal": attempt.attempt_ordinal,
        "lease_token": str(delivery.lease_token),
        "session_epoch": str(session_epoch),
        "payload_digest": event.payload_digest,
        "title": title,
        "body": body,
        "locale": delivery.rendered_snapshot.locale,
        "sensitivity": event.sensitivity.value,
    }
    if frozenset(document) != _WIRE_FIELDS:
        raise DesktopAgentUnavailableError("Agent wire projection имеет неверную структуру.")
    return DesktopAgentDeliveryFrame(cursor=cursor, document=document)


def validate_agent_delivery_document(document: object) -> dict[str, object]:
    """Проверить входной SSE frame до callback и ACK."""

    if not isinstance(document, Mapping) or frozenset(document) != _WIRE_FIELDS:
        raise DesktopAgentRequestError("SSE notification frame имеет неизвестные поля.")
    uuid_fields = ("event_id", "delivery_id", "lease_token", "session_epoch")
    try:
        parsed = {name: UUID(str(document[name])) for name in uuid_fields}
    except (TypeError, ValueError):
        raise DesktopAgentRequestError("SSE frame содержит некорректный UUID.") from None
    profile = document["profile_id"]
    source = document["event_source"]
    digest = document["payload_digest"]
    if not isinstance(profile, str) or not _SAFE_PROFILE_RE.fullmatch(profile):
        raise DesktopAgentRequestError("SSE frame profile имеет неверный формат.")
    if not isinstance(source, str) or not _SAFE_ID_RE.fullmatch(source):
        raise DesktopAgentRequestError("SSE frame source имеет неверный формат.")
    ordinal = document["attempt_ordinal"]
    sequence = document["profile_sequence"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
        raise DesktopAgentRequestError("SSE frame attempt ordinal имеет неверный формат.")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise DesktopAgentRequestError("SSE frame profile sequence имеет неверный формат.")
    if not isinstance(digest, str) or _SAFE_DIGEST_RE.fullmatch(digest) is None:
        raise DesktopAgentRequestError("SSE frame payload digest имеет неверный формат.")
    for name in ("event_type", "severity", "occurred_at", "title", "body", "locale", "sensitivity"):
        if not isinstance(document[name], str) or not document[name] or len(document[name]) > 65_536:
            raise DesktopAgentRequestError("SSE frame содержит некорректное текстовое поле.")
    result = dict(document)
    result.update(parsed)
    return result


class DesktopAgentChannel:
    """Channel adapter сохраняет provider acceptance; ACK приходит отдельным endpoint."""

    instance_id = DESKTOP_AGENT_CHANNEL_INSTANCE_ID
    channel_type = DESKTOP_AGENT_CHANNEL_TYPE
    capabilities = ChannelCapabilities(
        max_payload_bytes=64 * 1024,
        max_title_length=4096,
        max_body_length=64 * 1024,
        receipt_strength=ReceiptStrength.AGENT_ACK,
        health_check=True,
        policy_capabilities=frozenset({"handover_receipt"}),
    )

    def __init__(self, *, wake: Callable[[], None] | None = None) -> None:
        self._wake = wake

    def send(self, _prepared: object) -> DeliveryResult:
        if self._wake is not None:
            try:
                self._wake()
            except Exception:  # noqa: BLE001 - пробуждение является только оптимизацией.
                pass
        return DeliveryResult.provider_accepted(provider_message_id="desktop-agent")


class DesktopAgentHistoryService:
    def __init__(
        self,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        channel_instance_id: str = DESKTOP_AGENT_CHANNEL_INSTANCE_ID,
        telemetry: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._channel_instance_id = channel_instance_id
        self._telemetry = telemetry
        self._clock = clock or (lambda: datetime.now(UTC))

    def read(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        cursor: str | None,
        limit: int,
        session_epoch: UUID,
    ) -> tuple[DesktopAgentDeliveryFrame, ...]:
        _authorize_profile(principal, profile_id)
        if not 1 <= limit <= MAX_AGENT_BATCH_SIZE:
            raise DesktopAgentRequestError("Agent batch limit вне bounded диапазона.")
        if not isinstance(session_epoch, UUID):
            raise DesktopAgentAuthorizationError("Agent session identity некорректна.")
        parsed_cursor = NotificationCursor.decode(
            cursor, expected_profile_id=profile_id
        )
        with safe_telemetry_span(
            self._telemetry,
            "notification.history.read",
            attributes={"channel_type": DESKTOP_AGENT_CHANNEL_TYPE},
        ), self._uow_factory() as uow:
            if parsed_cursor is not None and not uow.notifications.validate_agent_cursor(
                profile_id=profile_id,
                channel_instance_id=self._channel_instance_id,
                after_sequence=parsed_cursor.profile_sequence,
                after_event_id=parsed_cursor.event_id,
            ):
                raise NotificationCursorError("Cursor отсутствует в durable Agent history.")
            rows = uow.notifications.list_agent_deliveries(
                profile_id=profile_id,
                channel_instance_id=self._channel_instance_id,
                after_sequence=parsed_cursor.profile_sequence if parsed_cursor else 0,
                after_event_id=parsed_cursor.event_id if parsed_cursor else None,
                limit=limit,
            )
            uow.commit()
        return tuple(
            notification_agent_document(row, session_epoch=session_epoch)
            for row in rows
        )

    def validate_cursor(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        cursor: str | None,
    ) -> None:
        _authorize_profile(principal, profile_id)
        parsed_cursor = NotificationCursor.decode(
            cursor, expected_profile_id=profile_id
        )
        if parsed_cursor is None:
            return
        with self._uow_factory() as uow:
            valid = uow.notifications.validate_agent_cursor(
                profile_id=profile_id,
                channel_instance_id=self._channel_instance_id,
                after_sequence=parsed_cursor.profile_sequence,
                after_event_id=parsed_cursor.event_id,
            )
            uow.commit()
        if not valid:
            raise NotificationCursorError("Cursor отсутствует в durable Agent history.")

    def acknowledge(
        self, principal: DesktopAgentPrincipal, ack: NotificationAgentAck
    ) -> NotificationAgentAckResult:
        _authorize_profile(principal, ack.profile_id)
        with safe_telemetry_span(
            self._telemetry,
            "notification.agent.ack",
            attributes={"channel_type": DESKTOP_AGENT_CHANNEL_TYPE},
        ), self._uow_factory() as uow:
            result = uow.notifications.acknowledge_agent_delivery(
                ack,
                now=_utc(self._clock()),
                channel_instance_id=self._channel_instance_id,
            )
            if result.status in {
                NotificationAgentAckStatus.ACKNOWLEDGED,
                NotificationAgentAckStatus.DUPLICATE,
            }:
                uow.commit()
            else:
                uow.rollback()
        _record_agent_ack(self._telemetry, result.status)
        return result

    def delivery_state(
        self, principal: DesktopAgentPrincipal, frame: DesktopAgentDeliveryFrame
    ) -> NotificationStoredDelivery | None:
        _authorize_profile(principal, frame.document["profile_id"])
        event_id = UUID(str(frame.document["event_id"]))
        source = str(frame.document["event_source"])
        delivery_id = UUID(str(frame.document["delivery_id"]))
        with self._uow_factory() as uow:
            delivery = uow.notifications.get_agent_delivery_state(
                profile_id=str(frame.document["profile_id"]),
                channel_instance_id=self._channel_instance_id,
                delivery_id=delivery_id,
                event_id=event_id,
                event_source=source,
            )
            uow.commit()
        return delivery


class NotificationHandoverWaiter:
    """Ожидает durable Agent ACK ограниченный срок после publish transaction."""

    def __init__(
        self,
        publisher: NotificationPublisher,
        dispatcher: NotificationDispatcher,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        poll_seconds: float = AGENT_POLL_SECONDS,
    ) -> None:
        if not 0 < poll_seconds <= 5:
            raise ValueError("Agent handover poll вне bounded диапазона.")
        self._publisher = publisher
        self._dispatcher = dispatcher
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._poll_seconds = poll_seconds

    def publish_and_wait(
        self, event: NotificationEvent, *, deadline: datetime
    ) -> HandoverNotificationResult:
        caller_deadline = _utc(deadline)
        published = self._publisher.publish_for_handover(event, caller_deadline)
        if published.outcome is not HandoverNotificationOutcome.ACCEPTED:
            return published
        started = self._monotonic()
        initial_remaining = max(
            0.0, (caller_deadline - _utc(self._clock())).total_seconds()
        )
        monotonic_deadline = started + initial_remaining
        while True:
            try:
                self._dispatcher.recover_expired()
                self._dispatcher.dispatch_once()
                deliveries = self._deliveries(event)
            except StorageUnavailableError:
                return replace(
                    published, outcome=HandoverNotificationOutcome.UNAVAILABLE
                )
            except StorageError:
                return replace(published, outcome=HandoverNotificationOutcome.FAILED)
            if self._deadline_reached(caller_deadline, monotonic_deadline):
                return replace(published, outcome=HandoverNotificationOutcome.FAILED)
            if _delivery_proof(deliveries, caller_deadline):
                # Чтение DB и проверка proof отделены от caller deadline.
                # Повторная проверка перед успехом не даёт позднему ACK
                # возобновить handover.
                if self._deadline_reached(caller_deadline, monotonic_deadline):
                    return replace(published, outcome=HandoverNotificationOutcome.FAILED)
                return HandoverNotificationResult(
                    HandoverNotificationOutcome.DELIVERED,
                    published.publish_result,
                    delivery_proof=True,
                )
            if _delivery_terminal_failure(deliveries):
                return replace(published, outcome=HandoverNotificationOutcome.FAILED)
            if self._monotonic() >= monotonic_deadline:
                return replace(published, outcome=HandoverNotificationOutcome.FAILED)
            remaining = (caller_deadline - _utc(self._clock())).total_seconds()
            if remaining <= 0:
                return replace(published, outcome=HandoverNotificationOutcome.FAILED)
            self._sleep(min(self._poll_seconds, remaining))

    def _deadline_reached(
        self, caller_deadline: datetime, monotonic_deadline: float
    ) -> bool:
        return (
            self._monotonic() >= monotonic_deadline
            or _utc(self._clock()) >= caller_deadline
        )

    def _deliveries(self, event: NotificationEvent) -> tuple[NotificationStoredDelivery, ...]:
        with self._uow_factory() as uow:
            deliveries = uow.notifications.list_deliveries(
                source=event.source, event_id=event.id
            )
            uow.commit()
        return deliveries


class DesktopAgentNotificationRuntime:
    """Composition root: один существующий storage engine, dispatcher и Agent API."""

    def __init__(
        self,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        credential: DesktopAgentCredential | None,
        clock: Callable[[], datetime] | None = None,
        telemetry: object | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._credential = credential
        self._enabled = credential is not None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._telemetry = telemetry
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._session_lock = threading.Lock()
        self._sessions: dict[tuple[str, str], UUID] = {}
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._worker_stopping = False
        self._fatal_stop_reason: str | None = None
        self._authenticator = DesktopAgentAuthenticator(credential)
        profiles = tuple(sorted(credential.profiles)) if credential is not None else ()
        channel = DesktopAgentChannel(wake=self.wake)
        channel_catalog = NotificationChannelCatalog((channel,)) if self._enabled else NotificationChannelCatalog()
        policy = _agent_policy(profiles)
        self._publisher = NotificationPublisher(
            uow_factory,
            registry=default_registry(),
            policy=policy,
            channel_catalog=channel_catalog,
            renderer_catalog=build_default_renderer_catalog(),
            retry_policy=retry_policy,
            clock=self._clock,
            telemetry=telemetry,
        )
        self._dispatcher = NotificationDispatcher(
            uow_factory,
            channel_catalog=channel_catalog,
            retry_policy=retry_policy,
            clock=self._clock,
            telemetry=telemetry,
        )
        self._history = DesktopAgentHistoryService(
            uow_factory,
            telemetry=telemetry,
            clock=self._clock,
        )
        self._waiter = NotificationHandoverWaiter(
            self._publisher,
            self._dispatcher,
            uow_factory,
            clock=self._clock,
        )

    @classmethod
    def from_environment(
        cls,
        uow_factory: Callable[[], NotificationUnitOfWork],
        *,
        environment: Mapping[str, str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        telemetry: object | None = None,
    ) -> DesktopAgentNotificationRuntime:
        source = os.environ if environment is None else environment
        try:
            credential = DesktopAgentCredential.from_environment(environment)
        except DesktopAgentConfigurationError:
            if _agent_identity_configuration_present(source):
                raise
            credential = None
        return cls(
            uow_factory,
            credential=credential,
            clock=clock,
            telemetry=telemetry,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled and self._fatal_stop_reason is None

    @property
    def fatal_stop_reason(self) -> str | None:
        return self._fatal_stop_reason

    @property
    def authenticator(self) -> DesktopAgentAuthenticator:
        return self._authenticator

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(sorted(self._credential.profiles)) if self._credential else ()

    def open_agent_session(
        self, principal: DesktopAgentPrincipal, *, profile_id: str
    ) -> UUID:
        """Создать server-issued session identity для одной profile connection."""

        if not self.enabled:
            raise DesktopAgentUnavailableError("Desktop Agent channel не настроен.")
        _authorize_profile(principal, profile_id)
        session_epoch = uuid4()
        with self._session_lock:
            self._sessions[(principal.agent_id, profile_id)] = session_epoch
        return session_epoch

    def is_agent_session_current(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        session_epoch: UUID,
    ) -> bool:
        if not isinstance(session_epoch, UUID):
            return False
        with self._session_lock:
            return self._sessions.get((principal.agent_id, profile_id)) == session_epoch

    def close_agent_session(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        session_epoch: UUID,
    ) -> None:
        with self._session_lock:
            key = (principal.agent_id, profile_id)
            if self._sessions.get(key) == session_epoch:
                self._sessions.pop(key, None)

    def start(self) -> None:
        with self._worker_lock:
            if (
                not self._enabled
                or self._worker is not None
                or self._worker_stopping
                or self._fatal_stop_reason is not None
            ):
                return
            self._stop_event.clear()
            worker = threading.Thread(
                target=self._run_dispatcher,
                name="azurpilot-notification-agent-dispatcher",
                daemon=True,
            )
            self._worker = worker
            try:
                worker.start()
            except Exception:
                self._worker = None
                raise
        _record_agent_connection(self._telemetry, "started")

    def stop(self) -> None:
        with self._worker_lock:
            self._worker_stopping = True
            self._stop_event.set()
            self._wake_event.set()
            worker = self._worker
        try:
            if worker is not None:
                worker.join(timeout=5.0)
        finally:
            with self._worker_lock:
                if self._worker is worker and (
                    worker is None or not worker.is_alive()
                ):
                    self._worker = None
                self._worker_stopping = False
        with self._session_lock:
            self._sessions.clear()
        if self._enabled:
            _record_agent_connection(self._telemetry, "stopped")

    def wake(self) -> None:
        self._wake_event.set()

    def record_agent_connection(self, status: str) -> None:
        _record_agent_connection(self._telemetry, status)

    def record_agent_reconnect(self) -> None:
        method = getattr(self._telemetry, "record_agent_reconnect", None)
        if callable(method):
            try:
                method()
            except Exception:  # noqa: BLE001 - телеметрия остаётся fail-open.
                return

    def record_agent_backlog(self, *, status: str) -> None:
        method = getattr(self._telemetry, "record_agent_backlog", None)
        if callable(method):
            try:
                method(status=status)
            except Exception:  # noqa: BLE001 - телеметрия остаётся fail-open.
                return

    def read_agent_batch(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        cursor: str | None,
        limit: int,
        session_epoch: UUID | None = None,
    ) -> tuple[DesktopAgentDeliveryFrame, ...]:
        if not self.enabled:
            raise DesktopAgentUnavailableError("Desktop Agent channel не настроен.")
        _authorize_profile(principal, profile_id)
        if session_epoch is None:
            with self._session_lock:
                session_epoch = self._sessions.get((principal.agent_id, profile_id))
            if session_epoch is None:
                session_epoch = self.open_agent_session(principal, profile_id=profile_id)
        elif not self.is_agent_session_current(
            principal,
            profile_id=profile_id,
            session_epoch=session_epoch,
        ):
            raise DesktopAgentAuthorizationError("Agent session identity устарела.")
        return self._history.read(
            principal,
            profile_id=profile_id,
            cursor=cursor,
            limit=limit,
            session_epoch=session_epoch,
        )

    def validate_agent_cursor(
        self,
        principal: DesktopAgentPrincipal,
        *,
        profile_id: str,
        cursor: str | None,
    ) -> None:
        if not self.enabled:
            raise DesktopAgentUnavailableError("Desktop Agent channel не настроен.")
        self._history.validate_cursor(
            principal,
            profile_id=profile_id,
            cursor=cursor,
        )

    def acknowledge_agent(
        self, principal: DesktopAgentPrincipal, ack: NotificationAgentAck
    ) -> NotificationAgentAckResult:
        if not self.enabled:
            raise DesktopAgentUnavailableError("Desktop Agent channel не настроен.")
        _authorize_profile(principal, ack.profile_id)
        if ack.agent_id != principal.agent_id:
            raise DesktopAgentAuthorizationError("Agent identity ACK не совпадает с credential.")
        if not self.is_agent_session_current(
            principal,
            profile_id=ack.profile_id,
            session_epoch=ack.session_epoch,
        ):
            return NotificationAgentAckResult(
                NotificationAgentAckStatus.REJECTED,
                "session_identity_mismatch",
            )
        return self._history.acknowledge(principal, ack)

    def delivery_state(
        self, principal: DesktopAgentPrincipal, frame: DesktopAgentDeliveryFrame
    ) -> NotificationStoredDelivery | None:
        if not self.enabled:
            raise DesktopAgentUnavailableError("Desktop Agent channel не настроен.")
        profile_id = str(frame.document["profile_id"])
        session_epoch = UUID(str(frame.document["session_epoch"]))
        if not self.is_agent_session_current(
            principal,
            profile_id=profile_id,
            session_epoch=session_epoch,
        ):
            raise DesktopAgentAuthorizationError("Agent session identity устарела.")
        return self._history.delivery_state(principal, frame)

    def notify_preemption(
        self,
        profile_id: str,
        operation_id: str,
        session_id: str | None,
        *,
        deadline: datetime | None,
        runtime_state: RuntimeStateSnapshot | None,
    ) -> NotificationOutcome:
        if not self.enabled:
            return NotificationOutcome.UNAVAILABLE
        if deadline is None or runtime_state is None:
            return NotificationOutcome.FAILED
        try:
            event = build_handover_preemption_event(
                profile_id,
                operation_id,
                session_id,
                deadline=deadline,
                runtime_state=runtime_state,
                clock=self._clock,
            )
            result = self._waiter.publish_and_wait(event, deadline=deadline)
        except (NotificationValidationError, DesktopAgentError, StorageError):
            return NotificationOutcome.FAILED
        if result.is_proof:
            return NotificationOutcome.DELIVERED
        if result.outcome is HandoverNotificationOutcome.UNAVAILABLE:
            return NotificationOutcome.UNAVAILABLE
        return NotificationOutcome.FAILED

    def _run_dispatcher(self) -> None:
        try:
            storage_backoff = AGENT_POLL_SECONDS
            while not self._stop_event.is_set():
                try:
                    recovered = self._dispatcher.recover_expired()
                    if recovered:
                        method = getattr(self._telemetry, "record_agent_timeout", None)
                        if callable(method):
                            try:
                                method()
                            except Exception:  # noqa: BLE001 - телеметрия остаётся fail-open.
                                pass
                    self._dispatcher.dispatch_once()
                except StorageUnavailableError:
                    self.record_agent_backlog(status="error")
                    self._wake_event.wait(timeout=storage_backoff)
                    self._wake_event.clear()
                    storage_backoff = min(
                        AGENT_STORAGE_BACKOFF_MAX_SECONDS, storage_backoff * 2
                    )
                    continue
                except Exception:  # noqa: BLE001 - фатальная ошибка worker завершает его fail-closed.
                    _record_agent_connection(self._telemetry, "unavailable")
                    self._fatal_stop_reason = "dispatcher_failed"
                    self._stop_event.set()
                    return
                storage_backoff = AGENT_POLL_SECONDS
                self._wake_event.wait(timeout=AGENT_POLL_SECONDS)
                self._wake_event.clear()
        finally:
            with self._worker_lock:
                if self._worker is threading.current_thread():
                    self._worker = None


def build_handover_preemption_event(
    profile_id: str,
    operation_id: str,
    session_id: str | None,
    *,
    deadline: datetime,
    runtime_state: RuntimeStateSnapshot,
    clock: Callable[[], datetime] | None = None,
) -> NotificationEvent:
    now = _utc(clock() if clock is not None else datetime.now(UTC))
    deadline_at = _utc(deadline)
    if not _SAFE_PROFILE_RE.fullmatch(profile_id):
        raise NotificationValidationError("profile_id_invalid")
    if not _SAFE_ID_RE.fullmatch(operation_id):
        raise NotificationValidationError("operation_id_invalid")
    if session_id is not None and not _SAFE_ID_RE.fullmatch(session_id):
        raise NotificationValidationError("session_id_invalid")
    if not isinstance(runtime_state, RuntimeStateSnapshot) or runtime_state.profile != profile_id:
        raise NotificationValidationError("runtime_state_identity_invalid")
    if runtime_state.operation_id not in {None, operation_id}:
        raise NotificationValidationError("runtime_operation_identity_mismatch")
    if session_id is not None and runtime_state.session_id not in {None, session_id}:
        raise NotificationValidationError("runtime_session_identity_mismatch")
    if not now < deadline_at:
        raise NotificationValidationError("handover_deadline_expired")
    current_task = None
    if runtime_state.current_task is not None and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", runtime_state.current_task
    ):
        current_task = NotificationSubject(kind="task", id=runtime_state.current_task)
    payload = HandoverPreemptionPayload(
        operation_id=operation_id,
        source_profile_id=profile_id,
        owner_epoch=_owner_epoch(runtime_state),
        reason_code="busy_handover",
        deadline_at=deadline_at,
        session_id=session_id,
        current_task=current_task,
    )
    return NotificationEvent(
        id=uuid4(),
        source="runtime",
        type="runtime.handover.preemption_requested",
        schema_version=1,
        profile_id=profile_id,
        severity=NotificationSeverity.CRITICAL,
        occurred_at=now,
        data=payload,
        runtime_instance_id=profile_id,
        correlation=NotificationCorrelation(runtime_session_id=session_id)
        if session_id is not None
        else None,
        dedup_key=operation_id,
    )


def _agent_policy(profiles: tuple[str, ...]):
    from module.application.notifications.models import (
        NotificationPolicy,
        NotificationRule,
        NotificationRuleMatcher,
        PolicyAction,
    )

    return NotificationPolicy(
        version=1,
        rules=tuple(
            NotificationRule(
                rule_id=f"desktop-agent-{profile}",
                priority=index,
                matcher=NotificationRuleMatcher(profile_id=profile),
                action=PolicyAction(
                    channel_instance_ids=(DESKTOP_AGENT_CHANNEL_INSTANCE_ID,),
                    locale="ru-RU",
                ),
            )
            for index, profile in enumerate(profiles)
        ),
        default_action=PolicyAction(suppression_reason="profile_not_configured"),
    )


def _authorize_profile(principal: DesktopAgentPrincipal, profile_id: str) -> None:
    if not isinstance(principal, DesktopAgentPrincipal) or not principal.can_access(profile_id):
        raise DesktopAgentAuthorizationError("Agent не имеет доступа к profile.")


def _validate_agent_token(token: str) -> None:
    if not isinstance(token, str) or not 16 <= len(token) <= MAX_AGENT_TOKEN_LENGTH:
        raise DesktopAgentConfigurationError("Agent token имеет неверный размер.")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token):
        raise DesktopAgentConfigurationError("Agent token содержит недопустимые символы.")


def _agent_identity_configuration_present(
    environment: Mapping[str, str | None],
) -> bool:
    return any(
        environment.get(name) is not None
        for name in (
            "AZURPILOT_NOTIFICATION_AGENT_ID",
            "AZURPILOT_NOTIFICATION_AGENT_PROFILES",
            "AZURPILOT_NOTIFICATION_AGENT_TOKEN",
            "AZURPILOT_NOTIFICATION_AGENT_TOKEN_FILE",
        )
    )


def _read_token_file(path_value: str) -> str:
    if not isinstance(path_value, str) or not path_value or len(path_value) > 4096:
        raise DesktopAgentConfigurationError("Agent token file path имеет неверный формат.")
    try:
        raw = Path(path_value).read_bytes()
    except (OSError, ValueError):
        raise DesktopAgentConfigurationError("Agent token file недоступен.") from None
    if len(raw) > MAX_AGENT_TOKEN_LENGTH + 2:
        raise DesktopAgentConfigurationError("Agent token file превысил bounded размер.")
    try:
        return raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise DesktopAgentConfigurationError("Agent token file имеет неверную кодировку.") from None


def _owner_epoch(runtime_state: RuntimeStateSnapshot) -> int:
    if runtime_state.worker_pid is not None and runtime_state.worker_created_at is not None:
        identity = f"{runtime_state.worker_pid}:{runtime_state.worker_created_at:.6f}".encode()
    else:
        identity = b"unknown"
    value = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") & ((1 << 63) - 1)
    return value or 1


def _delivery_proof(
    deliveries: tuple[NotificationStoredDelivery, ...], deadline: datetime
) -> bool:
    return bool(deliveries) and all(
        delivery.state is DeliveryState.DELIVERED
        and delivery.updated_at < deadline
        for delivery in deliveries
    )


def _delivery_terminal_failure(
    deliveries: tuple[NotificationStoredDelivery, ...]
) -> bool:
    if not deliveries:
        return True
    return any(
        delivery.state in {
            DeliveryState.FAILED,
            DeliveryState.SUPPRESSED,
            DeliveryState.PROVIDER_ACCEPTED,
        }
        for delivery in deliveries
    )


def _record_agent_connection(telemetry: object | None, status: str) -> None:
    method = getattr(telemetry, "record_agent_connection", None)
    if callable(method):
        try:
            method(status=status)
        except Exception:  # noqa: BLE001 - телеметрия остаётся fail-open.
            return


def _record_agent_ack(telemetry: object | None, status: NotificationAgentAckStatus) -> None:
    method = getattr(telemetry, "record_agent_ack", None)
    if callable(method):
        try:
            method(status=status.value)
        except Exception:  # noqa: BLE001 - телеметрия остаётся fail-open.
            return


def _utc(value: datetime) -> datetime:
    normalized = ensure_aware_utc(value)
    if normalized is None:
        raise DesktopAgentRequestError("Timestamp должен быть timezone-aware.")
    return normalized


__all__ = [
    "AGENT_STREAM_MAX_SECONDS",
    "DESKTOP_AGENT_ACK_PATH",
    "DESKTOP_AGENT_CHANNEL_INSTANCE_ID",
    "DESKTOP_AGENT_CHANNEL_TYPE",
    "DESKTOP_AGENT_STREAM_PATH",
    "MAX_AGENT_ACK_BODY_BYTES",
    "MAX_AGENT_BATCH_SIZE",
    "RECOVERABLE_AGENT_ACK_REASONS",
    "DesktopAgentAuthenticator",
    "DesktopAgentAuthorizationError",
    "DesktopAgentChannel",
    "DesktopAgentConfigurationError",
    "DesktopAgentCredential",
    "DesktopAgentDeliveryFrame",
    "DesktopAgentError",
    "DesktopAgentHistoryService",
    "DesktopAgentNotificationRuntime",
    "DesktopAgentPrincipal",
    "DesktopAgentRequestError",
    "DesktopAgentUnavailableError",
    "NotificationCursor",
    "NotificationCursorError",
    "NotificationHandoverWaiter",
    "build_handover_preemption_event",
    "notification_agent_document",
    "parse_agent_ack_document",
    "validate_agent_delivery_document",
]
