"""Контракты typed schema, encoding и channel для notification foundation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest

from module.application.notifications import (
    ChannelCapabilities,
    DeferredNotificationPayload,
    DeliveryResult,
    DeliveryResultClass,
    HandoverPreemptionRenderer,
    NotificationChannelCatalog,
    NotificationDescriptor,
    NotificationEvent,
    NotificationRegistry,
    NotificationSensitivity,
    NotificationSeverity,
    NotificationValidationError,
    RenderedSnapshot,
    ReceiptStrength,
    build_default_registry,
    default_registry,
)
from module.application.notifications.encoding import (
    MAX_PAYLOAD_BYTES,
    MAX_EVENT_DIGEST_DOCUMENT_BYTES,
    MAX_PAYLOAD_ITEMS,
    MAX_PAYLOAD_NODES,
    canonical_json,
    correlation_document,
    event_payload_digest,
    event_document,
    notification_delivery_idempotency_key,
)
from module.application.notifications.models import (
    NotificationCorrelation,
    NotificationSubject,
)
from tests.support.notifications import _event


@dataclass(frozen=True)
class _LargePayload:
    text: str


def _large_payload_registry() -> NotificationRegistry:
    return NotificationRegistry(
        (
            NotificationDescriptor(
                event_type="test.large",
                schema_version=1,
                payload_type=_LargePayload,
                serializer=lambda payload: {"text": payload.text},
                validator=lambda _event, _payload, _document: None,
                default_severity=NotificationSeverity.INFO,
                renderer_id="deferred",
                deserializer=lambda document: _LargePayload(document["text"]),
            ),
        )
    )


def test_registry_rejects_naive_and_canonicalizes_handover_payload() -> None:
    registry = build_default_registry()
    event = _event()
    _, document, digest = registry.validate(event)
    assert document["deadline_at"] == "2026-09-10T12:00:30+00:00"
    assert digest == event_payload_digest(event, document)

    invalid = replace(event, occurred_at=datetime(2026, 9, 10, 12, 0))  # noqa: DTZ001
    with pytest.raises(NotificationValidationError) as error:
        registry.validate(invalid)
    assert error.value.reason_code == "occurred_at_not_aware"


@pytest.mark.parametrize("reason_code", ("bad reason", "x" * 65, None))
def test_notification_reason_code_is_bounded_before_message_interpolation(
    reason_code: object,
) -> None:
    with pytest.raises(ValueError, match="неверный формат"):
        NotificationValidationError(reason_code)  # type: ignore[arg-type]


def test_handover_descriptor_requires_agent_ack_receipt_strength() -> None:
    descriptor = build_default_registry().require(
        "runtime.handover.preemption_requested", 1
    )
    assert descriptor.required_receipt_strength is ReceiptStrength.AGENT_ACK
    assert (
        build_default_registry().require("task.completed", 1).required_receipt_strength
        is None
    )


def test_delivery_idempotency_key_is_structured_and_versioned() -> None:
    first = notification_delivery_idempotency_key(
        source="a",
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        channel_instance_id="x:00000000-0000-0000-0000-000000000002:y",
    )
    collision_candidate = notification_delivery_idempotency_key(
        source="a:00000000-0000-0000-0000-000000000001:x",
        event_id=UUID("00000000-0000-0000-0000-000000000002"),
        channel_instance_id="y",
    )

    assert first.startswith("notification-delivery-v1:")
    assert first != collision_candidate
    assert first == notification_delivery_idempotency_key(
        source="a",
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        channel_instance_id="x:00000000-0000-0000-0000-000000000002:y",
    )


def test_payload_limit_is_separate_from_bounded_event_digest_envelope() -> None:
    event = replace(
        _event(),
        type="test.large",
        severity=NotificationSeverity.INFO,
        profile_id="p" * 128,
        runtime_instance_id="r" * 128,
        subject=NotificationSubject(kind="s" * 32, id="i" * 128),
        correlation=NotificationCorrelation(
            task_id="t" * 128,
            runtime_session_id="u" * 128,
            trace_id="a" * 32,
            span_id="b" * 16,
        ),
        data=_LargePayload("x" * 3500),
        dedup_key=None,
        sensitivity=NotificationSensitivity.SENSITIVE,
    )
    registry = _large_payload_registry()

    _, document, digest = registry.validate(event)

    assert len(canonical_json(document, max_bytes=MAX_PAYLOAD_BYTES)) <= MAX_PAYLOAD_BYTES
    assert len(canonical_json(event_document(event, document))) > MAX_PAYLOAD_BYTES
    assert len(digest) == 64
    assert MAX_EVENT_DIGEST_DOCUMENT_BYTES > MAX_PAYLOAD_BYTES


def test_payload_over_4kib_remains_rejected() -> None:
    event = replace(
        _event(),
        type="test.large",
        severity=NotificationSeverity.INFO,
        data=_LargePayload("x" * 4096),
        dedup_key=None,
    )

    with pytest.raises(NotificationValidationError, match="payload_too_large"):
        _large_payload_registry().validate(event)


@pytest.mark.parametrize("key", ("access_token", "auth_header", "session_cookie", "callback_url"))
def test_payload_rejects_compound_prohibited_keys(key: str) -> None:
    descriptor = NotificationDescriptor(
        event_type="test.large",
        schema_version=1,
        payload_type=_LargePayload,
        serializer=lambda payload, key=key: {key: payload.text},
        validator=lambda _event, _payload, _document: None,
        default_severity=NotificationSeverity.INFO,
        renderer_id="deferred",
        deserializer=lambda document: _LargePayload(document[key]),
    )
    event = replace(
        _event(),
        type="test.large",
        severity=NotificationSeverity.INFO,
        data=_LargePayload("bounded"),
        dedup_key=None,
    )

    with pytest.raises(NotificationValidationError, match="payload_prohibited_field"):
        descriptor.validate(event)


@pytest.mark.parametrize(
    "value",
    (
        {str(index): index for index in range(MAX_PAYLOAD_ITEMS + 1)},
        list(range(MAX_PAYLOAD_ITEMS + 1)),
    ),
)
def test_canonical_value_rejects_oversized_containers(value: object) -> None:
    with pytest.raises(NotificationValidationError, match="payload_too_large"):
        canonical_json(value, max_bytes=MAX_PAYLOAD_BYTES)


def test_canonical_value_rejects_excessive_total_nodes() -> None:
    nested: object = []
    for _ in range(13):
        nested = [nested, nested]

    with pytest.raises(NotificationValidationError, match="payload_too_many_nodes"):
        canonical_json(nested)

    assert MAX_PAYLOAD_NODES > MAX_PAYLOAD_ITEMS


def test_rendered_snapshot_allows_newline_only_in_body() -> None:
    snapshot = RenderedSnapshot(
        locale="ru-RU",
        renderer_id="test.renderer",
        renderer_version="v1",
        title="Заголовок",
        body="Первая строка\nВторая строка",
    )
    assert snapshot.is_valid(max_title=256, max_body=2048, max_payload_bytes=4096)
    assert not replace(snapshot, title="Заголовок\n").is_valid(
        max_title=256, max_body=2048, max_payload_bytes=4096
    )
    assert not replace(snapshot, body="Первая строка\tВторая строка").is_valid(
        max_title=256, max_body=2048, max_payload_bytes=4096
    )


def test_channel_capabilities_bound_title_by_payload() -> None:
    assert not ChannelCapabilities(
        max_payload_bytes=256,
        max_title_length=257,
        max_body_length=256,
    ).is_valid()


def test_empty_correlation_is_canonicalized_as_null() -> None:
    assert correlation_document(NotificationCorrelation()) is None


def test_handover_renderer_rejects_unknown_presentation_profile() -> None:
    with pytest.raises(NotificationValidationError) as error:
        HandoverPreemptionRenderer().render(
            _event(),
            locale="ru-RU",
            capabilities=ChannelCapabilities(),
            presentation_profile="compact",
        )

    assert error.value.reason_code == "renderer_presentation_profile_unsupported"


def test_default_registry_is_cached_and_frozen() -> None:
    registry = default_registry()

    assert registry is default_registry()
    with pytest.raises(RuntimeError, match="неизменяем"):
        registry.register(registry.descriptors()[0])


def test_publishable_descriptor_requires_typed_deserializer() -> None:
    descriptor = NotificationDescriptor(
        event_type="test.publishable",
        schema_version=1,
        payload_type=DeferredNotificationPayload,
        serializer=lambda _payload: {},
        validator=lambda _event, _payload, _document: None,
        default_severity=NotificationSeverity.INFO,
        renderer_id="deferred",
    )
    with pytest.raises(ValueError, match="typed deserializer"):
        NotificationRegistry((descriptor,))


def test_registry_rejects_event_type_over_storage_limit() -> None:
    descriptor = NotificationDescriptor(
        event_type="a" * 129,
        schema_version=1,
        payload_type=DeferredNotificationPayload,
        serializer=lambda _payload: {},
        validator=lambda _event, _payload, _document: None,
        default_severity=NotificationSeverity.INFO,
        renderer_id="deferred",
        publishable=False,
        deferred_reason="producer_schema_not_migrated",
    )

    with pytest.raises(ValueError, match="lowercase dotted token"):
        NotificationRegistry((descriptor,))


def test_handover_deserializer_rejects_naive_deadline() -> None:
    registry = build_default_registry()
    _, document, _ = registry.validate(_event())
    document["deadline_at"] = "2026-09-10T12:00:30"

    with pytest.raises(NotificationValidationError, match="stored_payload_invalid"):
        registry.require(
            "runtime.handover.preemption_requested", 1
        ).deserialize(document)


def test_canonical_payload_rejects_non_finite_decimal_and_deep_nesting() -> None:
    assert canonical_json(Decimal("1E+2")) == b'"100"'

    with pytest.raises(NotificationValidationError) as decimal_error:
        canonical_json(Decimal("NaN"))
    assert decimal_error.value.reason_code == "payload_non_finite_number"

    nested: object = "leaf"
    for _ in range(17):
        nested = {"value": nested}
    with pytest.raises(NotificationValidationError) as depth_error:
        canonical_json(nested)
    assert depth_error.value.reason_code == "payload_too_deep"


def test_delivery_result_rejects_unsafe_provider_data() -> None:
    assert not DeliveryResult.transient_failure(
        "channel_failed", summary="password=redacted"
    ).is_valid()
    assert not DeliveryResult.provider_accepted(
        provider_message_id="https://provider.example/message"
    ).is_valid()
    assert not DeliveryResult.transient_failure(
        "channel_failed", summary="serial=redacted"
    ).is_valid()
    assert not DeliveryResult.transient_failure("bearer_token").is_valid()
    assert DeliveryResult.transient_failure(
        "serialization_error", summary="serialization_error"
    ).is_valid()
    assert DeliveryResult.provider_accepted(provider_message_id="tokenizer-v1").is_valid()
    assert not DeliveryResult(
        DeliveryResultClass.DELIVERED,
        safe_error_code="unexpected_error",
    ).is_valid()
    assert not DeliveryResult(
        DeliveryResultClass.PROVIDER_ACCEPTED,
        safe_error_code="unexpected_error",
    ).is_valid()
    assert not DeliveryResult(
        DeliveryResultClass.PERMANENT_FAILURE,
        safe_error_code="permanent_error",
        retry_after_seconds=1,
    ).is_valid()


def test_channel_catalog_rejects_incomplete_adapter_as_typed_error() -> None:
    with pytest.raises(TypeError, match="атрибуты"):
        NotificationChannelCatalog((object(),))


def test_deferred_taxonomy_descriptor_rejects_publish_without_generic_payload() -> None:
    registry = build_default_registry()
    event = replace(
        _event(),
        type="task.completed",
        severity=NotificationSeverity.INFO,
        data=DeferredNotificationPayload(),
        dedup_key=None,
    )

    with pytest.raises(NotificationValidationError) as error:
        registry.validate(event)

    assert error.value.reason_code == "producer_schema_not_migrated"
