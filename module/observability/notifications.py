"""Low-cardinality OpenTelemetry signals для notification foundation."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Self

from module.application.notifications.reasons import NOTIFICATION_REASON_CODES

if TYPE_CHECKING:
    from module.application.notifications.models import DeliveryResult, PolicyDecision

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_PUBLISH_RESULTS = frozenset(
    {"persisted", "duplicate", "suppressed", "identity_conflict", "validation_failed", "unavailable"}
)
_SOURCE_DOMAINS = frozenset(
    {"runtime", "scheduler", "campaign", "commission", "opsi", "notification", "test"}
)
_RESULT_CLASSES = frozenset(
    {"DELIVERED", "PROVIDER_ACCEPTED", "TRANSIENT_FAILURE", "PERMANENT_FAILURE", "UNAVAILABLE", "SUPPRESSED"}
)
_POLICY_STATES = frozenset({"ROUTED", "SUPPRESSED"})
_CHANNEL_TYPES = frozenset(
    {"desktop", "desktop_agent", "telegram", "webhook", "onepush", "test", "unregistered"}
)
_DELIVERY_STATES = frozenset(
    {"PENDING", "IN_FLIGHT", "RETRY_WAIT", "FAILED", "PROVIDER_ACCEPTED", "AWAITING_AGENT_ACK", "DELIVERED", "SUPPRESSED"}
)


class NotificationTelemetry:
    """Переиспользует global OTel API и не создаёт отдельный exporter/provider."""

    def __init__(self, *, meter: Any | None = None, tracer: Any | None = None) -> None:
        if meter is None:
            from opentelemetry import metrics

            meter = metrics.get_meter("azurpilot.notifications")
        if tracer is None:
            from opentelemetry import trace

            tracer = trace.get_tracer("azurpilot.notifications")
        self._meter = meter
        self._tracer = tracer
        self._publish_total = self._counter("notification_publish_total")
        self._policy_total = self._counter("notification_policy_total")
        self._attempt_total = self._counter("notification_delivery_attempt_total")
        self._retry_total = self._counter("notification_retry_total")
        self._latency = self._histogram("notification_delivery_latency_seconds")
        self._backlog_age = self._histogram("notification_backlog_age_seconds")
        self._rejected_total = self._counter("notification_event_rejected_total")

    def _counter(self, name: str) -> Any:
        return self._meter.create_counter(name)

    def _histogram(self, name: str) -> Any:
        return self._meter.create_histogram(name, unit="s")

    def record_publish(self, *, event: object, value: str) -> None:
        self._safe_add(
            self._publish_total,
            {"source_domain": _source(event), "result": _choice(value, _PUBLISH_RESULTS)},
        )

    def record_policy(self, *, decision: PolicyDecision) -> None:
        self._safe_add(
            self._policy_total,
            {
                "policy_state": _choice(decision.state.value, _POLICY_STATES),
                "reason": _reason(decision.reason),
            },
        )

    def record_rejected(self, *, event: object, value: str) -> None:
        self._safe_add(
            self._rejected_total,
            {"source_domain": _source(event), "reason": _reason(value)},
        )

    def record_attempt(self, *, claimed: object, result: DeliveryResult) -> None:
        channel_type = _channel_type(claimed)
        result_class = _choice(result.result_class.value, _RESULT_CLASSES)
        self._safe_add(
            self._attempt_total,
            {"channel_type": _channel(channel_type), "result_class": result_class},
        )
        if result_class in {"TRANSIENT_FAILURE", "UNAVAILABLE"}:
            self._safe_add(
                self._retry_total,
                {"channel_type": _channel(channel_type), "reason": _reason(result.safe_error_code)},
            )

    def record_latency(self, *, channel_type: str, result_class: str, seconds: float) -> None:
        self._safe_record(
            self._latency,
            seconds,
            {
                "channel_type": _channel(channel_type),
                "result_class": _choice(result_class, _RESULT_CLASSES),
            },
        )

    def record_backlog_age(self, *, channel_type: str, state: str, seconds: float) -> None:
        self._safe_record(
            self._backlog_age,
            seconds,
            {"channel_type": _channel(channel_type), "state": _choice(state, _DELIVERY_STATES)},
        )

    @contextmanager
    def span(self, name: str, *, attributes: Mapping[str, object] | None = None) -> Iterator[Any]:
        """Создать bounded span; arbitrary event payload не добавляется в attributes."""
        try:
            context = self._tracer.start_as_current_span(
                name, attributes=_span_attributes(attributes or {})
            )
            span = context.__enter__()
        except Exception:  # noqa: BLE001 - OTel API fail-open boundary.
            yield _NoopSpan()
            return
        try:
            yield span
        except BaseException:
            try:
                context.__exit__(*sys.exc_info())
            except Exception:  # noqa: BLE001 - OTel API fail-open boundary.
                pass
            raise
        else:
            try:
                context.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - OTel API fail-open boundary.
                return

    def _safe_add(self, instrument: Any, attributes: Mapping[str, str]) -> None:
        try:
            instrument.add(1, attributes=attributes)
        except Exception:  # noqa: BLE001 - OTel API fail-open boundary.
            return

    def _safe_record(
        self, instrument: Any, value: float, attributes: Mapping[str, str]
    ) -> None:
        try:
            instrument.record(max(0.0, min(float(value), 86400.0)), attributes=attributes)
        except Exception:  # noqa: BLE001 - OTel API fail-open boundary.
            return


class _NoopSpan:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _token(value: object) -> str:
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return "unknown"


def _choice(value: object, choices: frozenset[str]) -> str:
    normalized = _token(value)
    return normalized if normalized in choices else "unknown"


def _reason(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _choice(value.casefold(), NOTIFICATION_REASON_CODES)


def _channel(value: object) -> str:
    return _choice(value, _CHANNEL_TYPES)


def _source(event: object) -> str:
    source = getattr(event, "source", None)
    if not isinstance(source, str):
        return "unknown"
    return _choice(source.split(".", 1)[0], _SOURCE_DOMAINS)


def _channel_type(claimed: object) -> str:
    delivery = getattr(claimed, "delivery", None)
    return _token(getattr(delivery, "channel_type", None))


def _span_attributes(attributes: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in attributes.items():
        if key == "source_domain":
            result[key] = _choice(value, _SOURCE_DOMAINS)
        elif key == "channel_type":
            result[key] = _channel(value)
        elif key == "result_class":
            result[key] = _choice(value, _RESULT_CLASSES)
        elif key == "policy_state":
            result[key] = _choice(value, _POLICY_STATES)
    return result


__all__ = ["NotificationTelemetry"]
