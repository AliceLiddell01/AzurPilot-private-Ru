"""Типизированные модели durable notification foundation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from re import IGNORECASE, fullmatch
from re import compile as compile_regex
from typing import Final
from uuid import UUID, uuid4

_TOKEN_RE = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"
DOTTED_TYPE_RE = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
_HEX_RE = r"[0-9a-f]+"
_SAFE_ERROR_CODE_RE = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
_UNSAFE_RESULT_RE = compile_regex(
    r"(?:https?://|(?:password|secret|token|credential|authorization|bearer|"
    r"traceback|stacktrace|serial)(?:\b|_)|device_id(?:\b|_))",
    IGNORECASE,
)

MAX_CHANNEL_PAYLOAD_BYTES: Final = 64 * 1024
MAX_CHANNEL_TITLE_LENGTH: Final = 4096
MAX_CHANNEL_BODY_LENGTH: Final = 64 * 1024


class NotificationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_SEVERITY_ORDER: Final[dict[NotificationSeverity, int]] = {
    NotificationSeverity.INFO: 10,
    NotificationSeverity.WARNING: 20,
    NotificationSeverity.ERROR: 30,
    NotificationSeverity.CRITICAL: 40,
}


class NotificationSensitivity(str, Enum):
    NORMAL = "NORMAL"
    SENSITIVE = "SENSITIVE"


class PolicyState(str, Enum):
    ROUTED = "ROUTED"
    SUPPRESSED = "SUPPRESSED"


class DeliveryState(str, Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    AWAITING_AGENT_ACK = "AWAITING_AGENT_ACK"
    DELIVERED = "DELIVERED"
    SUPPRESSED = "SUPPRESSED"


class DeliveryResultClass(str, Enum):
    DELIVERED = "DELIVERED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    UNAVAILABLE = "UNAVAILABLE"
    SUPPRESSED = "SUPPRESSED"


class ReceiptStrength(str, Enum):
    NONE = "NONE"
    PROVIDER_ACCEPTANCE = "PROVIDER_ACCEPTANCE"
    AGENT_ACK = "AGENT_ACK"


class PublishStatus(str, Enum):
    PERSISTED = "persisted"
    DUPLICATE = "duplicate"
    SUPPRESSED = "suppressed"
    IDENTITY_CONFLICT = "identity_conflict"
    VALIDATION_FAILED = "validation_failed"
    UNAVAILABLE = "unavailable"


class HandoverNotificationOutcome(str, Enum):
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


def _valid_token(value: object, *, limit: int, pattern: str = _TOKEN_RE) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and fullmatch(pattern, value) is not None


def ensure_aware_utc(value: object) -> datetime | None:
    """Вернуть UTC-время только для timezone-aware datetime."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class NotificationSubject:
    kind: str
    id: str

    def is_valid(self) -> bool:
        return _valid_token(self.kind, limit=32) and _valid_token(self.id, limit=128)


@dataclass(frozen=True, slots=True)
class NotificationCorrelation:
    task_id: str | None = None
    runtime_session_id: str | None = None
    parent_event_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def is_valid(self) -> bool:
        for value, limit in (
            (self.task_id, 128),
            (self.runtime_session_id, 128),
        ):
            if value is not None and not _valid_token(value, limit=limit):
                return False
        if self.trace_id is not None and (
            not isinstance(self.trace_id, str)
            or fullmatch(_HEX_RE, self.trace_id) is None
            or len(self.trace_id) not in {16, 32}
        ):
            return False
        return self.span_id is None or (
            isinstance(self.span_id, str)
            and fullmatch(_HEX_RE, self.span_id) is not None
            and len(self.span_id) == 16
        )


