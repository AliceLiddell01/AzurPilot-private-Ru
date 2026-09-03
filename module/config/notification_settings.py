"""Строгий adapter generated-конфигурации к typed notification settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import yaml

from module.application.notifications.models import NotificationSeverity, SubjectKind
from module.application.notifications.policy import (
    ChannelInstanceSettings,
    NotificationGlobalSettings,
    NotificationPolicy,
    NotificationPolicyResolver,
    NotificationPresentationSettings,
    NotificationSettings,
    PolicyMatcher,
    PolicyRule,
)
from module.application.notifications.registry import (
    DEFAULT_EVENT_REGISTRY,
    EventRegistry,
)

CONFIG_SCHEMA_VERSION = 1
MAX_STRUCTURED_CONFIG_LENGTH = 65_536


class NotificationConfigError(ValueError):
    """Generated notification config нарушает строгую Stage 2 schema."""


class GeneratedNotificationConfigSource(Protocol):
    Notifications_Global: str
    Notifications_Channels: str
    Notifications_Policies: str
    Notifications_Presentation: str


@dataclass(frozen=True, slots=True)
class LegacyNotificationSnapshot:
    """Только безопасная legacy-семантика без provider credentials."""

    scheduler_push_notification: bool
    eventshop_error_notification: bool
    opsi_notify_external: bool
    opsi_launcher_push: bool
    opsi_independent_push: bool
    commission_notify_reward: bool
    commission_include_reward_statistics: bool

    def __post_init__(self) -> None:
        for field, value in (
            ("scheduler_push_notification", self.scheduler_push_notification),
            ("eventshop_error_notification", self.eventshop_error_notification),
            ("opsi_notify_external", self.opsi_notify_external),
            ("opsi_launcher_push", self.opsi_launcher_push),
            ("opsi_independent_push", self.opsi_independent_push),
            ("commission_notify_reward", self.commission_notify_reward),
            (
                "commission_include_reward_statistics",
                self.commission_include_reward_statistics,
            ),
        ):
            if not isinstance(value, bool):
                raise NotificationConfigError(f"{field} должен быть bool")


def _expect_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise NotificationConfigError(f"{field} должен быть YAML mapping")
    return value


def _reject_unknown(
    mapping: dict[str, Any],
    *,
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise NotificationConfigError(
            f"{field} содержит неизвестные ключи: {', '.join(unknown)}"
        )


def _require_keys(
    mapping: dict[str, Any],
    *,
    required: set[str],
    field: str,
) -> None:
    missing = sorted(required - set(mapping))
    if missing:
        raise NotificationConfigError(
            f"{field} не содержит обязательные ключи: {', '.join(missing)}"
        )


def _parse_document(raw: str, *, field: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise NotificationConfigError(f"{field} должен быть строкой YAML")
    if not raw.strip():
        raise NotificationConfigError(f"{field} не должен быть пустым")
    if len(raw) > MAX_STRUCTURED_CONFIG_LENGTH:
        raise NotificationConfigError(
            f"{field} превышает допустимую длину {MAX_STRUCTURED_CONFIG_LENGTH}"
        )
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise NotificationConfigError(f"{field} содержит некорректный YAML") from exc
    mapping = _expect_mapping(parsed, field=field)
    version = mapping.get("schema_version")
    if version != CONFIG_SCHEMA_VERSION:
        raise NotificationConfigError(
            f"{field}.schema_version должен быть {CONFIG_SCHEMA_VERSION}"
        )
    return mapping


def _parse_global(raw: str) -> NotificationGlobalSettings:
    data = _parse_document(raw, field="Notifications.Global")
    _reject_unknown(
        data,
        allowed={"schema_version", "enabled", "history_retention"},
        field="Notifications.Global",
    )
    _require_keys(
        data,
        required={"schema_version", "enabled", "history_retention"},
        field="Notifications.Global",
    )
    try:
        return NotificationGlobalSettings(
            enabled=data["enabled"],
            history_retention_days=data["history_retention"],
        )
    except ValueError as exc:
        raise NotificationConfigError(str(exc)) from exc


def _parse_channels(raw: str) -> tuple[ChannelInstanceSettings, ...]:
    data = _parse_document(raw, field="Notifications.Channels")
    _reject_unknown(
        data,
        allowed={"schema_version", "items"},
        field="Notifications.Channels",
    )
    _require_keys(
        data,
        required={"schema_version", "items"},
        field="Notifications.Channels",
    )
    items = data["items"]
    if not isinstance(items, list):
        raise NotificationConfigError("Notifications.Channels.items должен быть списком")

    channels: list[ChannelInstanceSettings] = []
    for index, item in enumerate(items):
        field = f"Notifications.Channels.items[{index}]"
        mapping = _expect_mapping(item, field=field)
        _reject_unknown(
            mapping,
            allowed={
                "id",
                "type",
                "enabled",
                "config_reference",
                "secret_reference",
            },
            field=field,
        )
        _require_keys(mapping, required={"id", "type", "enabled"}, field=field)
        try:
            channels.append(
                ChannelInstanceSettings(
                    instance_id=mapping["id"],
                    channel_type=mapping["type"],
                    enabled=mapping["enabled"],
                    config_reference=mapping.get("config_reference"),
                    secret_reference=mapping.get("secret_reference"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise NotificationConfigError(f"{field}: {exc}") from exc
    return tuple(channels)


def _parse_severity(value: Any, *, field: str) -> NotificationSeverity | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NotificationConfigError(f"{field} должен быть строкой")
    try:
        return NotificationSeverity[value]
    except KeyError as exc:
        raise NotificationConfigError(f"{field} содержит неизвестную severity") from exc


def _parse_subject_kind(value: Any, *, field: str) -> SubjectKind | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NotificationConfigError(f"{field} должен быть строкой")
    try:
        return SubjectKind(value)
    except ValueError as exc:
        raise NotificationConfigError(f"{field} содержит неизвестный subject kind") from exc


def _parse_matcher(value: Any, *, field: str) -> PolicyMatcher:
    if value is None:
        value = {}
    data = _expect_mapping(value, field=field)
    _reject_unknown(
        data,
        allowed={
            "event_type",
            "severity_exact",
            "minimum_severity",
            "profile_id",
            "subject_kind",
            "subject_id",
        },
        field=field,
    )
    try:
        return PolicyMatcher(
            event_type=data.get("event_type"),
            severity_exact=_parse_severity(
                data.get("severity_exact"),
                field=f"{field}.severity_exact",
            ),
            minimum_severity=_parse_severity(
                data.get("minimum_severity"),
                field=f"{field}.minimum_severity",
            ),
            profile_id=data.get("profile_id"),
            subject_kind=_parse_subject_kind(
                data.get("subject_kind"),
                field=f"{field}.subject_kind",
            ),
            subject_id=data.get("subject_id"),
        )
    except ValueError as exc:
        raise NotificationConfigError(f"{field}: {exc}") from exc


def _parse_policies(raw: str) -> NotificationPolicy:
    data = _parse_document(raw, field="Notifications.Policies")
    _reject_unknown(
        data,
        allowed={"schema_version", "rules"},
        field="Notifications.Policies",
    )
    _require_keys(
        data,
        required={"schema_version", "rules"},
        field="Notifications.Policies",
    )
    raw_rules = data["rules"]
    if not isinstance(raw_rules, list):
        raise NotificationConfigError("Notifications.Policies.rules должен быть списком")

    rules: list[PolicyRule] = []
    allowed = {
        "id",
        "version",
        "priority",
        "default",
        "match",
        "channels",
        "suppress",
        "suppression_reason",
        "cooldown_seconds",
        "presentation_hint",
    }
    for index, item in enumerate(raw_rules):
        field = f"Notifications.Policies.rules[{index}]"
        mapping = _expect_mapping(item, field=field)
        _reject_unknown(mapping, allowed=allowed, field=field)
        _require_keys(
            mapping,
            required={"id", "version", "priority"},
            field=field,
        )
        channels = mapping.get("channels", [])
        if not isinstance(channels, list) or any(
            not isinstance(channel, str) for channel in channels
        ):
            raise NotificationConfigError(f"{field}.channels должен быть списком строк")
        suppress = mapping.get("suppress", False)
        is_default = mapping.get("default", False)
        if not isinstance(suppress, bool) or not isinstance(is_default, bool):
            raise NotificationConfigError(
                f"{field}.suppress/default должны быть bool"
            )
        try:
            rules.append(
                PolicyRule(
                    rule_id=mapping["id"],
                    version=mapping["version"],
                    priority=mapping["priority"],
                    matcher=_parse_matcher(
                        mapping.get("match"),
                        field=f"{field}.match",
                    ),
                    channels=tuple(channels),
                    suppress=suppress,
                    suppression_reason=mapping.get("suppression_reason"),
                    cooldown_seconds=mapping.get("cooldown_seconds"),
                    presentation_hint=mapping.get("presentation_hint"),
                    is_default=is_default,
                )
            )
        except (TypeError, ValueError) as exc:
            raise NotificationConfigError(f"{field}: {exc}") from exc

    try:
        return NotificationPolicy(tuple(rules))
    except ValueError as exc:
        raise NotificationConfigError(str(exc)) from exc


def _parse_presentation(raw: str) -> NotificationPresentationSettings:
    data = _parse_document(raw, field="Notifications.Presentation")
    _reject_unknown(
        data,
        allowed={"schema_version", "commission"},
        field="Notifications.Presentation",
    )
    _require_keys(
        data,
        required={"schema_version", "commission"},
        field="Notifications.Presentation",
    )
    commission = _expect_mapping(
        data["commission"],
        field="Notifications.Presentation.commission",
    )
    _reject_unknown(
        commission,
        allowed={"include_reward_statistics"},
        field="Notifications.Presentation.commission",
    )
    _require_keys(
        commission,
        required={"include_reward_statistics"},
        field="Notifications.Presentation.commission",
    )
    try:
        return NotificationPresentationSettings(
            commission_include_reward_statistics=commission[
                "include_reward_statistics"
            ]
        )
    except ValueError as exc:
        raise NotificationConfigError(str(exc)) from exc


def parse_notification_settings(
    source: GeneratedNotificationConfigSource,
    *,
    registry: EventRegistry = DEFAULT_EVENT_REGISTRY,
) -> NotificationSettings:
    """Преобразовать generated строки в validated typed settings."""

    try:
        settings = NotificationSettings(
            global_settings=_parse_global(source.Notifications_Global),
            channels=_parse_channels(source.Notifications_Channels),
            policy=_parse_policies(source.Notifications_Policies),
            presentation=_parse_presentation(source.Notifications_Presentation),
        )
        NotificationPolicyResolver(registry, settings)
    except NotificationConfigError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise NotificationConfigError(str(exc)) from exc
    return settings


def map_legacy_notification_settings(
    legacy: LegacyNotificationSnapshot,
    *,
    registry: EventRegistry = DEFAULT_EVENT_REGISTRY,
) -> NotificationSettings:
    """Спроецировать legacy toggles в target model без runtime cutover."""

    channels: list[ChannelInstanceSettings] = []
    rules: list[PolicyRule] = []

    def add_channel(channel: ChannelInstanceSettings) -> None:
        if all(existing.instance_id != channel.instance_id for existing in channels):
            channels.append(channel)

    def external_channel_id(*, opsi: bool = False) -> str:
        if opsi and legacy.opsi_independent_push:
            channel_id = "legacy.external.opsi"
            add_channel(
                ChannelInstanceSettings(
                    instance_id=channel_id,
                    channel_type="legacy_onepush",
                    secret_reference="legacy.OpsiGeneral.OpsiOnePushConfig",
                )
            )
            return channel_id
        channel_id = "legacy.external.default"
        add_channel(
            ChannelInstanceSettings(
                instance_id=channel_id,
                channel_type="legacy_onepush",
                secret_reference="legacy.Error.OnePushConfig",
            )
        )
        return channel_id

    if legacy.eventshop_error_notification:
        rules.append(
            PolicyRule(
                rule_id="legacy.eventshop.error",
                version=1,
                priority=10,
                matcher=PolicyMatcher(
                    event_type="task.failed",
                    subject_kind=SubjectKind.TASK,
                    subject_id="EventShop",
                ),
                channels=(external_channel_id(),),
            )
        )
    else:
        rules.append(
            PolicyRule(
                rule_id="legacy.eventshop.error.disabled",
                version=1,
                priority=10,
                matcher=PolicyMatcher(
                    event_type="task.failed",
                    subject_kind=SubjectKind.TASK,
                    subject_id="EventShop",
                ),
                suppress=True,
                suppression_reason="legacy_eventshop_disabled",
            )
        )

    if legacy.scheduler_push_notification:
        scheduler_channel = external_channel_id()
        for priority, event_type in (
            (20, "task.completed"),
            (21, "task.recovered"),
            (22, "task.failed"),
        ):
            rules.append(
                PolicyRule(
                    rule_id=f"legacy.scheduler.{event_type}",
                    version=1,
                    priority=priority,
                    matcher=PolicyMatcher(event_type=event_type),
                    channels=(scheduler_channel,),
                )
            )

    opsi_channels: list[str] = []
    if legacy.opsi_notify_external:
        opsi_channels.append(external_channel_id(opsi=True))
    if legacy.opsi_launcher_push:
        add_channel(
            ChannelInstanceSettings(
                instance_id="legacy.desktop",
                channel_type="desktop",
            )
        )
        opsi_channels.append("legacy.desktop")
    rules.append(
        PolicyRule(
            rule_id="legacy.opsi",
            version=1,
            priority=30,
            matcher=PolicyMatcher(event_type="opsi.*"),
            channels=tuple(opsi_channels),
            suppress=not opsi_channels,
            suppression_reason=None if opsi_channels else "legacy_opsi_disabled",
        )
    )

    if legacy.commission_notify_reward:
        commission_channels = (
            external_channel_id(),
            "legacy.desktop",
        )
        add_channel(
            ChannelInstanceSettings(
                instance_id="legacy.desktop",
                channel_type="desktop",
            )
        )
        rules.append(
            PolicyRule(
                rule_id="legacy.commission.reward",
                version=1,
                priority=40,
                matcher=PolicyMatcher(event_type="commission.reward.received"),
                channels=commission_channels,
            )
        )

    rules.append(
        PolicyRule(
            rule_id="legacy.default",
            version=1,
            priority=10_000,
            matcher=PolicyMatcher(),
            suppress=True,
            suppression_reason="legacy_unmapped",
            is_default=True,
        )
    )
    settings = NotificationSettings(
        global_settings=NotificationGlobalSettings(),
        channels=tuple(channels),
        policy=NotificationPolicy(tuple(rules)),
        presentation=NotificationPresentationSettings(
            commission_include_reward_statistics=(
                legacy.commission_include_reward_statistics
            )
        ),
    )
    try:
        NotificationPolicyResolver(registry, settings)
    except ValueError as exc:
        raise NotificationConfigError(str(exc)) from exc
    return settings
