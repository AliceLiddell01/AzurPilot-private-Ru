"""Строгое каноническое кодирование notification payload и snapshots."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Final, NoReturn
from uuid import UUID

from module.application.errors import NotificationValidationError
from module.application.notifications.models import (
    NotificationCorrelation,
    NotificationEvent,
    NotificationPolicySnapshot,
    NotificationSubject,
    RenderedSnapshot,
)

MAX_PAYLOAD_BYTES: Final = 4 * 1024
MAX_EVENT_DIGEST_DOCUMENT_BYTES: Final = 16 * 1024
MAX_SNAPSHOT_BYTES: Final = 8 * 1024
MAX_PAYLOAD_DEPTH: Final = 16
MAX_PAYLOAD_ITEMS: Final = 256
MAX_PAYLOAD_NODES: Final = 4096


def _reject(reason: str) -> NoReturn:
    raise NotificationValidationError(reason)


def canonical_value(
    value: object, *, _depth: int = 0, _node_budget: list[int] | None = None
) -> object:
    """Преобразовать только известные JSON-совместимые доменные значения."""
    if _depth > MAX_PAYLOAD_DEPTH:
        _reject("payload_too_deep")
    if _node_budget is None:
        _node_budget = [MAX_PAYLOAD_NODES]
    if _node_budget[0] <= 0:
        _reject("payload_too_many_nodes")
    _node_budget[0] -= 1
    if isinstance(value, Enum):
        return canonical_value(
            value.value, _depth=_depth + 1, _node_budget=_node_budget
        )
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value if type(value) is str else str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            _reject("payload_non_finite_number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            _reject("payload_naive_datetime")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            _reject("payload_non_finite_number")
        return format(value.normalize(), "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        if len(value) > MAX_PAYLOAD_ITEMS:
            _reject("payload_too_large")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                _reject("payload_mapping_key_invalid")
            result[key] = canonical_value(
                item, _depth=_depth + 1, _node_budget=_node_budget
            )
        return result
    if isinstance(value, (tuple, list)):
        if len(value) > MAX_PAYLOAD_ITEMS:
            _reject("payload_too_large")
        return [
            canonical_value(item, _depth=_depth + 1, _node_budget=_node_budget)
            for item in value
        ]
    _reject("payload_value_type_invalid")


def canonical_json(value: object, *, max_bytes: int | None = None) -> bytes:
    encoded = json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if max_bytes is not None and len(encoded) > max_bytes:
        _reject("payload_too_large")
    return encoded


def canonical_digest(value: object, *, max_bytes: int | None = None) -> str:
    return sha256(canonical_json(value, max_bytes=max_bytes)).hexdigest()


def subject_document(subject: NotificationSubject | None) -> dict[str, object] | None:
    if subject is None:
        return None
    return {"kind": subject.kind, "id": subject.id}


def correlation_document(
    correlation: NotificationCorrelation | None,
) -> dict[str, object] | None:
    if correlation is None:
        return None
    result: dict[str, object] = {}
    if correlation.task_id is not None:
        result["task_id"] = correlation.task_id
    if correlation.runtime_session_id is not None:
        result["runtime_session_id"] = correlation.runtime_session_id
    if correlation.parent_event_id is not None:
        result["parent_event_id"] = correlation.parent_event_id
    if correlation.trace_id is not None:
        result["trace_id"] = correlation.trace_id
    if correlation.span_id is not None:
        result["span_id"] = correlation.span_id
    return result or None


def event_document(
    event: NotificationEvent,
    payload_document: Mapping[str, object],
    *,
    include_id: bool = False,
) -> dict[str, object]:
    document: dict[str, object] = {
        "source": event.source,
        "type": event.type,
        "schema_version": event.schema_version,
        "profile_id": event.profile_id,
        "runtime_instance_id": event.runtime_instance_id,
        "subject": subject_document(event.subject),
        "severity": event.severity,
        "occurred_at": event.occurred_at,
        "data": dict(payload_document),
        "dedup_key": event.dedup_key,
        "correlation": correlation_document(event.correlation),
        "sensitivity": event.sensitivity,
    }
    if include_id:
        document["id"] = event.id
    return document


def event_payload_digest(
    event: NotificationEvent, payload_document: Mapping[str, object]
) -> str:
    """Рассчитать digest immutable occurrence-полей без server-owned значений и UUID id."""
    return canonical_digest(
        event_document(event, payload_document),
        max_bytes=MAX_EVENT_DIGEST_DOCUMENT_BYTES,
    )


def notification_delivery_idempotency_key(
    *, source: str, event_id: UUID, channel_instance_id: str
) -> str:
    """Сформировать bounded identity key без delimiter collision."""
    identity = {
        "domain": "notification-delivery-v1",
        "source": source,
        "event_id": event_id,
        "channel_instance_id": channel_instance_id,
    }
    return "notification-delivery-v1:" + canonical_digest(identity, max_bytes=1024)


def policy_snapshot_document(snapshot: NotificationPolicySnapshot) -> dict[str, object]:
    action = snapshot.action
    return {
        "policy_version": snapshot.policy_version,
        "rule_id": snapshot.rule_id,
        "channel_instance_ids": list(action.channel_instance_ids),
        "suppression_reason": action.suppression_reason,
        "locale": action.locale,
        "presentation_profile": action.presentation_profile,
        "priority": action.priority,
    }


def rendered_snapshot_document(snapshot: RenderedSnapshot) -> dict[str, object]:
    return {
        "locale": snapshot.locale,
        "renderer_id": snapshot.renderer_id,
        "renderer_version": snapshot.renderer_version,
        "title": snapshot.title,
        "body": snapshot.body,
    }


__all__ = [
    "MAX_EVENT_DIGEST_DOCUMENT_BYTES",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "MAX_PAYLOAD_ITEMS",
    "MAX_PAYLOAD_NODES",
    "MAX_SNAPSHOT_BYTES",
    "canonical_digest",
    "canonical_json",
    "canonical_value",
    "correlation_document",
    "event_document",
    "event_payload_digest",
    "notification_delivery_idempotency_key",
    "policy_snapshot_document",
    "rendered_snapshot_document",
    "subject_document",
]