@dataclass(frozen=True, slots=True)
class HandoverPreemptionPayload:
    operation_id: str
    source_profile_id: str
    owner_epoch: int
    reason_code: str
    deadline_at: datetime
    session_id: str | None = None
    current_task: NotificationSubject | None = None

    def is_valid(self, *, event_profile_id: str, occurred_at: datetime) -> bool:
        deadline = ensure_aware_utc(self.deadline_at)
        occurred = ensure_aware_utc(occurred_at)
        return (
            _valid_token(self.operation_id, limit=128)
            and _valid_token(self.source_profile_id, limit=128)
            and self.source_profile_id == event_profile_id
            and isinstance(self.owner_epoch, int)
            and not isinstance(self.owner_epoch, bool)
            and self.owner_epoch >= 0
            and _valid_token(self.reason_code, limit=64)
            and (
                self.session_id is None
                or _valid_token(self.session_id, limit=128)
            )
            and (self.current_task is None or self.current_task.is_valid())
            and occurred is not None
            and deadline is not None
            and occurred <= deadline <= occurred + timedelta(seconds=300)
        )


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    id: UUID
    source: str
    type: str
    schema_version: int
    profile_id: str
    severity: NotificationSeverity
    occurred_at: datetime
    data: object
    runtime_instance_id: str | None = None
    subject: NotificationSubject | None = None
    dedup_key: str | None = None
    correlation: NotificationCorrelation | None = None
    sensitivity: NotificationSensitivity = NotificationSensitivity.NORMAL
    persisted_at: datetime | None = None
    profile_sequence: int | None = None
    payload_digest: str | None = None

    @property
    def event_type(self) -> str:
        return self.type

    @property
    def subject_kind(self) -> str | None:
        return self.subject.kind if self.subject else None

    @property
    def subject_id(self) -> str | None:
        return self.subject.id if self.subject else None

    def with_persistence(
        self, *, persisted_at: datetime, profile_sequence: int, payload_digest: str
    ) -> NotificationEvent:
        return replace(
            self,
            persisted_at=persisted_at,
            profile_sequence=profile_sequence,
            payload_digest=payload_digest,
        )


@dataclass(frozen=True, slots=True)
class NotificationEventProjection:
    id: UUID
    source: str
    type: str
    schema_version: int
    profile_id: str
    severity: NotificationSeverity
    occurred_at: datetime
    data: object
    subject: NotificationSubject | None
    sensitivity: NotificationSensitivity


@dataclass(frozen=True, slots=True)
class RenderedSnapshot:
    locale: str
    renderer_id: str
    renderer_version: str
    title: str
    body: str

    def is_valid(self, *, max_title: int, max_body: int, max_payload_bytes: int) -> bool:
        if not _valid_token(self.locale, limit=32, pattern=r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})?"):
            return False
        if not _valid_token(self.renderer_id, limit=64):
            return False
        if not _valid_token(self.renderer_version, limit=32):
            return False
        if not isinstance(self.title, str) or not 0 < len(self.title) <= max_title:
            return False
        if not isinstance(self.body, str) or not 0 < len(self.body) <= max_body:
            return False
        if any(ord(character) < 32 for character in self.title):
            return False
        if any(ord(character) < 32 and character != "\n" for character in self.body):
            return False
        return len((self.title + self.body).encode("utf-8")) <= max_payload_bytes


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    max_payload_bytes: int = 4096
    max_title_length: int = 256
    max_body_length: int = 2048
    markup_mode: str = "plain"
    idempotency: bool = True
    receipt_strength: ReceiptStrength = ReceiptStrength.NONE
    health_check: bool = False

    def is_valid(self) -> bool:
        return (
            isinstance(self.max_payload_bytes, int)
            and not isinstance(self.max_payload_bytes, bool)
            and isinstance(self.max_title_length, int)
            and not isinstance(self.max_title_length, bool)
            and isinstance(self.max_body_length, int)
            and not isinstance(self.max_body_length, bool)
            and 256 <= self.max_payload_bytes <= MAX_CHANNEL_PAYLOAD_BYTES
            and 1 <= self.max_title_length <= min(
                MAX_CHANNEL_TITLE_LENGTH, self.max_payload_bytes
            )
            and 1 <= self.max_body_length <= min(
                MAX_CHANNEL_BODY_LENGTH, self.max_payload_bytes
            )
            and _valid_token(self.markup_mode, limit=32)
            and isinstance(self.idempotency, bool)
            and isinstance(self.receipt_strength, ReceiptStrength)
            and isinstance(self.health_check, bool)
        )


