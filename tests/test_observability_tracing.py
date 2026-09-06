"""Семантические тесты process-local application tracing signal-а."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import get_current_span

import module.observability.scheduler as scheduler_module
from alas import AzurLaneAutoScript
from module.config.config import Function, TaskEnd
from module.observability import scheduler_task_run
from module.observability.bootstrap import (
    _after_fork,
    _read_config,
    _runtimes,
    configure_application_observability,
    shutdown_application_observability,
)
from module.observability.tracing import (
    build_tracing_runtime,
    get_active_tracing_runtime,
    trace_operation,
)

_OTEL_ENVIRONMENT_KEYS = (
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_METRICS_EXEMPLAR_FILTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_METRIC_EXPORT_TIMEOUT",
    "OTEL_BLRP_SCHEDULE_DELAY",
    "OTEL_BLRP_MAX_QUEUE_SIZE",
    "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
    "OTEL_BLRP_EXPORT_TIMEOUT",
    "OTEL_BSP_SCHEDULE_DELAY",
    "OTEL_BSP_MAX_QUEUE_SIZE",
    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
    "OTEL_BSP_EXPORT_TIMEOUT",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_SDK_DISABLED",
)


def _clear_environment(monkeypatch):
    for key in _OTEL_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def _new_logger(name: str) -> logging.Logger:
    target = logging.getLogger(name)
    for handler in list(target.handlers):
        target.removeHandler(handler)
        handler.close()
    target.filters.clear()
    target.propagate = False
    target.setLevel(logging.DEBUG)
    return target


def _task(command: str = "Research") -> Function:
    return Function({"Scheduler": {"Command": command}})


def _enable_traces(monkeypatch, *, logs=False, metrics=False):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://collector:4318/v1/traces",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "10000")
    if logs:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
            "http://collector:4318/v1/logs",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")
    if metrics:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "http://collector:4318/v1/metrics",
        )


@pytest.mark.parametrize(
    ("logs", "metrics", "traces"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_signal_configuration_combinations_are_independent(
    monkeypatch,
    logs,
    metrics,
    traces,
):
    _clear_environment(monkeypatch)
    if logs:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
            "http://collector:4318/v1/logs",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")
    if metrics:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            "http://collector:4318/v1/metrics",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "http/protobuf")
    if traces:
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://collector:4318/v1/traces",
        )
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")

    config = _read_config()
    if not any((logs, metrics, traces)):
        assert config is None
        return
    assert config is not None
    assert config.logs_enabled is logs
    assert (config.metrics is not None) is metrics
    assert (config.traces is not None) is traces


def test_bad_trace_configuration_does_not_disable_application_logs(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "http://collector:4318/v1/logs",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "http://collector:4318/v1/traces",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
    target = _new_logger("observability-trace-config")
    log_exporter = InMemoryLogRecordExporter()

    def forbidden_trace_factory(_timeout):
        raise AssertionError("фабрика trace-экспортёра не должна вызываться")

    try:
        config = _read_config()
        assert config is not None
        assert config.logs_enabled
        assert config.traces is None
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: log_exporter,
            _traces_exporter_factory=forbidden_trace_factory,
        )
        target.info("logs survive trace configuration failure")
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert [item.log_record.body for item in log_exporter.get_finished_logs()] == [
            "logs survive trace configuration failure"
        ]
    finally:
        shutdown_application_observability(target)


def test_scheduler_boundary_closes_metrics_when_tracing_enter_fails(monkeypatch):
    events = []

    class RecordingMetrics:
        def __enter__(self):
            events.append("metrics-enter")
            return self

        def __exit__(self, exception_type, exception, traceback_object):
            events.append(("metrics-exit", exception_type, exception))
            return False

        task_name = "Research"

    class FailingTracing:
        def __enter__(self):
            events.append("tracing-enter")
            raise RuntimeError("trace enter failed")

        def __exit__(self, *_args):
            events.append("tracing-exit")
            return False

        def finish(self, _result):
            raise AssertionError("finish must not run after failed enter")

    metrics = RecordingMetrics()
    tracing = FailingTracing()
    monkeypatch.setattr(
        scheduler_module,
        "metrics_task_run",
        lambda **_kwargs: metrics,
    )
    monkeypatch.setattr(
        scheduler_module,
        "scheduler_task_span",
        lambda **_kwargs: tracing,
    )

    with pytest.raises(RuntimeError, match="trace enter failed"):
        scheduler_module.scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ).__enter__()

    assert events[0] == "metrics-enter"
    assert events[1] == "tracing-enter"
    assert events[2][0] == "metrics-exit"
    assert events[2][1] is RuntimeError
    assert isinstance(events[2][2], RuntimeError)


def test_trace_exporter_uses_standard_endpoint_precedence_without_duplicate_path(
    monkeypatch,
):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _clear_environment(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    generic_exporter = OTLPSpanExporter(timeout=1)
    try:
        assert generic_exporter._endpoint == "http://collector:4318/v1/traces"
    finally:
        generic_exporter.shutdown()

    specific_exporter = OTLPSpanExporter(
        endpoint="http://collector:4318/v1/traces",
        timeout=1,
    )
    try:
        assert specific_exporter._endpoint == "http://collector:4318/v1/traces"
    finally:
        specific_exporter.shutdown()


def test_build_tracing_runtime_closes_partial_resources(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter_module
    import opentelemetry.sdk.trace as trace_module
    import opentelemetry.sdk.trace.export as trace_export_module

    class Exporter:
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    class Provider:
        def __init__(self, **_kwargs):
            self.shutdown_calls = 0

        def add_span_processor(self, _processor):
            raise RuntimeError("processor registration failed")

        def shutdown(self):
            self.shutdown_calls += 1

    processor_instances = []

    class Processor:
        def __init__(self, exporter, **_kwargs):
            processor_instances.append(self)
            self.exporter = exporter
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1
            self.exporter.shutdown()

    exporter = Exporter()
    provider = None

    def provider_factory(**kwargs):
        nonlocal provider
        provider = Provider(**kwargs)
        return provider

    monkeypatch.setattr(trace_module, "TracerProvider", provider_factory)
    monkeypatch.setattr(trace_export_module, "BatchSpanProcessor", Processor)
    monkeypatch.setattr(
        trace_exporter_module,
        "OTLPSpanExporter",
        lambda **_kwargs: exporter,
    )

    config = SimpleNamespace(
        endpoint="http://collector:4318/v1/traces",
        timeout_millis=1000,
        schedule_delay_millis=1000,
        max_queue_size=16,
        max_export_batch_size=4,
        processor_timeout_millis=1000,
    )
    reporter = SimpleNamespace(report=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="processor registration failed"):
        build_tracing_runtime(
            config,
            resource=Resource.create({"service.name": "azurpilot"}),
            reporter=reporter,
        )

    assert provider is not None
    processor = processor_instances[0] if processor_instances else None
    assert processor is not None
    assert provider.shutdown_calls == 1
    assert processor.shutdown_calls == 1
    assert exporter.shutdown_calls == 1


def test_trace_root_is_once_at_scheduler_boundary_and_ignores_fake_roots(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-root")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = "Профиль с пробелом"
        script.__dict__["config"] = SimpleNamespace(
            task=_task(),
            args={"Research": {"Scheduler": {"Command": {"value": "Research"}}}},
        )
        script.__dict__["device"] = SimpleNamespace(screenshot=lambda: None)
        script.__dict__["research"] = lambda: True
        script.__dict__["goto_main"] = lambda: True

        assert script._run_scheduler_task("Research") is True
        with trace_operation("azurpilot.ui.wait", attributes={"phase": "main"}):
            pass
        assert script.run("goto_main", skip_first_screenshot=True) is True

        shutdown_application_observability(target, timeout_millis=3000)
        spans = exporter.get_finished_spans()
        roots = [span for span in spans if span.name == "azurpilot.task.run"]
        children = [span for span in spans if span.name == "azurpilot.ui.wait"]
        assert len(roots) == 1
        assert len(children) == 0
        root = roots[0]
        assert root.attributes["azurpilot.profile"] == "Профиль с пробелом"
        assert root.attributes["azurpilot.task"] == "Research"
        assert root.attributes["azurpilot.task.outcome"] == "success"
        assert root.status.status_code.name == "OK"
        assert all(span.name != "GotoMain" for span in spans)
    finally:
        shutdown_application_observability(target)


def test_trace_operation_is_nested_under_active_root(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-child")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task, trace_operation("azurpilot.ui.wait", attributes={"phase": "main"}):
            task.finish(True)
        shutdown_application_observability(target, timeout_millis=3000)
        spans = exporter.get_finished_spans()
        root = next(span for span in spans if span.name == "azurpilot.task.run")
        child = next(span for span in spans if span.name == "azurpilot.ui.wait")
        assert child.parent is not None
        assert child.parent.span_id == root.context.span_id
        assert child.attributes["phase"] == "main"
    finally:
        shutdown_application_observability(target)


def test_active_span_context_is_preserved_in_exported_log_record(monkeypatch):
    _enable_traces(monkeypatch, logs=True)
    target = _new_logger("observability-trace-log-context")
    log_exporter = InMemoryLogRecordExporter()
    trace_exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: log_exporter,
            _traces_exporter_factory=lambda _timeout: trace_exporter,
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            target.info("message remains free of trace identifiers")
            task.finish(True)
        shutdown_application_observability(target, timeout_millis=3000)
        span = next(
            span
            for span in trace_exporter.get_finished_spans()
            if span.name == "azurpilot.task.run"
        )
        record = log_exporter.get_finished_logs()[0].log_record
        assert record.trace_id == span.context.trace_id
        assert record.span_id == span.context.span_id
        assert "trace_id=" not in str(record.body)
        assert "span_id=" not in str(record.body)
    finally:
        shutdown_application_observability(target)


def test_task_end_is_stopped_and_exception_trace_is_sanitized(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-outcomes")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = "profile-a"
        script.__dict__["config"] = SimpleNamespace(
            task=_task(),
            args={"Research": {"Scheduler": {"Command": {"value": "Research"}}}},
        )
        script.__dict__["device"] = SimpleNamespace(screenshot=lambda: None)

        def stop_task():
            raise TaskEnd("normal stop")

        script.__dict__["research"] = stop_task
        assert script._run_scheduler_task("Research") is True

        with pytest.raises(RuntimeError, match="password"), scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ):
            raise RuntimeError(
                "password=raw-secret path=C:\\Users\\KykLa\\private\\config.json"
            )

        shutdown_application_observability(target, timeout_millis=3000)
        spans = exporter.get_finished_spans()
        stopped = [
            span
            for span in spans
            if span.attributes.get("azurpilot.task.outcome") == "stopped"
        ]
        failed = [
            span
            for span in spans
            if span.attributes.get("azurpilot.task.outcome") == "failure"
        ]
        assert len(stopped) == 1
        assert stopped[0].status.status_code.name != "ERROR"
        assert len(failed) == 1
        assert failed[0].status.status_code.name == "ERROR"
        event_text = str(failed[0].events)
        assert "raw-secret" not in event_text
        assert "C:\\Users\\KykLa\\private" not in event_text
    finally:
        shutdown_application_observability(target)


def test_direct_task_end_keeps_trace_status_non_error(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-direct-task-end")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        with pytest.raises(TaskEnd), scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ):
            raise TaskEnd("normal stop")
        assert shutdown_application_observability(target, timeout_millis=3000)
        span = next(
            span
            for span in exporter.get_finished_spans()
            if span.name == "azurpilot.task.run"
        )
        assert span.attributes["azurpilot.task.outcome"] == "stopped"
        assert span.status.status_code.name != "ERROR"
        assert not span.events
    finally:
        shutdown_application_observability(target)


def test_trace_runtime_is_idempotent_and_fail_open(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-idempotent")
    exporter = InMemorySpanExporter()
    factory_calls = []

    def exporter_factory(_timeout):
        factory_calls.append(True)
        return exporter

    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=exporter_factory,
        )
        first_runtime = _runtimes[id(target)].traces
        assert configure_application_observability(
            target,
            _traces_exporter_factory=exporter_factory,
        )
        assert _runtimes[id(target)].traces is first_runtime
        assert factory_calls == [True]
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            task.finish(True)
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert len(exporter.get_finished_spans()) == 1
    finally:
        shutdown_application_observability(target)


def test_trace_failure_does_not_disable_metrics(monkeypatch):
    _enable_traces(monkeypatch, metrics=True)
    target = _new_logger("observability-trace-failure")
    metric_reader = InMemoryMetricReader()

    class FailingExporter:
        def export(self, _spans):
            raise RuntimeError("synthetic trace transport failure")

        def shutdown(self):
            return None

    try:
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=lambda _timeout: object(),
            _metrics_reader_factory=lambda _exporter, _interval, _timeout: (
                metric_reader
            ),
            _traces_exporter_factory=lambda _timeout: FailingExporter(),
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            task.finish(True)
        assert shutdown_application_observability(target, timeout_millis=3000)
        points = (
            metric_reader.get_metrics_data()
            .resource_metrics[0]
            .scope_metrics[0]
            .metrics
        )
        assert any(metric.name == "azurpilot.task.run" for metric in points)
    finally:
        shutdown_application_observability(target)


def test_trace_init_failure_leaves_logs_and_metrics_functional(monkeypatch):
    _enable_traces(monkeypatch, logs=True, metrics=True)
    target = _new_logger("observability-trace-init-failure")
    log_exporter = InMemoryLogRecordExporter()
    metric_reader = InMemoryMetricReader()

    def failing_trace_factory(_timeout):
        raise RuntimeError("trace init failure")

    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: log_exporter,
            _metrics_exporter_factory=lambda _timeout: object(),
            _metrics_reader_factory=lambda _exporter, _interval, _timeout: (
                metric_reader
            ),
            _traces_exporter_factory=failing_trace_factory,
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            target.info("logs and metrics survive trace init failure")
            task.finish(True)
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert [item.log_record.body for item in log_exporter.get_finished_logs()] == [
            "logs and metrics survive trace init failure"
        ]
        points = (
            metric_reader.get_metrics_data()
            .resource_metrics[0]
            .scope_metrics[0]
            .metrics
        )
        assert any(metric.name == "azurpilot.task.run" for metric in points)
    finally:
        shutdown_application_observability(target)


def test_metrics_record_exemplars_from_active_root_without_trace_labels(monkeypatch):
    _enable_traces(monkeypatch, metrics=True)
    monkeypatch.setenv("OTEL_METRICS_EXEMPLAR_FILTER", "trace_based")
    target = _new_logger("observability-trace-exemplar")
    metric_reader = InMemoryMetricReader()
    trace_exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=lambda _timeout: object(),
            _metrics_reader_factory=lambda _exporter, _interval, _timeout: (
                metric_reader
            ),
            _traces_exporter_factory=lambda _timeout: trace_exporter,
        )
        with scheduler_task_run(
            profile="Профиль А",
            task=_task(),
            registry=("Research",),
        ) as task:
            task.finish(True)
        metrics_data = metric_reader.get_metrics_data()
        shutdown_application_observability(target, timeout_millis=3000)
        root = next(
            span
            for span in trace_exporter.get_finished_spans()
            if span.name == "azurpilot.task.run"
        )
        metrics = [
            metric
            for resource_metrics in metrics_data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        ]
        points = [
            point
            for metric in metrics
            if metric.name == "azurpilot.task.run"
            for point in metric.data.data_points
        ]
        assert len(points) == 1
        assert "trace_id" not in points[0].attributes
        assert "span_id" not in points[0].attributes
        if not points[0].exemplars:
            pytest.skip("текущий SDK или metrics reader не экспортирует exemplars")
        assert points[0].exemplars[0].trace_id == root.context.trace_id
        assert points[0].exemplars[0].span_id == root.context.span_id
    finally:
        shutdown_application_observability(target)


def test_nested_child_restores_parent_context_and_bounds_attributes(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-context")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            root_span = get_current_span()
            root_span_id = root_span.get_span_context().span_id
            with trace_operation(
                "azurpilot.ui.wait",
                attributes={
                    "phase": "password=raw-secret",
                    "oversized": "v" * 1000,
                },
            ) as child:
                assert child is not None
                assert (
                    get_current_span().get_span_context().span_id
                    == child.context.span_id
                )
                assert child.parent is not None
                assert child.parent.span_id == root_span_id
                assert child.attributes["phase"] == "password=***"
                assert len(child.attributes["oversized"]) <= 256
            assert get_current_span().get_span_context().span_id == root_span_id
            with trace_operation("x" * 65) as oversized_name:
                assert oversized_name is None
            task.finish(True)
        assert not get_current_span().get_span_context().is_valid
    finally:
        shutdown_application_observability(target)


def test_screenshot_spans_leave_budget_for_other_operations(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-screenshot-budget")
    exporter = InMemorySpanExporter()
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            for _ in range(64):
                with trace_operation("azurpilot.device.screenshot") as screenshot:
                    assert screenshot is not None
            with trace_operation("azurpilot.device.screenshot") as extra_screenshot:
                assert extra_screenshot is None
            with trace_operation("azurpilot.ocr.process") as ocr:
                assert ocr is not None
            task.finish(True)
        shutdown_application_observability(target, timeout_millis=3000)
        spans = exporter.get_finished_spans()
        assert sum(span.name == "azurpilot.device.screenshot" for span in spans) == 64
        assert sum(span.name == "azurpilot.ocr.process" for span in spans) == 1
    finally:
        shutdown_application_observability(target)


def test_concurrent_task_contexts_are_isolated(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-concurrency")
    exporter = InMemorySpanExporter()
    barrier = threading.Barrier(2)
    profiles = ("profile-one", "profile-two")
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: exporter,
        )

        def run_task(profile):
            with scheduler_task_run(
                profile=profile,
                task=_task(),
                registry=("Research",),
            ) as task:
                root_span_id = get_current_span().get_span_context().span_id
                barrier.wait(timeout=5)
                with trace_operation("azurpilot.ui.wait") as child:
                    assert child is not None
                    assert child.parent.span_id == root_span_id
                    assert (
                        get_current_span().get_span_context().span_id
                        == child.context.span_id
                    )
                assert get_current_span().get_span_context().span_id == root_span_id
                task.finish(True)
                return root_span_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            root_ids = tuple(executor.map(run_task, profiles))
        assert len(set(root_ids)) == 2
        assert shutdown_application_observability(target, timeout_millis=3000)
        roots = {
            span.attributes["azurpilot.profile"]
            for span in exporter.get_finished_spans()
            if span.name == "azurpilot.task.run"
        }
        assert roots == set(profiles)
    finally:
        shutdown_application_observability(target)


def test_trace_runtime_is_discarded_after_fork_boundary(monkeypatch):
    _enable_traces(monkeypatch)
    target = _new_logger("observability-trace-fork")
    parent_exporter = InMemorySpanExporter()
    child_exporter = InMemorySpanExporter()
    parent_runtime = None
    try:
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: parent_exporter,
        )
        parent_runtime = _runtimes[id(target)].traces
        assert parent_runtime is not None
        _after_fork()
        assert parent_runtime.active is False
        assert get_active_tracing_runtime() is None
        assert configure_application_observability(
            target,
            _traces_exporter_factory=lambda _timeout: child_exporter,
        )
        child_runtime = _runtimes[id(target)].traces
        assert child_runtime is not None
        assert child_runtime is not parent_runtime
        with scheduler_task_run(
            profile="profile-a",
            task=_task(),
            registry=("Research",),
        ) as task:
            task.finish(True)
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert len(parent_exporter.get_finished_spans()) == 0
        assert len(child_exporter.get_finished_spans()) == 1
    finally:
        shutdown_application_observability(target)
        if parent_runtime is not None:
            parent_runtime.shutdown(1000)
