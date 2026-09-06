"""Fail-open application tracing через существующий OTLP observability runtime."""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from module.logging_core import is_sensitive_name, sanitize_log_text
from module.observability._shared import (
    _OUTCOMES,
    _bounded_exception_stacktrace,
    _exception_type_name,
    _format_exception_chain,
    _metric_label,
    _outcome_from_exception,
    _outcome_from_result,
    _profile_label,
    _safe_exception_message,
)

_TRACE_SCOPE_NAME = "azurpilot.observability"
_TASK_SPAN_NAME = "azurpilot.task.run"
_MAX_CHILD_SPANS_PER_TASK = 128
_MAX_SCREENSHOT_SPANS_PER_TASK = 64
_SCREENSHOT_OPERATION_NAME = "azurpilot.device.screenshot"
_MAX_OPERATION_ATTRIBUTES = 8
_MAX_OPERATION_ATTRIBUTE_KEY = 64
_MAX_OPERATION_ATTRIBUTE_VALUE = 256
_OPERATION_NAME_RE = re.compile(
    r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+",
    re.ASCII,
)

_runtime_lock = threading.RLock()
_active_runtime: TracingRuntime | None = None
_task_depth: ContextVar[int] = ContextVar("azurpilot_tracing_task_depth", default=0)
_task_outcome: ContextVar[str | None] = ContextVar(
    "azurpilot_tracing_task_outcome",
    default=None,
)
_child_span_count: ContextVar[int] = ContextVar(
    "azurpilot_tracing_child_span_count",
    default=0,
)
_screenshot_span_count: ContextVar[int] = ContextVar(
    "azurpilot_tracing_screenshot_span_count",
    default=0,
)


@dataclass(frozen=True)
class TracingConfig:
    """Проверенный bounded contract traces exporter-а."""

    endpoint: str | None
    timeout_millis: int
    schedule_delay_millis: int
    max_queue_size: int
    max_export_batch_size: int
    processor_timeout_millis: int


def _report(
    reporter: Any, message: str, exception: BaseException | None = None
) -> None:
    try:
        reporter.report(message, exception)
    except Exception:
        pass


def _safe_operation_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        value = value.strip()
    except Exception:
        return None
    if (
        not value
        or len(value) > _MAX_OPERATION_ATTRIBUTE_KEY
        or _OPERATION_NAME_RE.fullmatch(value) is None
    ):
        return None
    return value


def _safe_operation_attributes(
    value: Mapping[object, object] | None,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    attributes: dict[str, object] = {}
    for key, item in value.items():
        if len(attributes) >= _MAX_OPERATION_ATTRIBUTES:
            break
        if not isinstance(key, str):
            continue
        try:
            normalized_key = key.strip()
        except Exception:
            continue
        if (
            not normalized_key
            or len(normalized_key) > _MAX_OPERATION_ATTRIBUTE_KEY
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                for char in normalized_key
            )
        ):
            continue
        if is_sensitive_name(normalized_key):
            attributes[normalized_key] = "***"
            continue
        if isinstance(item, (bool, int, float)):
            attributes[normalized_key] = item
            continue
        if not isinstance(item, str):
            continue
        try:
            normalized_value = sanitize_log_text(item, _MAX_OPERATION_ATTRIBUTE_VALUE)
        except Exception:
            continue
        if normalized_value:
            attributes[normalized_key] = normalized_value
    return attributes


def _set_span_outcome(span: Any, outcome: str) -> None:
    try:
        span.set_attribute(
            "azurpilot.task.outcome",
            outcome if outcome in _OUTCOMES else "unknown",
        )
        from opentelemetry.trace import Status, StatusCode

        if outcome == "failure":
            span.set_status(Status(StatusCode.ERROR))
        elif outcome == "success":
            span.set_status(Status(StatusCode.OK))
    except Exception:
        return None


def _record_sanitized_exception(
    span: Any,
    exception: BaseException,
    traceback_object: TracebackType | None,
) -> None:
    """Записать bounded exception event через уже существующую policy logs."""
    try:
        type_name = _exception_type_name(type(exception), exception)
        message = _safe_exception_message(exception)
        stacktrace = _bounded_exception_stacktrace(
            _format_exception_chain(exception, traceback_object)
        )
        span.add_event(
            "exception",
            attributes={
                "exception.type": type_name,
                "exception.message": message,
                "exception.stacktrace": stacktrace,
            },
        )
    except Exception:
        try:
            span.add_event(
                "exception",
                attributes={"exception.type": type(exception).__name__},
            )
        except Exception:
            pass


class _FailOpenSpanExporter:
    """Изолировать ошибки OTLP trace exporter от gameplay и scheduler."""

    def __init__(self, exporter: Any, reporter: Any) -> None:
        self._exporter = exporter
        self._reporter = reporter

    def export(self, spans: Any) -> Any:
        try:
            result = self._exporter.export(spans)
        except Exception as exc:
            _report(
                self._reporter,
                "OTLP trace exporter недоступен; остальные сигналы продолжат работу",
                exc,
            )
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.FAILURE
        if getattr(result, "name", "") == "FAILURE":
            _report(
                self._reporter,
                "OTLP trace exporter отклонил пакет; остальные сигналы продолжат работу",
            )
        return result

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception as exc:
            _report(self._reporter, "Не удалось завершить OTLP trace exporter", exc)