@dataclass(frozen=True, slots=True)
class NotificationAttribute:
    key: str
    value: str | int | bool

    def is_valid(self) -> bool:
        return _valid_token(self.key, limit=64) and (
            isinstance(self.value, (str, int, bool))
            and (not isinstance(self.value, str) or len(self.value) <= 256)
        )


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    delivery_id: UUID
    event: NotificationEventProjection
    rendered_snapshot: RenderedSnapshot
    idempotency_key: str
    timeout_seconds: float
    attributes: tuple[NotificationAttribute, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    result_class: DeliveryResultClass
    safe_error_code: str | None = None
    safe_error_summary: str | None = None
    retry_after_seconds: int | None = None
    provider_message_id: str | None = None
    received_at: datetime | None = None

    def is_valid(self) -> bool:
        if not isinstance(self.result_class, DeliveryResultClass):
            return False
        if self.result_class in {
            DeliveryResultClass.DELIVERED,
            DeliveryResultClass.PROVIDER_ACCEPTED,
        } and self.safe_error_code is not None:
            return False
        if (
            self.result_class is DeliveryResultClass.PERMANENT_FAILURE
            and self.retry_after_seconds is not None
        ):
            return False
        if self.safe_error_code is not None and (
            not isinstance(self.safe_error_code, str)
            or fullmatch(_SAFE_ERROR_CODE_RE, self.safe_error_code) is None
            or _UNSAFE_RESULT_RE.search(self.safe_error_code) is not None
        ):
            return False
        if self.safe_error_summary is not None and (
            not isinstance(self.safe_error_summary, str)
            or len(self.safe_error_summary) > 256
            or any(ord(character) < 32 for character in self.safe_error_summary)
            or _UNSAFE_RESULT_RE.search(self.safe_error_summary) is not None
        ):
            return False
        if self.retry_after_seconds is not None and (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or not 0 <= self.retry_after_seconds <= 3600
        ):
            return False
        if self.provider_message_id is not None and (
            not isinstance(self.provider_message_id, str)
            or not 0 < len(self.provider_message_id) <= 128
            or fullmatch(_TOKEN_RE, self.provider_message_id) is None
            or _UNSAFE_RESULT_RE.search(self.provider_message_id) is not None
        ):
            return False
        return self.received_at is None or ensure_aware_utc(self.received_at) is not None

    @classmethod
    def delivered(cls, *, provider_message_id: str | None = None) -> DeliveryResult:
        return cls(DeliveryResultClass.DELIVERED, provider_message_id=provider_message_id)

    @classmethod
    def provider_accepted(cls, *, provider_message_id: str | None = None) -> DeliveryResult:
        return cls(
            DeliveryResultClass.PROVIDER_ACCEPTED,
            provider_message_id=provider_message_id,
        )

    @classmethod
    def transient_failure(
        cls, code: str, *, retry_after_seconds: int | None = None, summary: str | None = None
    ) -> DeliveryResult:
        return cls(
            DeliveryResultClass.TRANSIENT_FAILURE,
            safe_error_code=code,
            safe_error_summary=summary,
            retry_after_seconds=retry_after_seconds,
        )

    @classmethod
    def permanent_failure(cls, code: str, *, summary: str | None = None) -> DeliveryResult:
        return cls(
            DeliveryResultClass.PERMANENT_FAILURE,
            safe_error_code=code,
            safe_error_summary=summary,
        )

    @classmethod
    def unavailable(cls, code: str = "channel_unavailable") -> DeliveryResult:
        return cls(DeliveryResultClass.UNAVAILABLE, safe_error_code=code)


@dataclass(frozen=True, slots=True)
class PolicyAction:
    channel_instance_ids: tuple[str, ...] = ()
    suppression_reason: str | None = None
    locale: str = "ru-RU"
    presentation_profile: str = "default"

    @property
    def suppressed(self) -> bool:
        return self.suppression_reason is not None or not self.channel_instance_ids


@dataclass(frozen=True, slots=True)
class NotificationRuleMatcher:
    exact_type: str | None = None
    type_prefix: str | None = None
    exact_severity: NotificationSeverity | None = None
    minimum_severity: NotificationSeverity | None = None
    profile_id: str | None = None
    subject_kind: str | None = None
    subject_id: str | None = None
    source: str | None = None

    def matches(self, event: NotificationEvent) -> bool:
        if self.exact_type is not None and event.type != self.exact_type:
            return False
        if self.type_prefix is not None and not (
            event.type == self.type_prefix or event.type.startswith(self.type_prefix + ".")
        ):
            return False
        if self.exact_severity is not None and event.severity is not self.exact_severity:
            return False
        if self.minimum_severity is not None:
            if not isinstance(event.severity, NotificationSeverity):
                return False
            actual = _SEVERITY_ORDER.get(event.severity)
            minimum = _SEVERITY_ORDER.get(self.minimum_severity)
            if actual is None or minimum is None or actual < minimum:
                return False
        if self.profile_id is not None and event.profile_id != self.profile_id:
            return False
        if self.source is not None and event.source != self.source:
            return False
        if self.subject_kind is not None and event.subject_kind != self.subject_kind:
            return False
        return self.subject_id is None or event.subject_id == self.subject_id


@dataclass(frozen=True, slots=True)
class NotificationRule:
    rule_id: str
    priority: int
    matcher: NotificationRuleMatcher
    action: PolicyAction


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    version: int
    rules: tuple[NotificationRule, ...]
    default_action: PolicyAction
    global_enabled: bool = True


@dataclass(frozen=True, slots=True)
class NotificationPolicySnapshot:
    policy_version: int
    rule_id: str | None
    action: PolicyAction


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    state: PolicyState
    policy_version: int
    matched_rule_id: str | None
    reason: str
    snapshot: NotificationPolicySnapshot
    snapshot_hash: str

    @property
    def channel_instance_ids(self) -> tuple[str, ...]:
        return self.snapshot.action.channel_instance_ids


@dataclass(frozen=True, slots=True)
class NotificationDeliveryPlan:
    id: UUID
    event_id: UUID
    channel_instance_id: str
    channel_type: str
    priority: int
    next_attempt_at: datetime
    deadline_at: datetime | None
    rendered_snapshot: RenderedSnapshot
    idempotency_key: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class NotificationStoredEvent:
    event: NotificationEvent
    payload_document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class NotificationStoredDelivery:
    id: UUID
    event_id: UUID
    channel_instance_id: str
    channel_type: str
    state: DeliveryState
    priority: int
    created_at: datetime
    next_attempt_at: datetime
    deadline_at: datetime | None
    attempt_count: int
    lease_owner: str | None
    lease_token: UUID | None
    lease_until: datetime | None
    last_safe_error_code: str | None
    rendered_snapshot: RenderedSnapshot
    idempotency_key: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationStoredAttempt:
    delivery_id: UUID
    attempt_ordinal: int
    started_at: datetime
    finished_at: datetime | None
    result_class: DeliveryResultClass | None
    safe_error_code: str | None
    safe_error_summary: str | None
    retry_after_seconds: int | None
    provider_message_id: str | None
    lease_token: UUID | None
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationPersistenceResult:
    status: PublishStatus
    event: NotificationStoredEvent | None = None
    decision: PolicyDecision | None = None
    deliveries: tuple[NotificationStoredDelivery, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery: NotificationStoredDelivery
    event: NotificationStoredEvent
    attempt_ordinal: int
    lease_token: UUID
    prepared: PreparedDelivery


@dataclass(frozen=True, slots=True)
class DeliveryUpdate:
    state: DeliveryState
    result: DeliveryResult
    next_attempt_at: datetime
    lease_until: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """Итог bounded batch; failed означает исключение обработки элемента."""

    claimed: int
    processed: int
    updated: int
    stale_updates: int
    failed: int = 0


@dataclass(frozen=True, slots=True)
class PublishResult:
    status: PublishStatus
    event_id: UUID
    profile_sequence: int | None = None
    decision: PolicyDecision | None = None
    deliveries: tuple[NotificationStoredDelivery, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HandoverNotificationResult:
    outcome: HandoverNotificationOutcome
    publish_result: PublishResult
    delivery_proof: bool = False

    @property
    def is_proof(self) -> bool:
        return self.delivery_proof and self.outcome is HandoverNotificationOutcome.DELIVERED


def new_uuid() -> UUID:
    return uuid4()


__all__ = [
    "DOTTED_TYPE_RE",
    "ChannelCapabilities",
    "ClaimedDelivery",
    "DeliveryResult",
    "DeliveryResultClass",
    "DeliveryState",
    "DeliveryUpdate",
    "DispatchReport",
    "HandoverNotificationOutcome",
    "HandoverNotificationResult",
    "HandoverPreemptionPayload",
    "NotificationAttribute",
    "NotificationCorrelation",
    "NotificationDeliveryPlan",
    "NotificationEvent",
    "NotificationEventProjection",
    "NotificationPolicy",
    "NotificationPolicySnapshot",
    "NotificationRule",
    "NotificationRuleMatcher",
    "NotificationSensitivity",
    "NotificationSeverity",
    "NotificationStoredAttempt",
    "NotificationStoredDelivery",
    "NotificationStoredEvent",
    "NotificationSubject",
    "PolicyAction",
    "PolicyDecision",
    "PolicyState",
    "PreparedDelivery",
    "PublishResult",
    "PublishStatus",
    "ReceiptStrength",
    "RenderedSnapshot",
    "ensure_aware_utc",
    "new_uuid",
]
