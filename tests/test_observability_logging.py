import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from module.logging_context import (
    get_logging_context,
    get_task_context,
    logging_context,
    task_context,
    task_logging_context,
)
from module.observability.bootstrap import (
    _read_config,
    _Runtime,
    _SanitizedOTelHandler,
    _shutdown_runtime,
    configure_application_observability,
    shutdown_application_observability,
)

_ROOT = Path(__file__).resolve().parents[1]
_OTEL_ENVIRONMENT_KEYS = (
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SDK_DISABLED",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_PYTHON_LOG_HANDLER_LEVEL",
    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_BLRP_SCHEDULE_DELAY",
    "OTEL_BLRP_MAX_QUEUE_SIZE",
    "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
    "OTEL_BLRP_EXPORT_TIMEOUT",
)


def _configure_test_environment(monkeypatch):
    for key in _OTEL_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "http://127.0.0.1:4318/v1/logs",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment.name=test",
    )
    monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "10000")


def _new_logger(name: str) -> logging.Logger:
    target = logging.getLogger(name)
    for handler in list(target.handlers):
        target.removeHandler(handler)
        handler.close()
    target.filters.clear()
    target.setLevel(logging.DEBUG)
    target.propagate = False
    return target


def _observability_handlers(target: logging.Logger):
    return [
        handler
        for handler in target.handlers
        if getattr(handler, "_azurpilot_observability_handler", False)
    ]


def test_application_logging_is_disabled_without_explicit_endpoint(monkeypatch):
    for key in _OTEL_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    target = _new_logger("observability-disabled")
    try:
        assert not configure_application_observability(target)
        assert _observability_handlers(target) == []
    finally:
        shutdown_application_observability(target)


def test_application_logging_disabled_flag_wins_over_endpoint(monkeypatch):
    _configure_test_environment(monkeypatch)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    target = _new_logger("observability-disabled-flag")
    try:
        assert not configure_application_observability(target)
        assert _observability_handlers(target) == []
    finally:
        shutdown_application_observability(target)


def test_generic_otlp_endpoint_uses_standard_logs_path_and_timeout_fallback(
    monkeypatch,
):
    for key in _OTEL_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2000")

    config = _read_config()
    assert config is not None
    assert config.signal_endpoint is None
    assert config.timeout_millis == 2000


def test_handler_level_is_configurable(monkeypatch):
    _configure_test_environment(monkeypatch)
    monkeypatch.setenv("OTEL_PYTHON_LOG_HANDLER_LEVEL", "WARNING")
    target = _new_logger("observability-level")
    exporter = InMemoryLogRecordExporter()
    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: exporter,
        )
        handler = _observability_handlers(target)[0]
        assert handler.level == logging.WARNING
    finally:
        shutdown_application_observability(target)


def test_application_logging_is_idempotent_and_preserves_context_and_redaction(
    monkeypatch,
):
    _configure_test_environment(monkeypatch)
    target = _new_logger("observability-records")
    exporter = InMemoryLogRecordExporter()

    class SecretObject:
        def __str__(self):
            return "raw-secret-from-object"

    local_records = []
    local_handler = logging.Handler()
    local_handler.emit = local_records.append
    target.addHandler(local_handler)
    try:
        assert configure_application_observability(
            target,
            default_profile="default-profile",
            _exporter_factory=lambda _timeout: exporter,
        )
        assert configure_application_observability(
            target,
            default_profile="default-profile",
            _exporter_factory=lambda _timeout: exporter,
        )
        assert len(_observability_handlers(target)) == 1

        with logging_context(
            profile="profile-a",
            component="component-a",
            run_id="run-a",
        ):
            with task_context("ObservationTask"):
                target.warning(
                    "token=raw-token credential=raw-credential cookie=raw-cookie "
                    "session=raw-session private_key=raw-private-key "
                    "url=https://user:password@example.test/path?secret=raw-secret value=%s",
                    SecretObject(),
                    extra={"raw_secret": "raw-attribute"},
                )
                try:
                    raise ValueError("password=raw-exception")
                except ValueError:
                    target.exception(
                        "[bold]exception body[/bold]", extra={"markup": True}
                    )

        assert shutdown_application_observability(target, timeout_millis=3000)
        records = exporter.get_finished_logs()
        assert len(records) == 2

        warning = records[0].log_record
        exception = records[1].log_record
        assert warning.severity_text == "WARN"
        assert "raw-token" not in str(warning.body)
        assert "raw-credential" not in str(warning.body)
        assert "raw-cookie" not in str(warning.body)
        assert "raw-session" not in str(warning.body)
        assert "raw-private-key" not in str(warning.body)
        assert "raw-secret" not in str(warning.body)
        assert "raw-secret-from-object" not in str(warning.body)
        assert "https://***@example.test/path?secret=***" in warning.body
        assert warning.attributes["azurpilot.profile"] == "profile-a"
        assert warning.attributes["azurpilot.task"] == "ObservationTask"
        assert warning.attributes["azurpilot.component"] == "component-a"
        assert warning.attributes["azurpilot.run.id"] == "run-a"
        assert isinstance(warning.attributes["process.pid"], int)
        assert "pathname" not in warning.attributes
        assert "raw_secret" not in warning.attributes
        assert local_records[0].raw_secret == "raw-attribute"

        assert exception.severity_text == "ERROR"
        assert exception.body == "exception body"
        assert exception.attributes["exception.type"] == "ValueError"
        assert "raw-exception" not in exception.attributes["exception.message"]
        assert "raw-exception" not in exception.attributes["exception.stacktrace"]

        resource = records[0].resource.attributes
        assert resource["service.name"] == "azurpilot"
        assert resource["deployment.environment.name"] == "test"
    finally:
        target.removeHandler(local_handler)
        local_handler.close()
        shutdown_application_observability(target)


