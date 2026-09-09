"""Low-cardinality observability-контракты notification foundation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self

from module.application.notifications.models import DeliveryResult
from module.observability.notifications import NotificationTelemetry


class _Instrument:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, str]]] = []

    def add(self, value: object, *, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))

    def record(self, value: object, *, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))


class _Meter:
    def __init__(self) -> None:
        self.instruments: dict[str, _Instrument] = {}

    def create_counter(self, name: str, **_kwargs: object) -> _Instrument:
        instrument = _Instrument()
        self.instruments[name] = instrument
        return instrument

    def create_histogram(self, name: str, **_kwargs: object) -> _Instrument:
        instrument = _Instrument()
        self.instruments[name] = instrument
        return instrument


class _Tracer:
    def start_as_current_span(self, name: str, *, attributes: dict[str, str]):
        return _Span(name, attributes)


class _Span:
    def __init__(self, name: str, attributes: dict[str, str]) -> None:
        self.name = name
        self.attributes = attributes

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FailingInstrument:
    def add(self, _value: object, *, attributes: dict[str, str]) -> None:
        raise RuntimeError("instrument unavailable")

    def record(self, _value: object, *, attributes: dict[str, str]) -> None:
        raise RuntimeError("instrument unavailable")


class _FailingMeter:
    def create_counter(self, _name: str, **_kwargs: object) -> _FailingInstrument:
        return _FailingInstrument()

    def create_histogram(self, _name: str, **_kwargs: object) -> _FailingInstrument:
        return _FailingInstrument()


class _FailingSpan:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        raise RuntimeError("tracer exit unavailable")


class _FailingTracer:
    def start_as_current_span(
        self, _name: str, *, attributes: dict[str, str]
    ) -> _FailingSpan:
        return _FailingSpan()


def test_notification_metrics_never_use_event_or_delivery_identity_as_label() -> None:
    meter = _Meter()
    telemetry = NotificationTelemetry(meter=meter, tracer=_Tracer())
    event = SimpleNamespace(
        source="runtime.dispatcher", id="event-secret", profile_id="profile-secret"
    )
    telemetry.record_publish(event=event, value="persisted")
    telemetry.record_rejected(event=event, value="arbitrary-secret-reason")
    claimed = SimpleNamespace(
        delivery=SimpleNamespace(channel_type="untrusted-channel", id="delivery-secret")
    )
    telemetry.record_attempt(claimed=claimed, result=DeliveryResult.unavailable())

    observed = [
        attributes
        for instrument in meter.instruments.values()
        for _, attributes in instrument.calls
    ]
    assert observed
    assert all("event_id" not in attrs for attrs in observed)
    assert all("delivery_id" not in attrs for attrs in observed)
    assert all("profile_id" not in attrs for attrs in observed)
    assert all("secret" not in str(attrs) for attrs in observed)
    assert any(attrs.get("source_domain") == "runtime" for attrs in observed)


def test_notification_span_keeps_only_allowlisted_attributes() -> None:
    telemetry = NotificationTelemetry(meter=_Meter(), tracer=_Tracer())
    with telemetry.span(
        "notification.channel.send",
        attributes={
            "channel_type": "test",
            "source_domain": "runtime",
            "event_id": "not-an-attribute",
        },
    ) as span:
        assert span.name == "notification.channel.send"
    assert span.attributes == {"channel_type": "test", "source_domain": "runtime"}


def test_notification_telemetry_stays_fail_open() -> None:
    telemetry = NotificationTelemetry(meter=_FailingMeter(), tracer=_FailingTracer())
    event = SimpleNamespace(source="runtime")

    telemetry.record_publish(event=event, value="persisted")
    telemetry.record_latency(channel_type="test", result_class="DELIVERED", seconds=1.0)
    with telemetry.span("notification.publish") as span:
        assert span is not None
