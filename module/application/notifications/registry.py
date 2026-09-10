"""Typed registry notification-схем и безопасных descriptor-ов."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from re import fullmatch
from typing import Final
from uuid import UUID

from module.application.errors import NotificationValidationError
from module.application.notifications.encoding import (
    MAX_PAYLOAD_BYTES,
    canonical_json,
    event_payload_digest,
)
from module.application.notifications.models import (
    HandoverPreemptionPayload,
    HandoverPublishContext,
    NotificationEvent,
    NotificationSensitivity,
    NotificationSeverity,
    NotificationSubject,
)

NotificationSerializer = Callable[[object], Mapping[str, object]]
NotificationValidator = Callable[[NotificationEvent, object, Mapping[str, object]], None]
NotificationDeserializer = Callable[[Mapping[str, object]], object]

_SOURCE_RE: Final = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
_DEDUP_KEY_RE: Final = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
_PROFILE_RE: Final = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_PROHIBITED_KEYS = frozenset(
    {
        "auth",
        "config",
        "cookie",
        "credential",
        "credentials",
        "device",
        "device_id",
        "exception",
        "headers",
        "metadata",
        "password",
        "raw",
        "raw_payload",
        "raw_response",
        "screenshot",
        "secret",
        "stacktrace",
        "token",
        "traceback",
        "url",
    }
)


@dataclass(frozen=True, slots=True)
class NotificationDescriptor:
    event_type: str
    schema_version: int
    payload_type: type
    serializer: NotificationSerializer
    validator: NotificationValidator
    default_severity: NotificationSeverity
    renderer_id: str
    deserializer: NotificationDeserializer | None = None
    policy_capabilities: tuple[str, ...] = ()
    dedup_required: bool = False
    allow_severity_override: bool = False
    publishable: bool = True
    deferred_reason: str | None = None
    default_priority: int = 0
    required_context_type: type | None = None

    def validate(self, event: NotificationEvent) -> tuple[dict[str, object], str]:
        if not self.publishable:
            raise NotificationValidationError(self.deferred_reason or "descriptor_not_publishable")
        if not isinstance(event.data, self.payload_type):
            raise NotificationValidationError("payload_type_invalid")
        if not self.allow_severity_override and event.severity is not self.default_severity:
            raise NotificationValidationError("severity_not_allowed")
        try:
            document = self.serializer(event.data)
        except NotificationValidationError:
            raise
        except Exception:  # noqa: BLE001 - descriptor boundary скрывает raw payload exception.
            raise NotificationValidationError("payload_encoding_failed") from None
        if not isinstance(document, Mapping) or any(
            not isinstance(key, str) for key in document
        ):
            raise NotificationValidationError("payload_schema_invalid")
        normalized = canonical_json(document, max_bytes=MAX_PAYLOAD_BYTES)
        try:
            self.validator(event, event.data, document)
        except NotificationValidationError:
            raise
        except Exception:  # noqa: BLE001 - stored payload boundary имеет bounded error code.
            raise NotificationValidationError("payload_validation_failed") from None
        normalized_document = json.loads(normalized)
        if not isinstance(normalized_document, dict):
            raise NotificationValidationError("payload_schema_invalid")
        _reject_prohibited_keys(normalized_document)
        return normalized_document, event_payload_digest(event, normalized_document)

    def deserialize(self, document: Mapping[str, object]) -> object:
        if self.deserializer is None:
            raise NotificationValidationError("stored_payload_decoder_unavailable")
        try:
            payload = self.deserializer(document)
        except Exception:  # noqa: BLE001 - corrupted stored payload не выходит наружу.
            raise NotificationValidationError("stored_payload_invalid") from None
        if not isinstance(payload, self.payload_type):
            raise NotificationValidationError("stored_payload_type_invalid")
        return payload


class DeferredNotificationPayload:
    """Явный placeholder без generic dict escape hatch для будущих producers."""


def _empty_serializer(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, DeferredNotificationPayload):
        raise NotificationValidationError("payload_type_invalid")
    return {}


def _empty_validator(
    event: NotificationEvent, payload: object, document: Mapping[str, object]
) -> None:
    if not isinstance(payload, DeferredNotificationPayload) or document != {}:
        raise NotificationValidationError("payload_schema_invalid")


def _handover_serializer(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, HandoverPreemptionPayload):
        raise NotificationValidationError("payload_type_invalid")
    document: dict[str, object] = {
        "operation_id": payload.operation_id,
        "source_profile_id": payload.source_profile_id,
        "owner_epoch": payload.owner_epoch,
        "reason_code": payload.reason_code,
        "deadline_at": payload.deadline_at,
    }
    if payload.session_id is not None:
        document["session_id"] = payload.session_id
    if payload.current_task is not None:
        document["current_task"] = {
            "kind": payload.current_task.kind,
            "id": payload.current_task.id,
        }
    return document


def _handover_validator(
    event: NotificationEvent, payload: object, document: Mapping[str, object]
) -> None:
    if not isinstance(payload, HandoverPreemptionPayload):
        raise NotificationValidationError("payload_type_invalid")
    if not payload.is_valid(event_profile_id=event.profile_id, occurred_at=event.occurred_at):
        raise NotificationValidationError("handover_payload_invalid")
    if event.dedup_key != payload.operation_id:
        raise NotificationValidationError("handover_dedup_key_invalid")
    if "occurred_at" in document:
        raise NotificationValidationError("payload_occurrence_time_forbidden")


def _handover_deserializer(document: Mapping[str, object]) -> object:
    current_task = document.get("current_task")
    subject = None
    if current_task is not None:
        if not isinstance(current_task, Mapping):
            raise ValueError
        subject = NotificationSubject(kind=current_task["kind"], id=current_task["id"])
    deadline_at = document["deadline_at"]
    if isinstance(deadline_at, str):
        deadline_at = datetime.fromisoformat(deadline_at)
    return HandoverPreemptionPayload(
        operation_id=document["operation_id"],
        source_profile_id=document["source_profile_id"],
        owner_epoch=document["owner_epoch"],
        reason_code=document["reason_code"],
        deadline_at=deadline_at,
        session_id=document.get("session_id"),
        current_task=subject,
    )


_TAXONOMY: Final = (
    "task.completed",
    "task.recovered",
    "task.failed",
    "task.failure_limit.reached",
    "runtime.game.unavailable",
    "runtime.game.stuck",
    "runtime.game.error",
    "runtime.game.page_unknown",
    "runtime.emulator.unavailable",
    "runtime.emulator.recovered",
    "runtime.recovery.succeeded",
    "runtime.recovery.failed",
    "campaign.stop_condition.reached",
    "campaign.auto_search.configuration_failed",
    "commission.reward.received",
    "opsi.action_point.changed",
    "opsi.action_point.low",
    "opsi.resources.insufficient",
    "opsi.scheduler.configuration.invalid",
    "opsi.scheduler.coin_task.executed",
    "opsi.ship_exp.check_failed",
    "opsi.ship_exp.check_completed",
    "opsi.ship_exp.target_reached",
    "opsi.fleet.auto_change.completed",
    "opsi.fleet.auto_change.failed",
    "notification.test.requested",
)


class NotificationRegistry:
    """Расширяемая map event type/version на typed descriptor."""

    def __init__(self, descriptors: tuple[NotificationDescriptor, ...] = ()) -> None:
        self._descriptors: dict[tuple[str, int], NotificationDescriptor] = {}
        self._frozen = False
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: NotificationDescriptor) -> None:
        if self._frozen:
            raise RuntimeError("Notification registry по умолчанию неизменяем.")
        if not isinstance(descriptor, NotificationDescriptor):
            raise TypeError("Notification descriptor имеет неверный тип.")
        if not isinstance(descriptor.event_type, str) or len(descriptor.event_type) > 128 or fullmatch(
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*", descriptor.event_type
        ) is None:
            raise ValueError("event_type descriptor должен быть lowercase dotted token.")
        if not isinstance(descriptor.schema_version, int) or isinstance(
            descriptor.schema_version, bool
        ) or descriptor.schema_version <= 0:
            raise ValueError("schema_version descriptor должен быть положительным.")
        if not isinstance(descriptor.default_severity, NotificationSeverity):
            raise TypeError("Descriptor default severity имеет неверный тип.")
        if not isinstance(descriptor.payload_type, type):
            raise TypeError("Descriptor payload_type должен быть type.")
        if not callable(descriptor.serializer) or not callable(descriptor.validator):
            raise TypeError("Descriptor serializer и validator должны быть callable.")
        if not isinstance(descriptor.renderer_id, str) or fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", descriptor.renderer_id
        ) is None:
            raise ValueError("Descriptor renderer id имеет неверный формат.")
        if not isinstance(descriptor.policy_capabilities, tuple):
            raise TypeError("Descriptor policy capabilities должны быть tuple.")
        if any(
            not isinstance(capability, str)
            or fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", capability) is None
            for capability in descriptor.policy_capabilities
        ):
            raise ValueError("Descriptor policy capability имеет неверный формат.")
        if not isinstance(descriptor.dedup_required, bool) or not isinstance(
            descriptor.allow_severity_override, bool
        ) or not isinstance(descriptor.publishable, bool):
            raise TypeError("Descriptor flags должны быть bool.")
        if descriptor.deserializer is not None and not callable(descriptor.deserializer):
            raise TypeError("Descriptor deserializer должен быть callable.")
        if descriptor.publishable and descriptor.deserializer is None:
            raise ValueError(
                "Publishable descriptor должен иметь typed deserializer."
            )
        if not isinstance(descriptor.default_priority, int) or isinstance(
            descriptor.default_priority, bool
        ) or not -100 <= descriptor.default_priority <= 100:
            raise ValueError("Descriptor default priority имеет неверный диапазон.")
        if descriptor.required_context_type is not None and not isinstance(
            descriptor.required_context_type, type
        ):
            raise TypeError("Descriptor required context должен быть type.")
        key = (descriptor.event_type, descriptor.schema_version)
        if key in self._descriptors:
            raise ValueError("Notification descriptor уже зарегистрирован.")
        self._descriptors[key] = descriptor

    def get(self, event_type: str, schema_version: int) -> NotificationDescriptor | None:
        return self._descriptors.get((event_type, schema_version))

    def require(self, event_type: str, schema_version: int) -> NotificationDescriptor:
        descriptor = self.get(event_type, schema_version)
        if descriptor is None:
            raise NotificationValidationError("event_type_or_version_unknown")
        return descriptor

    def validate(self, event: NotificationEvent) -> tuple[NotificationDescriptor, dict[str, object], str]:
        if not isinstance(event, NotificationEvent):
            raise NotificationValidationError("event_invalid")
        if not isinstance(event.id, UUID):
            raise NotificationValidationError("event_id_invalid")
        if not isinstance(event.source, str) or fullmatch(_SOURCE_RE, event.source) is None:
            raise NotificationValidationError("source_invalid")
        if not isinstance(event.type, str) or len(event.type) > 128 or fullmatch(
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*", event.type
        ) is None:
            raise NotificationValidationError("event_type_invalid")
        if not isinstance(event.schema_version, int) or isinstance(event.schema_version, bool) or event.schema_version <= 0:
            raise NotificationValidationError("schema_version_invalid")
        if not isinstance(event.profile_id, str) or fullmatch(_PROFILE_RE, event.profile_id) is None:
            raise NotificationValidationError("profile_id_invalid")
        if event.runtime_instance_id is not None and (
            not isinstance(event.runtime_instance_id, str)
            or fullmatch(_PROFILE_RE, event.runtime_instance_id) is None
        ):
            raise NotificationValidationError("runtime_instance_id_invalid")
        if event.dedup_key is not None and (
            not isinstance(event.dedup_key, str)
            or fullmatch(_DEDUP_KEY_RE, event.dedup_key) is None
        ):
            raise NotificationValidationError("dedup_key_invalid")
        if event.subject is not None and not event.subject.is_valid():
            raise NotificationValidationError("subject_invalid")
        if event.correlation is not None and not event.correlation.is_valid():
            raise NotificationValidationError("correlation_invalid")
        if not isinstance(event.severity, NotificationSeverity):
            raise NotificationValidationError("severity_invalid")
        if not isinstance(event.sensitivity, NotificationSensitivity):
            raise NotificationValidationError("sensitivity_invalid")
        if not isinstance(event.occurred_at, datetime):
            raise NotificationValidationError("occurred_at_invalid")
        if event.occurred_at.tzinfo is None or event.occurred_at.utcoffset() is None:
            raise NotificationValidationError("occurred_at_not_aware")
        if event.persisted_at is not None or event.profile_sequence is not None or event.payload_digest is not None:
            raise NotificationValidationError("server_owned_field_provided")
        descriptor = self.require(event.type, event.schema_version)
        if descriptor.dedup_required and event.dedup_key is None:
            raise NotificationValidationError("dedup_key_required")
        document, digest = descriptor.validate(event)
        return descriptor, document, digest

    def descriptors(self) -> tuple[NotificationDescriptor, ...]:
        return tuple(self._descriptors.values())

    def _freeze(self) -> NotificationRegistry:
        self._frozen = True
        return self


def _reject_prohibited_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = key.casefold() if isinstance(key, str) else ""
            if key_name in _PROHIBITED_KEYS or any(
                marker in key_name
                for marker in (
                    "credential",
                    "password",
                    "secret",
                    "stacktrace",
                    "traceback",
                )
            ):
                raise NotificationValidationError("payload_prohibited_field")
            _reject_prohibited_keys(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_prohibited_keys(item)


def build_default_registry() -> NotificationRegistry:
    descriptors = [
        NotificationDescriptor(
            event_type=event_type,
            schema_version=1,
            payload_type=DeferredNotificationPayload,
            serializer=_empty_serializer,
            validator=_empty_validator,
            default_severity=NotificationSeverity.INFO,
            renderer_id="deferred",
            publishable=False,
            deferred_reason="producer_schema_not_migrated",
        )
        for event_type in _TAXONOMY
    ]
    descriptors.append(
        NotificationDescriptor(
            event_type="runtime.handover.preemption_requested",
            schema_version=1,
            payload_type=HandoverPreemptionPayload,
            serializer=_handover_serializer,
            validator=_handover_validator,
            default_severity=NotificationSeverity.CRITICAL,
            renderer_id="handover.preemption",
            deserializer=_handover_deserializer,
            policy_capabilities=("handover_receipt",),
            dedup_required=True,
            default_priority=30,
            required_context_type=HandoverPublishContext,
        )
    )
    return NotificationRegistry(tuple(descriptors))


@lru_cache(maxsize=1)
def default_registry() -> NotificationRegistry:
    """Вернуть общий неизменяемый registry стандартных схем."""
    return build_default_registry()._freeze()


__all__ = [
    "DeferredNotificationPayload",
    "NotificationDescriptor",
    "NotificationRegistry",
    "build_default_registry",
    "default_registry",
]
