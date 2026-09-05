"""Регрессии bounded application metrics и их lifecycle-контракта."""

import logging

import pytest
from alas import AzurLaneAutoScript
from module.config.config import TaskEnd
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.resources import Resource

from module.logging_context import task_logging_context
from module.observability.bootstrap import (
    _after_fork,
    _runtimes,
    _read_config,
    configure_application_observability,
    shutdown_application_observability,
)
from module.observability.metrics import (
    MetricsConfig,
    activate_metrics_runtime,
    build_metrics_runtime,
    deactivate_metrics_runtime,
    get_active_metrics_runtime,
    record_task_run,
    task_run,
)


_OTEL_ENVIRONMENT_KEYS = (
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_METRICS_EXEMPLAR_FILTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_METRIC_EXPORT_TIMEOUT",
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


def _metric_by_name(reader: InMemoryMetricReader, name: str):
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return metric
    raise AssertionError(f"metric {name!r} was not collected")


def _build_in_memory_runtime():
    reader = InMemoryMetricReader()
    runtime = build_metrics_runtime(
        MetricsConfig(
            endpoint=None,
            timeout_millis=1000,
            export_interval_millis=60_000,
            export_timeout_millis=30_000,
        ),
        resource=Resource.create({"service.name": "azurpilot"}),
        reporter=type("Reporter", (), {"report": lambda *_args, **_kwargs: None})(),
        exporter_factory=lambda _timeout: object(),
        reader_factory=lambda _exporter, _interval, _timeout: reader,
    )
    activate_metrics_runtime(runtime)
    return runtime, reader


def test_read_config_supports_signal_specific_metrics_and_bounded_values(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2500")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "123")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_TIMEOUT", "5000")

    config = _read_config()

    assert config is not None
    assert not config.logs_enabled
    assert config.signal_endpoint is None
    assert config.metrics == MetricsConfig(
        endpoint="http://collector:4318/v1/metrics",
        timeout_millis=2500,
        export_interval_millis=123,
        export_timeout_millis=5000,
    )


def test_metrics_are_disabled_without_endpoint_or_with_sdk_disabled(monkeypatch):
    _clear_environment(monkeypatch)
    assert _read_config() is None

    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert _read_config() is None


def test_metrics_protocol_and_endpoint_precedence_are_fail_open(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://generic:4318")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://specific:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "http/protobuf")

    config = _read_config()
    assert config is not None
    assert config.metrics is not None
    assert config.metrics.endpoint == "http://specific:4318/v1/metrics"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_PROTOCOL", "grpc")
    config = _read_config()
    assert config is not None
    assert config.logs_enabled
    assert config.metrics is None


def test_metrics_invalid_interval_and_timeout_use_bounded_defaults(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "not-a-number")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_TIMEOUT", "0")

    config = _read_config()

    assert config is not None
    assert config.metrics is not None
    assert config.metrics.export_interval_millis == 60_000
    assert config.metrics.export_timeout_millis == 30_000


def test_signal_specific_metric_exporter_endpoint_is_not_extended_twice():
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    exporter = OTLPMetricExporter(
        endpoint="http://collector:4318/v1/metrics",
        timeout=2,
    )
    try:
        assert exporter._endpoint == "http://collector:4318/v1/metrics"
    finally:
        exporter.shutdown()


def test_metrics_temporality_policy_fails_closed_without_disabling_logs(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "http://collector:4318/v1/logs",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta")

    config = _read_config()

    assert config is not None
    assert config.logs_enabled
    assert config.metrics is None


def test_task_boundary_records_one_cumulative_counter_and_duration_histogram():
    runtime, reader = _build_in_memory_runtime()
    try:
        class Probe:
            config_name = "profile_a"

            @task_logging_context
            def run(self, command):
                return True

        assert Probe().run("research") is True

        with task_run(profile="profile_a", task="outer") as outer:
            with task_run(profile="profile_a", task="inner") as inner:
                inner.finish(False)
            outer.finish("recoverable")

        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = "profile_a"

        def stop_task():
            raise TaskEnd("normal task stop")

        script.__dict__["research"] = stop_task
        assert script.run("research", skip_first_screenshot=True) is True

        record_task_run(
            profile="unsafe profile",
            task="unsafe/task",
            outcome="not-allowed",
            duration_seconds=0,
        )

        counter = _metric_by_name(reader, "azurpilot.task.run")
        histogram = _metric_by_name(reader, "azurpilot.task.duration")
        assert counter.unit == "{run}"
        assert histogram.unit == "s"
        assert counter.data.aggregation_temporality == AggregationTemporality.CUMULATIVE
        assert histogram.data.aggregation_temporality == AggregationTemporality.CUMULATIVE

        counter_points = {
            tuple(sorted(point.attributes.items())): point.value
            for point in counter.data.data_points
        }
        assert counter_points[
            (
                ("azurpilot.profile", "profile_a"),
                ("azurpilot.task", "Research"),
                ("azurpilot.task.outcome", "success"),
            )
        ] == 1
        assert counter_points[
            (
                ("azurpilot.profile", "profile_a"),
                ("azurpilot.task", "outer"),
                ("azurpilot.task.outcome", "recoverable"),
            )
        ] == 1
        assert counter_points[
            (
                ("azurpilot.profile", "profile_a"),
                ("azurpilot.task", "Research"),
                ("azurpilot.task.outcome", "stopped"),
            )
        ] == 1
        assert counter_points[
            (
                ("azurpilot.profile", "unknown"),
                ("azurpilot.task", "unknown"),
                ("azurpilot.task.outcome", "unknown"),
            )
        ] == 1
        assert all(point.count == 1 for point in histogram.data.data_points)
        assert all(point.sum > 0 for point in histogram.data.data_points)
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_task_boundary_records_failure_for_exception():
    runtime, reader = _build_in_memory_runtime()
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            with task_run(profile="profile_a", task="research"):
                raise RuntimeError("synthetic")

        counter = _metric_by_name(reader, "azurpilot.task.run")
        point = counter.data.data_points[0]
        assert point.attributes["azurpilot.task.outcome"] == "failure"
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_metrics_failure_does_not_mask_task_exception():
    class ExplodingInstrument:
        def add(self, *_args, **_kwargs):
            raise RuntimeError("counter exporter failed")

        def record(self, *_args, **_kwargs):
            raise RuntimeError("histogram exporter failed")

    reports = []
    runtime, _reader = _build_in_memory_runtime()
    runtime.task_run_counter = ExplodingInstrument()
    runtime.task_duration_histogram = ExplodingInstrument()
    runtime.reporter = type(
        "Reporter",
        (),
        {"report": lambda _self, message, exc=None: reports.append((message, exc))},
    )()
    try:
        with pytest.raises(RuntimeError, match="task failure"):
            with task_run(profile="profile_a", task="research"):
                raise RuntimeError("task failure")
        assert reports
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_metrics_provider_is_process_local_and_configure_is_idempotent(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    target = _new_logger("observability-metrics-idempotent")
    reader = InMemoryMetricReader()
    exporter_calls = []
    reader_calls = []

    def exporter_factory(timeout):
        exporter_calls.append(timeout)
        return object()

    def reader_factory(exporter, interval, timeout):
        reader_calls.append((exporter, interval, timeout))
        return reader

    try:
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=exporter_factory,
            _metrics_reader_factory=reader_factory,
        )
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=exporter_factory,
            _metrics_reader_factory=reader_factory,
        )
        assert exporter_calls == [1000]
        assert len(reader_calls) == 1
        assert [
            handler
            for handler in target.handlers
            if getattr(handler, "_azurpilot_observability_handler", False)
        ] == []
        record_task_run(
            profile="profile_a",
            task="research",
            outcome=True,
            duration_seconds=0.1,
        )
        assert _metric_by_name(reader, "azurpilot.task.run").data.data_points[0].value == 1
    finally:
        assert shutdown_application_observability(target, timeout_millis=1000)


def test_after_fork_discards_inherited_metrics_runtime_before_child_bootstrap(monkeypatch):
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    target = _new_logger("observability-metrics-fork")
    parent_reader = InMemoryMetricReader()
    child_reader = InMemoryMetricReader()
    readers = iter((parent_reader, child_reader))

    try:
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=lambda _timeout: object(),
            _metrics_reader_factory=lambda _exporter, _interval, _timeout: next(readers),
        )
        parent_runtime = _runtimes[id(target)].metrics
        assert parent_runtime is not None

        _after_fork()

        assert id(target) not in _runtimes
        assert get_active_metrics_runtime() is None
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=lambda _timeout: object(),
            _metrics_reader_factory=lambda _exporter, _interval, _timeout: next(readers),
        )
        child_runtime = _runtimes[id(target)].metrics
        assert child_runtime is not None
        assert child_runtime is not parent_runtime
        record_task_run(
            profile="profile_a",
            task="research",
            outcome=True,
            duration_seconds=0.1,
        )
        assert _metric_by_name(child_reader, "azurpilot.task.run").data.data_points[0].value == 1
    finally:
        shutdown_application_observability(target, timeout_millis=1000)
        if "parent_runtime" in locals():
            assert parent_runtime.shutdown(1000)


def test_metrics_initialization_failure_does_not_disable_logs(monkeypatch):
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "http://collector:4318/v1/logs",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    target = _new_logger("observability-metrics-failure")
    exporter = InMemoryLogRecordExporter()
    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: exporter,
            _metrics_exporter_factory=lambda _timeout: (_ for _ in ()).throw(
                RuntimeError("synthetic metrics failure")
            ),
        )
        target.info("logs remain enabled")
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert [item.log_record.body for item in exporter.get_finished_logs()] == [
            "logs remain enabled"
        ]
    finally:
        shutdown_application_observability(target)