@dataclass
class TracingRuntime:
    """Process-local TracerProvider и один BatchSpanProcessor."""

    provider: Any
    tracer: Any
    owner_pid: int
    reporter: Any
    active: bool = True

    def after_fork(self) -> None:
        """Отключить унаследованный provider до explicit child bootstrap."""
        self.active = False

    def shutdown(self, timeout_millis: int) -> bool:
        self.active = False
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        finished = threading.Event()

        def close_provider() -> None:
            try:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                try:
                    if not self.provider.force_flush(timeout_millis=remaining):
                        _report(
                            self.reporter,
                            "Не удалось сбросить буфер application traces",
                        )
                except Exception as exc:
                    _report(
                        self.reporter,
                        "Не удалось сбросить буфер application traces",
                        exc,
                    )
                try:
                    self.provider.shutdown()
                except Exception as exc:
                    _report(self.reporter, "Не удалось завершить traces provider", exc)
            finally:
                finished.set()

        worker = threading.Thread(
            target=close_provider,
            name="AzurPilotOtelTraceShutdown",
            daemon=True,
        )
        worker.start()
        completed = finished.wait(max(0, deadline - time.monotonic()))
        if not completed:
            _report(
                self.reporter,
                "Завершение application traces остановлено по bounded timeout",
            )
        return completed


def activate_tracing_runtime(runtime: TracingRuntime) -> None:
    global _active_runtime
    with _runtime_lock:
        _active_runtime = runtime


def deactivate_tracing_runtime(runtime: TracingRuntime | None) -> None:
    global _active_runtime
    with _runtime_lock:
        if runtime is None or _active_runtime is runtime:
            _active_runtime = None


def reset_tracing_runtime_after_fork() -> None:
    """Сбросить process-local registry в child без захвата parent lock."""
    global _runtime_lock, _active_runtime
    _runtime_lock = threading.RLock()
    runtime = _active_runtime
    _active_runtime = None
    _task_depth.set(0)
    _task_outcome.set(None)
    _child_span_count.set(0)
    _screenshot_span_count.set(0)
    if runtime is not None:
        runtime.after_fork()


def get_active_tracing_runtime() -> TracingRuntime | None:
    with _runtime_lock:
        runtime = _active_runtime
    if runtime is None or not runtime.active or runtime.owner_pid != os.getpid():
        return None
    return runtime