def test_logging_context_restores_nested_values_and_isolates_async_tasks():
    assert get_logging_context().profile is None
    assert get_task_context() is None

    with logging_context(profile="outer", component="outer-component"):
        with task_context("OuterTask"):
            assert get_logging_context().profile == "outer"
            assert get_task_context() == "OuterTask"
            with logging_context(component="inner", run_id="run"):
                with task_context("InnerTask"):
                    assert get_logging_context().component == "inner"
                    assert get_logging_context().run_id == "run"
                    assert get_task_context() == "InnerTask"
            assert get_logging_context().component == "outer-component"
            assert get_logging_context().run_id is None
            assert get_task_context() == "OuterTask"

    async def read_context(profile):
        with logging_context(profile=profile):
            with task_context(f"Task-{profile}"):
                await asyncio.sleep(0)
                return get_logging_context().profile, get_task_context()

    async def read_both():
        return await asyncio.gather(read_context("one"), read_context("two"))

    assert asyncio.run(read_both()) == [
        ("one", "Task-one"),
        ("two", "Task-two"),
    ]
    assert get_logging_context().profile is None
    assert get_task_context() is None


def test_task_boundary_adds_profile_without_replacing_canonical_task_context():
    class Probe:
        config_name = "profile-from-boundary"

        @task_logging_context
        def run(self, command):
            return get_logging_context().profile, get_task_context()

    probe = Probe()
    assert probe.run("research") == ("profile-from-boundary", "Research")
    assert get_logging_context().profile is None
    assert get_task_context() is None


def test_importing_logger_does_not_create_file_handler_or_remote_bootstrap():
    code = """
import module.logger as logger_module
print(logger_module.logger.log_file)
print(sum(isinstance(handler, logger_module.RichTimedRotatingHandler) for handler in logger_module.logger.handlers))
print(any(name.startswith("opentelemetry") for name in __import__("sys").modules))
"""
    environment = os.environ.copy()
    for key in _OTEL_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["OTEL_SDK_DISABLED"] = "true"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-3:] == ["None", "0", "False"]


def test_exporter_failure_is_fail_open_for_local_logger(monkeypatch):
    _configure_test_environment(monkeypatch)
    target = _new_logger("observability-failure")

    class FailingExporter:
        def export(self, _records):
            raise RuntimeError("искусственный сбой транспорта")

        def shutdown(self):
            return None

    records = []
    target.addHandler(logging.Handler())
    target.handlers[0].emit = records.append
    try:
        assert configure_application_observability(
            target,
            _exporter_factory=lambda _timeout: FailingExporter(),
        )
        target.error("локальная запись сохраняется")
        assert shutdown_application_observability(target, timeout_millis=3000)
        assert records
    finally:
        shutdown_application_observability(target)


def test_shutdown_is_bounded_even_when_provider_blocks():
    class SlowProvider:
        def force_flush(self, timeout_millis):
            time.sleep(0.2)
            return False

        def shutdown(self):
            time.sleep(0.2)

    target = _new_logger("observability-timeout")
    handler = _SanitizedOTelHandler(
        logging.Handler(),
        SlowProvider(),
        reporter=type("Reporter", (), {"report": lambda *_args, **_kwargs: None})(),
    )
    runtime = _Runtime(target=target, provider=SlowProvider(), handler=handler)
    started = time.monotonic()
    assert not _shutdown_runtime(runtime, timeout_millis=25)
    assert time.monotonic() - started < 0.35
