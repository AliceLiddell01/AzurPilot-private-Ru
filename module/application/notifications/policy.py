"""Детерминированная policy-модель маршрутизации уведомлений."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from module.application.notifications.models import (
    Delivery,
    DeliveryIdentity,
    DeliveryState,
    NotificationEvent,
    NotificationSeverity,
    SubjectKind,
    require_utc,
)
from module.application.notifications.registry import EventRegistry

MAX_CHANNELS = 64
MAX_POLICY_RULES = 256
MAX_RETENTION_DAYS = 3650
MAX_COOLDOWN_SECONDS = 30 * 24 * 60 * 60
_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_EVENT_MATCHER_RE = re.compile(r"^[a-z0-9]+(?:[._][a-z0-9]+)*(?:\.\*)?$")
_SAFE_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _require_identifier(value: str, *, field: str, maximum: int = 128) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    if len(value) > maximum:
        raise ValueError(f"{field} превышает допустимую длину {maximum}")


def _require_reason(value: str | None, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _SAFE_REASON_RE.fullmatch(value):
        raise ValueError(f"{field} должен быть ограниченным безопасным кодом")


@dataclass(frozen=True, slots=True)
class PolicyMatcher:
    """Ограниченный matcher без regex и произвольного кода."""

    event_type: str | None = None
    severity_exact: NotificationSeverity | None = None
    minimum_severity: NotificationSeverity | None = None
    profile_id: str | None = None
    subject_kind: SubjectKind | None = None
    subject_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_type is not None:
            _require_identifier(self.event_type, field="matcher.event_type")
            if not _EVENT_MATCHER_RE.fullmatch(self.event_type):
                raise ValueError(
                    "event_type допускает только точный тип или dotted-prefix вида opsi.*"
                )
        if self.severity_exact is not None and self.minimum_severity is not None:
            raise ValueError("Нельзя одновременно задавать exact и minimum severity")
        for name, severity in (
            ("severity_exact", self.severity_exact),
            ("minimum_severity", self.minimum_severity),
        ):
            if severity is not None and not isinstance(severity, NotificationSeverity):
                raise ValueError(f"matcher.{name} содержит неизвестную severity")
        if self.profile_id is not None:
            _require_identifier(self.profile_id, field="matcher.profile_id")
        if self.subject_kind is not None and not isinstance(
            self.subject_kind,
            SubjectKind,
        ):
            raise ValueError("matcher.subject_kind содержит неизвестный kind")
        if self.subject_id is not None:
            _require_identifier(self.subject_id, field="matcher.subject_id")
            if self.subject_kind is None:
                raise ValueError("subject_id требует subject_kind")

    @property
    def is_unconditional(self) -> bool:
        return all(
            value is None
            for value in (
                self.event_type,
                self.severity_exact,
                self.minimum_severity,
                self.profile_id,
                self.subject_kind,
                self.subject_id,
            )
        )

    def matches(self, event: NotificationEvent) -> bool:
        if self.event_type is not None:
            if self.event_type.endswith(".*"):
                prefix = self.event_type[:-2]
                if not event.type.startswith(f"{prefix}."):
                    return False
            elif event.type != self.event_type:
                return False
        if self.severity_exact is not None and event.severity is not self.severity_exact:
            return False
        if self.minimum_severity is not None and event.severity < self.minimum_severity:
            return False
        if self.profile_id is not None and event.profile_id != self.profile_id:
            return False
        if self.subject_kind is not None:
            if event.subject is None or event.subject.kind is not self.subject_kind:
                return False
            if self.subject_id is not None and event.subject.id != self.subject_id:
                return False
        return True


@dataclass(frozen=True, slots=True)
class PolicyRule:
    rule_id: str
    version: int
    priority: int
    matcher: PolicyMatcher
    channels: tuple[str, ...] = ()
    suppress: bool = False
    suppression_reason: str | None = None
    cooldown_seconds: int | None = None
    presentation_hint: str | None = None
    is_default: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.rule_id, field="rule_id")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version <= 0
        ):
            raise ValueError("rule.version должен быть положительным целым числом")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError("rule.priority должен быть целым числом")
        if not isinstance(self.matcher, PolicyMatcher):
            raise ValueError("rule.matcher должен быть PolicyMatcher")
        if self.is_default and not self.matcher.is_unconditional:
            raise ValueError("Правило по умолчанию должно иметь безусловный matcher")
        if len(self.channels) > MAX_CHANNELS:
            raise ValueError("rule.channels содержит слишком много каналов")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("rule.channels содержит дубликаты")
        for channel_id in self.channels:
            _require_identifier(channel_id, field="channel_instance_id")
        _require_reason(self.suppression_reason, field="suppression_reason")
        if self.suppress:
            if self.channels:
                raise ValueError("Suppress rule не должна выбирать каналы")
            if self.suppression_reason is None:
                raise ValueError("Suppress rule требует suppression_reason")
        else:
            if not self.channels:
                raise ValueError("Routed rule должна выбирать хотя бы один канал")
            if self.suppression_reason is not None:
                raise ValueError("Routed rule не должна задавать suppression_reason")
        if self.cooldown_seconds is not None:
            if (
                not isinstance(self.cooldown_seconds, int)
                or isinstance(self.cooldown_seconds, bool)
                or self.cooldown_seconds <= 0
                or self.cooldown_seconds > MAX_COOLDOWN_SECONDS
            ):
                raise ValueError("cooldown_seconds находится вне допустимого диапазона")
        if self.presentation_hint is not None:
            _require_identifier(
                self.presentation_hint,
                field="presentation_hint",
                maximum=64,
            )


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    rules: tuple[PolicyRule, ...]

    def __post_init__(self) -> None:
        if not self.rules or len(self.rules) > MAX_POLICY_RULES:
            raise ValueError("Policy должна содержать от 1 до 256 правил")
        priorities = [rule.priority for rule in self.rules]
        if len(set(priorities)) != len(priorities):
            raise ValueError("Одинаковый priority запрещён: порядок должен быть однозначным")
        identities = [rule.rule_id for rule in self.rules]
        if len(set(identities)) != len(identities):
            raise ValueError("rule_id должен быть уникальным в активной policy")
        default_rules = [rule for rule in self.rules if rule.is_default]
        if len(default_rules) != 1:
            raise ValueError("Policy должна содержать ровно одно правило по умолчанию")
        ordered = self.ordered_rules
        if ordered[-1] is not default_rules[0]:
            raise ValueError(
                "Правило по умолчанию должно выполняться последним по priority"
            )

    @property
    def ordered_rules(self) -> tuple[PolicyRule, ...]:
        return tuple(sorted(self.rules, key=lambda rule: rule.priority))

    @property
    def fingerprint(self) -> str:
        parts: list[str] = []
        for rule in self.ordered_rules:
            matcher = rule.matcher
            parts.append(
                "|".join(
                    (
                        rule.rule_id,
                        str(rule.version),
                        str(rule.priority),
                        matcher.event_type or "",
                        matcher.severity_exact.name if matcher.severity_exact else "",
                        matcher.minimum_severity.name if matcher.minimum_severity else "",
                        matcher.profile_id or "",
                        matcher.subject_kind.value if matcher.subject_kind else "",
                        matcher.subject_id or "",
                        ",".join(rule.channels),
                        "1" if rule.suppress else "0",
                        rule.suppression_reason or "",
                        str(rule.cooldown_seconds or ""),
                        rule.presentation_hint or "",
                        "1" if rule.is_default else "0",
                    )
                )
            )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NotificationGlobalSettings:
    enabled: bool = True
    history_retention_days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("Notifications.Global.enabled должен быть bool")
        if (
            not isinstance(self.history_retention_days, int)
            or isinstance(self.history_retention_days, bool)
            or not 1 <= self.history_retention_days <= MAX_RETENTION_DAYS
        ):
            raise ValueError("history_retention_days находится вне допустимого диапазона")


@dataclass(frozen=True, slots=True)
class ChannelInstanceSettings:
    instance_id: str
    channel_type: str
    enabled: bool = True
    config_reference: str | None = None
    secret_reference: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.instance_id, field="channel.instance_id")
        _require_identifier(self.channel_type, field="channel.type")
        if not isinstance(self.enabled, bool):
            raise ValueError("channel.enabled должен быть bool")
        for field, value in (
            ("channel.config_reference", self.config_reference),
            ("channel.secret_reference", self.secret_reference),
        ):
            if value is not None and (
                not isinstance(value, str) or not _SAFE_REFERENCE_RE.fullmatch(value)
            ):
                raise ValueError(
                    f"{field} должен быть безопасной ссылкой без URL и credentials"
                )


@dataclass(frozen=True, slots=True)
class NotificationPresentationSettings:
    commission_include_reward_statistics: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.commission_include_reward_statistics, bool):
            raise ValueError("Presentation commission flag должен быть bool")


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    global_settings: NotificationGlobalSettings
    channels: tuple[ChannelInstanceSettings, ...]
    policy: NotificationPolicy
    presentation: NotificationPresentationSettings

    def __post_init__(self) -> None:
        if not isinstance(self.global_settings, NotificationGlobalSettings):
            raise ValueError("global_settings должен быть NotificationGlobalSettings")
        if not isinstance(self.policy, NotificationPolicy):
            raise ValueError("policy должен быть NotificationPolicy")
        if not isinstance(self.presentation, NotificationPresentationSettings):
            raise ValueError(
                "presentation должен быть NotificationPresentationSettings"
            )
        if len(self.channels) > MAX_CHANNELS:
            raise ValueError("Слишком много channel instances")
        if any(not isinstance(channel, ChannelInstanceSettings) for channel in self.channels):
            raise ValueError("channels должен содержать ChannelInstanceSettings")
        ids = [channel.instance_id for channel in self.channels]
        if len(set(ids)) != len(ids):
            raise ValueError("channel_instance_id должен быть уникальным")
        known = set(ids)
        for rule in self.policy.rules:
            unknown = [
                channel_id
                for channel_id in rule.channels
                if channel_id not in known
            ]
            if unknown:
                raise ValueError(
                    f"Policy rule {rule.rule_id} ссылается на неизвестные каналы: "
                    + ", ".join(unknown)
                )

    def channel(self, channel_instance_id: str) -> ChannelInstanceSettings:
        for channel in self.channels:
            if channel.instance_id == channel_instance_id:
                return channel
        raise KeyError(channel_instance_id)


class PolicyDecisionState(StrEnum):
    ROUTED = "ROUTED"
    SUPPRESSED = "SUPPRESSED"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    event_id: UUID
    state: PolicyDecisionState
    matched_rule_id: str | None
    matched_rule_version: int | None
    policy_fingerprint: str
    selected_channel_ids: tuple[str, ...]
    suppression_reason: str | None
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise ValueError("event_id должен быть UUID")
        if not isinstance(self.state, PolicyDecisionState):
            raise ValueError("Неизвестное состояние PolicyDecision")
        if (self.matched_rule_id is None) != (self.matched_rule_version is None):
            raise ValueError("matched rule id/version должны задаваться вместе")
        if self.matched_rule_id is not None:
            _require_identifier(self.matched_rule_id, field="matched_rule_id")
            if (
                not isinstance(self.matched_rule_version, int)
                or isinstance(self.matched_rule_version, bool)
                or self.matched_rule_version <= 0
            ):
                raise ValueError(
                    "matched_rule_version должен быть положительным целым числом"
                )
        if (
            not isinstance(self.policy_fingerprint, str)
            or len(self.policy_fingerprint) != 64
            or any(
                char not in "0123456789abcdef"
                for char in self.policy_fingerprint
            )
        ):
            raise ValueError("policy_fingerprint должен быть SHA-256 hex")
        if len(set(self.selected_channel_ids)) != len(self.selected_channel_ids):
            raise ValueError("selected_channel_ids содержит дубликаты")
        for channel_id in self.selected_channel_ids:
            _require_identifier(channel_id, field="selected_channel_id")
        _require_reason(self.suppression_reason, field="suppression_reason")
        require_utc(self.evaluated_at, field="evaluated_at")
        if self.state is PolicyDecisionState.ROUTED:
            if not self.selected_channel_ids:
                raise ValueError("ROUTED decision должна содержать каналы")
            if self.suppression_reason is not None:
                raise ValueError(
                    "ROUTED decision не должна содержать suppression_reason"
                )
            if self.matched_rule_id is None:
                raise ValueError("ROUTED decision требует matched rule")
        else:
            if self.selected_channel_ids:
                raise ValueError("SUPPRESSED decision не должна содержать каналы")
            if self.suppression_reason is None:
                raise ValueError("SUPPRESSED decision требует suppression_reason")


class NotificationPolicyResolver:
    """Resolver без side effects: validation → global gate → первое совпадение."""

    def __init__(
        self,
        registry: EventRegistry,
        settings: NotificationSettings,
    ):
        if not isinstance(registry, EventRegistry):
            raise ValueError("registry должен быть EventRegistry")
        if not isinstance(settings, NotificationSettings):
            raise ValueError("settings должен быть NotificationSettings")
        self._registry = registry
        self._settings = settings
        self._validate_matchers()

    def _validate_matchers(self) -> None:
        event_types = self._registry.event_types
        for rule in self._settings.policy.rules:
            selector = rule.matcher.event_type
            if selector is None:
                continue
            if selector.endswith(".*"):
                prefix = selector[:-2]
                if not any(
                    event_type.startswith(f"{prefix}.")
                    for event_type in event_types
                ):
                    raise ValueError(
                        f"Policy rule {rule.rule_id} содержит неизвестный prefix: "
                        f"{selector}"
                    )
            elif selector not in event_types:
                raise ValueError(
                    f"Policy rule {rule.rule_id} содержит неизвестный event type: "
                    f"{selector}"
                )

    def resolve(
        self,
        event: NotificationEvent,
        *,
        evaluated_at: datetime | None = None,
    ) -> PolicyDecision:
        self._registry.validate(event)
        now = evaluated_at or datetime.now(UTC)
        require_utc(now, field="evaluated_at")
        fingerprint = self._settings.policy.fingerprint

        if not self._settings.global_settings.enabled:
            return PolicyDecision(
                event_id=event.id,
                state=PolicyDecisionState.SUPPRESSED,
                matched_rule_id=None,
                matched_rule_version=None,
                policy_fingerprint=fingerprint,
                selected_channel_ids=(),
                suppression_reason="global_disabled",
                evaluated_at=now,
            )

        for rule in self._settings.policy.ordered_rules:
            if not rule.matcher.matches(event):
                continue
            if rule.suppress:
                return PolicyDecision(
                    event_id=event.id,
                    state=PolicyDecisionState.SUPPRESSED,
                    matched_rule_id=rule.rule_id,
                    matched_rule_version=rule.version,
                    policy_fingerprint=fingerprint,
                    selected_channel_ids=(),
                    suppression_reason=rule.suppression_reason,
                    evaluated_at=now,
                )
            return PolicyDecision(
                event_id=event.id,
                state=PolicyDecisionState.ROUTED,
                matched_rule_id=rule.rule_id,
                matched_rule_version=rule.version,
                policy_fingerprint=fingerprint,
                selected_channel_ids=rule.channels,
                suppression_reason=None,
                evaluated_at=now,
            )

        raise RuntimeError("Правило policy по умолчанию не было применено")


def create_initial_delivery(
    event_id: UUID,
    channel: ChannelInstanceSettings,
    *,
    delivery_id: UUID | None = None,
) -> Delivery:
    """Создать состояние Delivery этапа 2 без внешней отправки."""

    identity = DeliveryIdentity(
        event_id=event_id,
        channel_instance_id=channel.instance_id,
    )
    if channel.enabled:
        return Delivery(
            id=delivery_id or uuid4(),
            identity=identity,
            state=DeliveryState.PENDING,
        )
    return Delivery(
        id=delivery_id or uuid4(),
        identity=identity,
        state=DeliveryState.SUPPRESSED,
        suppression_reason="channel_disabled",
    )
