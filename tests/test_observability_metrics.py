"""Регрессии bounded application metrics и их lifecycle-контракта."""

import logging
import json
import os
import sys
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from alas import AzurLaneAutoScript
from module.config.config import Function, TaskEnd
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.resources import Resource

from module.logging_context import CONTEXT_VALUE_LIMIT, task_logging_context
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
    scheduler_task_run,
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
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
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


def _metric_by_name(reader: InMemoryMetricReader, name: str):
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return metric
    raise AssertionError(f"metric {name!r} was not collected")


def _metric_points(reader: InMemoryMetricReader, name: str):
    try:
        data = reader.get_metrics_data()
        if data is None:
            return []
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name == name:
                        return list(metric.data.data_points)
        return []
    except AssertionError:
        return []


def _configured_task(command: str) -> Function:
    return Function({"Scheduler": {"Command": command}})


def _scheduler_args(*commands: str) -> dict[str, object]:
    return {
        command: {"Scheduler": {"Command": {"value": command}}}
        for command in commands
    }


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
        assert _metric_points(reader, "azurpilot.task.run") == []

        registry = ("Research", "Outer", "Inner")
        with scheduler_task_run(
            profile="profile_a",
            task=_configured_task("Outer"),
            registry=registry,
        ) as outer:
            with scheduler_task_run(
                profile="profile_a",
                task=_configured_task("Inner"),
                registry=registry,
            ) as inner:
                inner.finish(False)
            outer.finish("recoverable")

        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = "profile_a"
        script.__dict__["config"] = SimpleNamespace(
            task=_configured_task("Research"),
            args=_scheduler_args("Research"),
        )
        script.__dict__["device"] = SimpleNamespace(screenshot=lambda: None)

        script.__dict__["goto_main"] = lambda: None
        assert script.run("goto_main", skip_first_screenshot=True) is True
        assert all(
            point.attributes["azurpilot.task"] != "GotoMain"
            for point in _metric_points(reader, "azurpilot.task.run")
        )

        script.__dict__["research"] = lambda: None
        assert script._run_scheduler_task("Research") is True

        def stop_task():
            raise TaskEnd("normal task stop")

        script.__dict__["research"] = stop_task
        assert script._run_scheduler_task("Research") is True

        with scheduler_task_run(
            profile="unsafe/profile",
            task=_configured_task("NonexistentTask123"),
            registry=("Research",),
        ) as unknown_run:
            unknown_run.finish(None)

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
                ("azurpilot.task", "Outer"),
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
        assert "NonexistentTask123" not in {
            point.attributes["azurpilot.task"]
            for point in counter.data.data_points
        }
        assert all(point.count == 1 for point in histogram.data.data_points)
        assert all(point.sum > 0 for point in histogram.data.data_points)
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_task_boundary_records_failure_for_exception():
    runtime, reader = _build_in_memory_runtime()
    try:
        with pytest.raises(RuntimeError, match="synthetic"):
            with scheduler_task_run(
                profile="profile_a",
                task=_configured_task("Research"),
                registry=("Research",),
            ):
                raise RuntimeError("synthetic")

        counter = _metric_by_name(reader, "azurpilot.task.run")
        point = counter.data.data_points[0]
        assert point.attributes["azurpilot.task.outcome"] == "failure"
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_profile_metric_identity_uses_project_contract_and_keeps_unicode_distinct():
    runtime, reader = _build_in_memory_runtime()
    profiles = (
        "profile-a",
        "Профиль А",
        "Профиль с пробелом",
        "p" * CONTEXT_VALUE_LIMIT,
    )
    try:
        for profile in profiles:
            with scheduler_task_run(
                profile=profile,
                task=_configured_task("Research"),
                registry=("Research",),
            ) as task:
                task.finish(True)
        with scheduler_task_run(
            profile="p" * (CONTEXT_VALUE_LIMIT + 1),
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(True)
        with scheduler_task_run(
            profile="invalid/profile",
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(True)

        points = _metric_by_name(reader, "azurpilot.task.run").data.data_points
        profile_counts = Counter(point.attributes["azurpilot.profile"] for point in points)
        assert set(profiles) <= set(profile_counts)
        assert all(profile_counts[profile] == 1 for profile in profiles)
        unknown_points = [
            point for point in points if point.attributes["azurpilot.profile"] == "unknown"
        ]
        assert len(unknown_points) == 1
        assert unknown_points[0].value == 2
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (True, "success"),
        ("recoverable", "recoverable"),
        (False, "failure"),
    ],
)
def test_task_boundary_maps_terminal_results(result, expected):
    runtime, reader = _build_in_memory_runtime()
    try:
        with scheduler_task_run(
            profile="profile_a",
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(result)

        counter = _metric_by_name(reader, "azurpilot.task.run")
        assert counter.data.data_points[0].attributes["azurpilot.task.outcome"] == expected
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (SystemExit(None), "stopped"),
        (SystemExit(0), "stopped"),
        (SystemExit(1), "failure"),
        (RuntimeError("synthetic"), "failure"),
    ],
)
def test_task_boundary_maps_exception_outcomes(exception, expected):
    runtime, reader = _build_in_memory_runtime()
    try:
        with pytest.raises(type(exception)):
            with scheduler_task_run(
                profile="profile_a",
                task=_configured_task("Research"),
                registry=("Research",),
            ):
                raise exception

        counter = _metric_by_name(reader, "azurpilot.task.run")
        assert counter.data.data_points[0].attributes["azurpilot.task.outcome"] == expected
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
            with scheduler_task_run(
                profile="profile_a",
                task=_configured_task("Research"),
                registry=("Research",),
            ):
                raise RuntimeError("task failure")
        assert reports
    finally:
        deactivate_metrics_runtime(runtime)
        assert runtime.shutdown(1000)


