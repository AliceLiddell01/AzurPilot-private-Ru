"""Контракты typed schema, encoding и channel для notification foundation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from module.application.notifications import (
    ChannelCapabilities,
    DeferredNotificationPayload,
    DeliveryResult,
    DeliveryResultClass,
    HandoverPreemptionRenderer,
    NotificationChannelCatalog,
    NotificationDescriptor,
    NotificationRegistry,
    NotificationSeverity,
    NotificationValidationError,
    RenderedSnapshot,
    build_default_registry,
    default_registry,
)
from module.application.notifications.encoding import (
    MAX_PAYLOAD_BYTES,
    MAX_PAYLOAD_ITEMS,
    canonical_json,
    correlation_document,
    event_payload_digest,
)
from module.application.notifications.models import NotificationCorrelation
from tests.notification_test_support import _event


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


def test_canonical_payload_rejects_non_finite_decimal_and_deep_nesting() -> None:
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
