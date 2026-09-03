"""Типизированные модели ядра платформы уведомлений."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from uuid import UUID

_SAFE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9]+(?:[._][a-z0-9]+)*$")
MAX_DEDUP_KEY_LENGTH = 256
MAX_IDENTIFIER_LENGTH = 128


def _require_text(value: str, *, field: str, maximum: int = MAX_IDENTIFIER_LENGTH) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    if len(value) > maximum:
        raise ValueError(f"{field} превышает допустимую длину {maximum}")


def _require_safe_code(value: str | None, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _SAFE_CODE_RE.fullmatch(value):
        raise ValueError(f"{field} должен быть ограниченным безопасным кодом")


def require_utc(value: datetime, *, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} должен содержать часовой пояс UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} должен быть задан в UTC")


class NotificationSeverity(IntEnum):
    """Канонический уровень серьёзности события."""

    INFO = 10
    WARNING = 20
    ERROR = 30
    CRITICAL = 40


class NotificationSensitivity(StrEnum):
    """Класс чувствительности безопасной проекции события."""

    NORMAL = "NORMAL"
    SENSITIVE = "SENSITIVE"


class SubjectKind(StrEnum):
    """Ограниченные виды предметных ссылок через application boundary."""

    TASK = "task"
    CAMPAIGN = "campaign"
    FLEET = "fleet"
    RUNTIME_COMPONENT = "runtime_component"
    COMMISSION = "commission"
    OPSI_CONTEXT = "opsi_context"


class CampaignStopKind(StrEnum):
    RUN_COUNT = "run_count"
    LEVEL = "level"
    COIN = "coin"
    NEW_SHIP = "new_ship"


class ShipExpTargetScope(StrEnum):
    FLEET = "fleet"
    CUSTOM_POSITIONS = "custom_positions"


@dataclass(frozen=True, slots=True)
class SubjectRef:
    kind: SubjectKind
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SubjectKind):
            raise ValueError("subject.kind должен быть значением SubjectKind")
        _require_text(self.id, field="subject.id")


@dataclass(frozen=True, slots=True)
class TaskEventPayload:
    task: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.task, field="data.task")
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class TaskFailureLimitPayload:
    task: str
    consecutive_failures: int

    def __post_init__(self) -> None:
        _require_text(self.task, field="data.task")
        if not isinstance(self.consecutive_failures, int) or isinstance(
            self.consecutive_failures,
            bool,
        ):
            raise ValueError("data.consecutive_failures должен быть целым числом")
        if self.consecutive_failures <= 0 or self.consecutive_failures > 1_000_000:
            raise ValueError(
                "data.consecutive_failures находится вне допустимого диапазона"
            )


@dataclass(frozen=True, slots=True)
class RuntimeEventPayload:
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class CampaignStopConditionPayload:
    kind: CampaignStopKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CampaignStopKind):
            raise ValueError("data.kind должен быть значением CampaignStopKind")


@dataclass(frozen=True, slots=True)
class CampaignConfigurationFailurePayload:
    reason_code: str

    def __post_init__(self) -> None:
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class CommissionRewardPayload:
    reward_count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.reward_count, int) or isinstance(self.reward_count, bool):
            raise ValueError("data.reward_count должен быть целым числом")
        if self.reward_count <= 0 or self.reward_count > 10_000:
            raise ValueError("data.reward_count находится вне допустимого диапазона")


@dataclass(frozen=True, slots=True)
class OpsiActionPointChangedPayload:
    previous: int | None
    current: int

    def __post_init__(self) -> None:
        for name, value in (("previous", self.previous), ("current", self.current)):
            if value is None and name == "previous":
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"data.{name} должен быть неотрицательным целым числом"
                )


@dataclass(frozen=True, slots=True)
class OpsiActionPointLowPayload:
    current: int
    minimum: int

    def __post_init__(self) -> None:
        for name, value in (("current", self.current), ("minimum", self.minimum)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"data.{name} должен быть неотрицательным целым числом"
                )


@dataclass(frozen=True, slots=True)
class OpsiResourcesInsufficientPayload:
    action_points: int | None
    yellow_coins: int | None

    def __post_init__(self) -> None:
        if self.action_points is None and self.yellow_coins is None:
            raise ValueError("data должен содержать хотя бы один наблюдаемый ресурс")
        for name, value in (
            ("action_points", self.action_points),
            ("yellow_coins", self.yellow_coins),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(
                    f"data.{name} должен быть неотрицательным целым числом"
                )


@dataclass(frozen=True, slots=True)
class OpsiSchedulerConfigurationInvalidPayload:
    reason_code: str

    def __post_init__(self) -> None:
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class OpsiSchedulerCoinTaskExecutedPayload:
    task: str

    def __post_init__(self) -> None:
        _require_text(self.task, field="data.task")


@dataclass(frozen=True, slots=True)
class OpsiShipExpCheckPayload:
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class OpsiShipExpTargetReachedPayload:
    scope: ShipExpTargetScope

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ShipExpTargetScope):
            raise ValueError("data.scope должен быть значением ShipExpTargetScope")


@dataclass(frozen=True, slots=True)
class OpsiFleetAutoChangePayload:
    reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_safe_code(self.reason_code, field="data.reason_code")


@dataclass(frozen=True, slots=True)
class NotificationTestRequestedPayload:
    label: str = "default"

    def __post_init__(self) -> None:
        _require_text(self.label, field="data.label", maximum=64)


type NotificationPayload = (
    TaskEventPayload
    | TaskFailureLimitPayload
    | RuntimeEventPayload
    | CampaignStopConditionPayload
    | CampaignConfigurationFailurePayload
    | CommissionRewardPayload
    | OpsiActionPointChangedPayload
    | OpsiActionPointLowPayload
    | OpsiResourcesInsufficientPayload
    | OpsiSchedulerConfigurationInvalidPayload
    | OpsiSchedulerCoinTaskExecutedPayload
    | OpsiShipExpCheckPayload
    | OpsiShipExpTargetReachedPayload
    | OpsiFleetAutoChangePayload
    | NotificationTestRequestedPayload
)


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """Неизменяемый транспортно-независимый факт предметного события."""

    id: UUID
    type: str
    schema_version: int
    source: str
    profile_id: str
    runtime_instance_id: str | None
    subject: SubjectRef | None
    severity: NotificationSeverity
    occurred_at: datetime
    data: NotificationPayload
    dedup_key: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    sensitivity: NotificationSensitivity = NotificationSensitivity.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("id должен быть UUID")
        if not isinstance(self.type, str) or not _EVENT_TYPE_RE.fullmatch(self.type):
            raise ValueError("type должен быть canonical dotted event type")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version <= 0
        ):
            raise ValueError("schema_version должен быть положительным целым числом")
        _require_text(self.source, field="source")
        _require_text(self.profile_id, field="profile_id")
        if self.runtime_instance_id is not None:
            _require_text(self.runtime_instance_id, field="runtime_instance_id")
        if self.subject is not None and not isinstance(self.subject, SubjectRef):
            raise ValueError("subject должен быть SubjectRef или None")
        if not isinstance(self.severity, NotificationSeverity):
            raise ValueError("severity должен быть значением NotificationSeverity")
        require_utc(self.occurred_at, field="occurred_at")
        if self.dedup_key is not None:
            _require_text(
                self.dedup_key,
                field="dedup_key",
                maximum=MAX_DEDUP_KEY_LENGTH,
            )
        for name, value in (
            ("correlation_id", self.correlation_id),
            ("causation_id", self.causation_id),
        ):
            if value is not None and not isinstance(value, UUID):
                raise ValueError(f"{name} должен быть UUID или None")
        if not isinstance(self.sensitivity, NotificationSensitivity):
            raise ValueError(
                "sensitivity должен быть значением NotificationSensitivity"
            )


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY_WAIT = "RETRY_WAIT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DeliveryState.DELIVERED,
            DeliveryState.FAILED,
            DeliveryState.SUPPRESSED,
        }


@dataclass(frozen=True, slots=True)
class DeliveryIdentity:
    event_id: UUID
    channel_instance_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise ValueError("event_id должен быть UUID")
        _require_text(self.channel_instance_id, field="channel_instance_id")


@dataclass(frozen=True, slots=True)
class Delivery:
    """Будущая долговечная Delivery без реализации persistence."""

    id: UUID
    identity: DeliveryIdentity
    state: DeliveryState
    suppression_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise ValueError("delivery.id должен быть UUID")
        if not isinstance(self.identity, DeliveryIdentity):
            raise ValueError("delivery.identity должен быть DeliveryIdentity")
        if not isinstance(self.state, DeliveryState):
            raise ValueError("delivery.state должен быть DeliveryState")
        _require_safe_code(
            self.suppression_reason,
            field="delivery.suppression_reason",
        )
        if self.state is DeliveryState.SUPPRESSED and self.suppression_reason is None:
            raise ValueError("SUPPRESSED delivery требует suppression_reason")
        if (
            self.state is not DeliveryState.SUPPRESSED
            and self.suppression_reason is not None
        ):
            raise ValueError(
                "suppression_reason допустим только для SUPPRESSED delivery"
            )