def test_metrics_runtime_closes_reader_when_provider_creation_fails(monkeypatch):
    from opentelemetry.sdk import metrics as sdk_metrics

    class Reader:
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    class FailingProvider:
        def __init__(self, **_kwargs):
            raise RuntimeError("provider creation failed")

    reader = Reader()
    monkeypatch.setattr(sdk_metrics, "MeterProvider", FailingProvider)
    with pytest.raises(RuntimeError, match="provider creation failed"):
        build_metrics_runtime(
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
    assert reader.shutdown_calls == 1


def test_metrics_runtime_closes_exporter_once_when_reader_creation_fails():
    class Exporter:
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    exporter = Exporter()

    def reader_factory(_exporter, _interval, _timeout):
        raise RuntimeError("reader creation failed")

    with pytest.raises(RuntimeError, match="reader creation failed"):
        build_metrics_runtime(
            MetricsConfig(
                endpoint=None,
                timeout_millis=1000,
                export_interval_millis=60_000,
                export_timeout_millis=30_000,
            ),
            resource=Resource.create({"service.name": "azurpilot"}),
            reporter=type("Reporter", (), {"report": lambda *_args, **_kwargs: None})(),
            exporter_factory=lambda _timeout: exporter,
            reader_factory=reader_factory,
        )
    assert exporter.shutdown_calls == 1


def test_metrics_reader_failure_keeps_application_logs_working(monkeypatch):
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
    target = _new_logger("observability-reader-failure")
    log_exporter = InMemoryLogRecordExporter()

    class Exporter:
        def __init__(self):
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1

    metric_exporter = Exporter()
    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: log_exporter,
            _metrics_exporter_factory=lambda _timeout: metric_exporter,
            _metrics_reader_factory=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("reader creation failed")
            ),
        )
        target.info("logs remain available")
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert metric_exporter.shutdown_calls == 1
        assert [item.log_record.body for item in log_exporter.get_finished_logs()] == [
            "logs remain available"
        ]
    finally:
        shutdown_application_observability(target)


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
        with scheduler_task_run(
            profile="profile_a",
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(True)
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
        with scheduler_task_run(
            profile="profile_a",
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(True)
        assert _metric_by_name(child_reader, "azurpilot.task.run").data.data_points[0].value == 1
    finally:
        shutdown_application_observability(target, timeout_millis=1000)
        if "parent_runtime" in locals():
            assert parent_runtime.shutdown(1000)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux fork lifecycle contract")
def test_linux_fork_uses_fresh_production_periodic_reader(monkeypatch):
    from opentelemetry.sdk.metrics.export import MetricExportResult, MetricExporter

    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "http://collector:4318/v1/metrics",
    )
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "10")
    target = _new_logger("observability-metrics-production-fork")

    class RecordingExporter(MetricExporter):
        def __init__(self):
            super().__init__()
            self.export_pids = []
            self.force_flush_pids = []
            self.shutdown_pids = []

        def export(self, _metrics_data, timeout_millis=10_000, **_kwargs):
            del timeout_millis
            self.export_pids.append(os.getpid())
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10_000):
            del timeout_millis
            self.force_flush_pids.append(os.getpid())
            return True

        def shutdown(self, timeout_millis=30_000, **_kwargs):
            del timeout_millis
            self.shutdown_pids.append(os.getpid())

    parent_pid = os.getpid()
    parent_exporter = RecordingExporter()
    read_fd, write_fd = os.pipe()

    try:
        assert configure_application_observability(
            target,
            _metrics_exporter_factory=lambda _timeout: parent_exporter,
        )
        parent_runtime = _runtimes[id(target)].metrics
        assert parent_runtime is not None
        assert parent_runtime.provider.force_flush(timeout_millis=3000)
        parent_export_count_before_fork = len(parent_exporter.export_pids)

        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                # Если унаследованный production reader запустит thread после fork,
                # он успеет обратиться к parent_exporter до child bootstrap.
                time.sleep(0.15)
                child_exporter = RecordingExporter()
                child_factory_calls = []

                def child_exporter_factory(_timeout):
                    child_factory_calls.append(True)
                    return child_exporter

                configured = configure_application_observability(
                    target,
                    _metrics_exporter_factory=child_exporter_factory,
                )
                child_runtime = _runtimes[id(target)].metrics
                if child_runtime is None:
                    raise AssertionError("child metrics runtime was not bootstrapped")
                with scheduler_task_run(
                    profile="profile_a",
                    task=_configured_task("Research"),
                    registry=("Research",),
                ) as task:
                    task.finish(True)
                shutdown_ok = shutdown_application_observability(
                    target,
                    timeout_millis=3000,
                )
                payload = {
                    "pid": os.getpid(),
                    "configured": configured,
                    "runtime_owner_pid": child_runtime.owner_pid,
                    "factory_calls": len(child_factory_calls),
                    "child_export_pids": child_exporter.export_pids,
                    "child_force_flush_pids": child_exporter.force_flush_pids,
                    "child_shutdown_pids": child_exporter.shutdown_pids,
                    "inherited_parent_export_pids": parent_exporter.export_pids,
                    "shutdown_ok": shutdown_ok,
                }
                os.write(write_fd, json.dumps(payload).encode("utf-8"))
                os._exit(0)
            except BaseException as exc:
                payload = {"error": f"{type(exc).__name__}: {exc}"}
                os.write(write_fd, json.dumps(payload).encode("utf-8"))
                os._exit(1)

        os.close(write_fd)
        chunks = []
        while True:
            chunk = os.read(read_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        _, status = os.waitpid(child_pid, 0)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        assert os.waitstatus_to_exitcode(status) == 0, payload
        assert payload["configured"] is True
        assert payload["runtime_owner_pid"] == payload["pid"]
        assert payload["factory_calls"] == 1
        assert payload["shutdown_ok"] is True
        assert payload["child_export_pids"]
        assert set(payload["child_export_pids"]) == {payload["pid"]}
        assert set(payload["child_force_flush_pids"]) == {payload["pid"]}
        assert set(payload["child_shutdown_pids"]) == {payload["pid"]}
        assert len(payload["inherited_parent_export_pids"]) == parent_export_count_before_fork
        assert set(payload["inherited_parent_export_pids"]) <= {parent_pid}

        assert get_active_metrics_runtime() is parent_runtime
        with scheduler_task_run(
            profile="profile_a",
            task=_configured_task("Research"),
            registry=("Research",),
        ) as task:
            task.finish(True)
        assert parent_runtime.provider.force_flush(timeout_millis=3000)
        assert parent_exporter.export_pids
        assert set(parent_exporter.export_pids) == {parent_pid}
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            shutdown_application_observability(target, timeout_millis=3000)
        except Exception:
            pass


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