def build_tracing_runtime(
    config: TracingConfig,
    *,
    resource: Any,
    reporter: Any,
    exporter_factory: Any | None = None,
) -> TracingRuntime:
    """Создать traces provider только после explicit application opt-in."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    class _ProcessLocalBatchSpanProcessor(BatchSpanProcessor):
        """Не запускать унаследованный BatchProcessor worker после fork."""

        def _at_fork_reinit(self) -> None:
            return None

    exporter = None
    provider = None
    processor = None
    processor_added = False
    try:
        exporter = (
            exporter_factory(config.timeout_millis)
            if exporter_factory is not None
            else OTLPSpanExporter(
                endpoint=config.endpoint,
                timeout=config.timeout_millis / 1000,
            )
        )
        provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        processor = _ProcessLocalBatchSpanProcessor(
            _FailOpenSpanExporter(exporter, reporter),
            schedule_delay_millis=config.schedule_delay_millis,
            max_queue_size=config.max_queue_size,
            max_export_batch_size=config.max_export_batch_size,
            export_timeout_millis=config.processor_timeout_millis,
        )
        provider.add_span_processor(processor)
        processor_added = True
        return TracingRuntime(
            provider=provider,
            tracer=provider.get_tracer(_TRACE_SCOPE_NAME),
            owner_pid=os.getpid(),
            reporter=reporter,
        )
    except Exception:
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass
        if processor is not None and not processor_added:
            try:
                processor.shutdown()
            except Exception:
                if exporter is not None:
                    try:
                        exporter.shutdown()
                    except Exception:
                        pass
        elif exporter is not None and processor is None:
            try:
                exporter.shutdown()
            except Exception:
                pass
        raise


class TraceTaskRun:
    """Контекст root span одной canonical scheduler task boundary."""

    def __init__(self, *, profile: object, task: object) -> None:
        self._profile = _profile_label(profile)
        self._task = _metric_label(task)
        self._runtime: TracingRuntime | None = None
        self._span_context: Any = None
        self._span: Any = None
        self._depth_token: Any = None
        self._outcome_token: Any = None
        self._child_count_token: Any = None
        self._screenshot_count_token: Any = None
        self._nested = False
        self._finished = False
        self._result_outcome: str | None = None

    def __enter__(self) -> Self:
        if _task_depth.get() > 0:
            self._nested = True
            return self
        runtime = get_active_tracing_runtime()
        if runtime is None:
            return self
        self._runtime = runtime
        self._depth_token = _task_depth.set(1)
        self._outcome_token = _task_outcome.set(None)
        self._child_count_token = _child_span_count.set(0)
        self._screenshot_count_token = _screenshot_span_count.set(0)
        try:
            self._span_context = runtime.tracer.start_as_current_span(
                _TASK_SPAN_NAME,
                attributes={
                    "azurpilot.profile": self._profile,
                    "azurpilot.task": self._task,
                },
                record_exception=False,
                set_status_on_exception=False,
            )
            self._span = self._span_context.__enter__()
        except Exception as exc:
            _report(
                runtime.reporter,
                "Не удалось начать application trace; задача продолжит работу",
                exc,
            )
            self._span_context = None
            self._span = None
            self._runtime = None
            if self._child_count_token is not None:
                _child_span_count.reset(self._child_count_token)
            if self._screenshot_count_token is not None:
                _screenshot_span_count.reset(self._screenshot_count_token)
            if self._outcome_token is not None:
                _task_outcome.reset(self._outcome_token)
            if self._depth_token is not None:
                _task_depth.reset(self._depth_token)
        return self

    def finish(self, result: object) -> None:
        if self._nested or self._runtime is None:
            return
        self._result_outcome = _outcome_from_result(result)
        self._finished = True

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback_object: TracebackType | None,
    ) -> bool:
        if self._nested or self._runtime is None:
            return False
        try:
            outcome = (
                _outcome_from_exception(exception)
                if exception is not None
                else _task_outcome.get()
                or (self._result_outcome if self._finished else "unknown")
            )
            if self._span is not None:
                _set_span_outcome(self._span, outcome)
                if exception is not None and outcome != "stopped":
                    _record_sanitized_exception(
                        self._span,
                        exception,
                        traceback_object,
                    )
                    try:
                        from opentelemetry.trace import Status, StatusCode

                        self._span.set_status(Status(StatusCode.ERROR))
                    except Exception:
                        pass
        finally:
            if self._span_context is not None:
                try:
                    # Запись необработанного исключения намеренно отключена выше.
                    self._span_context.__exit__(None, None, None)
                except Exception as exc:
                    _report(
                        self._runtime.reporter,
                        "Не удалось завершить application trace span",
                        exc,
                    )
            if self._child_count_token is not None:
                _child_span_count.reset(self._child_count_token)
            if self._screenshot_count_token is not None:
                _screenshot_span_count.reset(self._screenshot_count_token)
            if self._outcome_token is not None:
                _task_outcome.reset(self._outcome_token)
            if self._depth_token is not None:
                _task_depth.reset(self._depth_token)
        return False


def scheduler_task_span(*, profile: object, task: object) -> TraceTaskRun:
    """Создать root trace context для уже разрешённой scheduler task."""
    return TraceTaskRun(profile=profile, task=task)


def mark_task_stopped() -> None:
    """Отметить normal ``TaskEnd`` как stopped без изменения control flow."""
    if _task_depth.get() > 0:
        _task_outcome.set("stopped")


@contextmanager
def trace_operation(
    name: str,
    *,
    attributes: Mapping[object, object] | None = None,
) -> Iterator[Any]:
    """Создать bounded child span только внутри canonical task root."""
    if _task_depth.get() <= 0:
        yield None
        return

    runtime = get_active_tracing_runtime()
    operation_name = _safe_operation_name(name)
    screenshot_budget_exhausted = (
        operation_name == _SCREENSHOT_OPERATION_NAME
        and _screenshot_span_count.get() >= _MAX_SCREENSHOT_SPANS_PER_TASK
    )
    if (
        runtime is None
        or operation_name is None
        or _child_span_count.get() >= _MAX_CHILD_SPANS_PER_TASK
        or screenshot_budget_exhausted
    ):
        yield None
        return

    try:
        span_context = runtime.tracer.start_as_current_span(
            operation_name,
            attributes=_safe_operation_attributes(attributes),
            record_exception=False,
            set_status_on_exception=False,
        )
        span = span_context.__enter__()
    except Exception as exc:
        _report(
            runtime.reporter,
            "Не удалось начать application child span; операция продолжит работу",
            exc,
        )
        yield None
        return

    _child_span_count.set(_child_span_count.get() + 1)
    if operation_name == _SCREENSHOT_OPERATION_NAME:
        _screenshot_span_count.set(_screenshot_span_count.get() + 1)
    try:
        try:
            yield span
        except BaseException as exc:
            if _outcome_from_exception(exc) != "stopped":
                _record_sanitized_exception(span, exc, exc.__traceback__)
                try:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(Status(StatusCode.ERROR))
                except Exception:
                    pass
            raise
    finally:
        try:
            span_context.__exit__(None, None, None)
        except Exception as exc:
            _report(
                runtime.reporter, "Не удалось завершить application child span", exc
            )


__all__ = (
    "TraceTaskRun",
    "TracingConfig",
    "TracingRuntime",
    "activate_tracing_runtime",
    "build_tracing_runtime",
    "deactivate_tracing_runtime",
    "get_active_tracing_runtime",
    "mark_task_stopped",
    "reset_tracing_runtime_after_fork",
    "scheduler_task_span",
    "trace_operation",
)
