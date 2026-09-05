"""Явный fail-open bootstrap application logging через OTLP.

OTel Logs API остаётся изолированным в этом модуле. Центральный AzurPilot
logger передаёт сюда обычные ``LogRecord`` без изменений существующих call
sites, а локальные console/WebUI/file handlers продолжают работать отдельно.
"""

from __future__ import annotations

import atexit
import copy
import importlib.metadata
import logging
import os
import sys
import threading
import time
import traceback
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from module.logging_context import get_logging_context, get_task_context
from module.logging_core import (
    REMOTE_LOG_TEXT_LIMIT,
    sanitize_log_text,
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SUPPORTED_PROTOCOL = "http/protobuf"
_DEFAULT_ENVIRONMENT = "local"
_DEFAULT_HANDLER_LEVEL = logging.INFO
_DEFAULT_EXPORT_TIMEOUT_MILLIS = 1_000
_MAX_EXPORT_TIMEOUT_MILLIS = 5_000
_DEFAULT_SCHEDULE_DELAY_MILLIS = 1_000
_MAX_SCHEDULE_DELAY_MILLIS = 10_000
_DEFAULT_MAX_QUEUE_SIZE = 512
_MAX_QUEUE_SIZE = 2_048
_DEFAULT_MAX_EXPORT_BATCH_SIZE = 128
_MAX_EXPORT_BATCH_SIZE = 512
_DEFAULT_PROCESSOR_TIMEOUT_MILLIS = 1_000
_MAX_PROCESSOR_TIMEOUT_MILLIS = 5_000
_REMOTE_ATTRIBUTE_LIMIT = 8 * 1024
_REMOTE_STACKTRACE_LIMIT = 32 * 1024
_SHUTDOWN_TIMEOUT_MILLIS = 1_000
_FAILURE_REPORT_INTERVAL = 60.0
_HANDLER_MARKER = "_azurpilot_observability_handler"
_EXPORTER_INTERNAL = ContextVar("azurpilot_observability_exporter", default=False)
_OTEL_INTERNAL_LOGGERS = (
    "opentelemetry.exporter.otlp",
    "opentelemetry.sdk._logs",
    "opentelemetry.instrumentation.logging",
)

_STANDARD_RECORD_FIELDS = frozenset(
    vars(
        logging.LogRecord(
            name="",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="",
            args=(),
            exc_info=None,
            func="",
            sinfo=None,
        )
    )
) | frozenset({"message", "asctime"})


@dataclass(frozen=True)
class _ObservabilityConfig:
    """Проверенный bounded contract одного OTLP logging exporter."""

    signal_endpoint: str | None
    handler_level: int
    timeout_millis: int
    schedule_delay_millis: int
    max_queue_size: int
    max_export_batch_size: int
    processor_timeout_millis: int


@dataclass
class _Runtime:
    target: logging.Logger
    provider: Any
    handler: "_SanitizedOTelHandler"


class _FailureReporter:
    """Rate-limited stderr diagnostics, не проходящие через AzurPilot logger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_report_at = 0.0

    def report(self, message: str, exc: BaseException | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_report_at < _FAILURE_REPORT_INTERVAL:
                return
            self._last_report_at = now
        suffix = f" ({type(exc).__name__})" if exc is not None else ""
        try:
            sys.stderr.write(f"[AzurPilot] {message}{suffix}.\n")
        except Exception:
            pass


_failure_reporter = _FailureReporter()
_state_lock = threading.RLock()
_runtimes: dict[int, _Runtime] = {}
_atexit_registered = False


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE_VALUES


def _safe_context_value(
    value: object, *, limit: int = _REMOTE_ATTRIBUTE_LIMIT
) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = f"<байтовое значение, размер={len(value)}>"
    elif not isinstance(value, (str, int, float, bool)):
        value = f"<объект {type(value).__name__}>"
    try:
        normalized = sanitize_log_text(value, limit)
    except Exception:
        return None
    return normalized or None


def _safe_message_argument(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return f"<байтовое значение, размер={len(value)}>"
    return f"<объект {type(value).__name__}>"


def _safe_message(record: logging.LogRecord) -> str:
    try:
        if isinstance(record.msg, str):
            safe_record = copy.copy(record)
            args = record.args
            if isinstance(args, tuple):
                safe_record.args = tuple(_safe_message_argument(item) for item in args)
            elif isinstance(args, dict):
                safe_record.args = {
                    _safe_context_value(key, limit=256) or "": _safe_message_argument(
                        value
                    )
                    for key, value in args.items()
                }
            elif args:
                safe_record.args = _safe_message_argument(args)
            text = safe_record.getMessage()
        elif not record.args and isinstance(record.msg, (int, float, bool)):
            text = str(record.msg)
        else:
            text = f"<объект {type(record.msg).__name__}>"
    except Exception:
        text = "<сообщение не удалось безопасно сформировать>"

    if getattr(record, "markup", False):
        try:
            from rich.text import Text

            text = Text.from_markup(text).plain
        except Exception:
            pass
    return sanitize_log_text(text, REMOTE_LOG_TEXT_LIMIT)


def _safe_exception_message(value: BaseException | None) -> str:
    if value is None:
        return ""
    args = getattr(value, "args", ())
    if args:
        first = args[0]
        if isinstance(first, (str, int, float, bool)) or first is None:
            return sanitize_log_text(first, _REMOTE_ATTRIBUTE_LIMIT)
        return f"<объект {type(first).__name__}>"
    return f"<исключение {type(value).__name__}>"


def _exception_attributes(record: logging.LogRecord) -> dict[str, str]:
    if not record.exc_info:
        return {}
    try:
        exception_type, exception_value, traceback_object = record.exc_info
    except TypeError, ValueError:
        return {}

    type_name = (
        _safe_context_value(
            getattr(exception_type, "__name__", "Exception"),
            limit=256,
        )
        or "Exception"
    )
    message = _safe_exception_message(exception_value)
    attributes = {
        "exception.type": type_name,
        "exception.message": message,
    }
    try:
        frames = traceback.extract_tb(traceback_object) if traceback_object else ()
        stacktrace = "".join(traceback.format_list(frames))
        stacktrace += f"{type_name}: {message}"
    except Exception:
        stacktrace = f"{type_name}: {message}"
    attributes["exception.stacktrace"] = sanitize_log_text(
        stacktrace,
        _REMOTE_STACKTRACE_LIMIT,
    )
    return attributes


def _safe_process_command(record: logging.LogRecord) -> str:
    try:
        command = Path(sys.argv[0]).name
    except OSError, RuntimeError, TypeError:
        command = ""
    if not command:
        command = getattr(record, "processName", "")
    return _safe_context_value(command, limit=256) or "unknown"


def _attributes_for_record(
    record: logging.LogRecord,
    default_profile: str | None,
) -> dict[str, object]:
    context = get_logging_context()
    profile = context.profile or default_profile
    attributes: dict[str, object] = {}
    for key, value in (
        ("azurpilot.profile", profile),
        ("azurpilot.task", get_task_context()),
        ("azurpilot.component", context.component or record.name),
        ("azurpilot.run.id", context.run_id),
    ):
        safe_value = _safe_context_value(value)
        if safe_value is not None:
            attributes[key] = safe_value

    if isinstance(record.process, int):
        attributes["process.pid"] = record.process
    attributes["process.command"] = _safe_process_command(record)
    attributes.update(_exception_attributes(record))
    return attributes


def _safe_record(
    record: logging.LogRecord,
    default_profile: str | None,
) -> logging.LogRecord:
    safe_record = copy.copy(record)
    safe_record.msg = _safe_message(record)
    safe_record.args = ()
    safe_record.exc_info = None
    safe_record.exc_text = None
    safe_record.stack_info = None
    for key in list(vars(safe_record)):
        if key not in _STANDARD_RECORD_FIELDS:
            delattr(safe_record, key)
    for key, value in _attributes_for_record(record, default_profile).items():
        setattr(safe_record, key, value)
    return safe_record


class _FailOpenExporter:
    """Изолировать ошибки официального exporter от основного logging-потока."""

    def __init__(self, exporter: Any, reporter: _FailureReporter) -> None:
        self._exporter = exporter
        self._reporter = reporter

    def export(self, records: Any) -> Any:
        token = _EXPORTER_INTERNAL.set(True)
        try:
            result = self._exporter.export(records)
        except Exception as exc:
            self._reporter.report(
                "OTLP exporter недоступен; запись останется только в локальном журнале",
                exc,
            )
            return None
        finally:
            _EXPORTER_INTERNAL.reset(token)
        if getattr(result, "name", "") == "FAILURE":
            self._reporter.report(
                "OTLP exporter отклонил пакет; запись останется только в локальном журнале"
            )
        return result

    def shutdown(self, timeout_millis: int | None = None) -> None:
        try:
            if timeout_millis is None:
                self._exporter.shutdown()
            else:
                try:
                    self._exporter.shutdown(timeout_millis=timeout_millis)
                except TypeError:
                    self._exporter.shutdown()
        except Exception as exc:
            self._reporter.report("Не удалось завершить OTLP exporter", exc)


class _SanitizedOTelHandler(logging.Handler):
    """Адаптер stdlib record к текущему официальному OTel logging handler."""

    def __init__(
        self,
        delegate: Any,
        provider: Any,
        *,
        default_profile: object = None,
        reporter: _FailureReporter,
    ) -> None:
        super().__init__(level=logging.INFO)
        self._delegate = delegate
        self._provider = provider
        self._default_profile = _safe_context_value(default_profile)
        self._owner_pid = os.getpid()
        self._reporter = reporter
        setattr(self, _HANDLER_MARKER, True)

    @property
    def owner_pid(self) -> int:
        return self._owner_pid

    @property
    def provider(self) -> Any:
        return self._provider

    def set_default_profile(self, value: object) -> None:
        self._default_profile = _safe_context_value(value)

    def emit(self, record: logging.LogRecord) -> None:
        if self._owner_pid != os.getpid() or _EXPORTER_INTERNAL.get():
            return
        try:
            self._delegate.emit(_safe_record(record, self._default_profile))
        except Exception as exc:
            self._reporter.report(
                "Ошибка подготовки записи для OTLP; локальный журнал продолжит работу",
                exc,
            )

    def flush(self) -> None:
        # Provider flush выполняется отдельным bounded shutdown-контрактом.
        return None


@dataclass(frozen=True)
class _OTelComponents:
    logger_provider: Any
    resource: Any
    batch_processor: Any
    log_exporter: Any
    logging_handler: Any


def _load_otel_components() -> _OTelComponents:
    """Загрузить нестабильные OTel Logs API только при явном opt-in."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.instrumentation.logging.handler import LoggingHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    return _OTelComponents(
        logger_provider=LoggerProvider,
        resource=Resource,
        batch_processor=BatchLogRecordProcessor,
        log_exporter=OTLPLogExporter,
        logging_handler=LoggingHandler,
    )


def _silence_otel_transport_loggers() -> None:
    """Не дублировать transport diagnostics в игровом console/logger потоке."""
    for name in _OTEL_INTERNAL_LOGGERS:
        internal_logger = logging.getLogger(name)
        internal_logger.setLevel(logging.CRITICAL + 1)
        internal_logger.propagate = False


def _bounded_int(
    name: str,
    default: int,
    maximum: int,
    *,
    fallback_name: str | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None and fallback_name is not None:
        raw = os.environ.get(fallback_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        _failure_reporter.report(
            f"Параметр {name} имеет неверное значение; используется default"
        )
        return default
    if value <= 0:
        _failure_reporter.report(
            f"Параметр {name} должен быть положительным; используется default"
        )
        return default
    return min(value, maximum)


def _handler_level() -> int:
    raw = os.environ.get("OTEL_PYTHON_LOG_HANDLER_LEVEL")
    if raw is None or not raw.strip():
        return _DEFAULT_HANDLER_LEVEL
    value = logging.getLevelName(raw.strip().upper())
    if isinstance(value, int):
        return value
    _failure_reporter.report(
        "Параметр OTEL_PYTHON_LOG_HANDLER_LEVEL имеет неверное значение; используется INFO"
    )
    return _DEFAULT_HANDLER_LEVEL


def _read_config() -> _ObservabilityConfig | None:
    if _is_true(os.environ.get("OTEL_SDK_DISABLED")):
        return None
    signal_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
    generic_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    endpoint = signal_endpoint or generic_endpoint
    if not endpoint:
        # Явный endpoint является opt-in и сохраняет обычный запуск offline.
        return None
    if not endpoint.lower().startswith(("http://", "https://")):
        _failure_reporter.report(
            "OTLP logs endpoint имеет неподдержанный URL; удалённый журнал отключён"
        )
        return None

    protocol = (
        (
            os.environ.get("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL")
            or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
            or _SUPPORTED_PROTOCOL
        )
        .strip()
        .lower()
    )
    if protocol != _SUPPORTED_PROTOCOL:
        _failure_reporter.report(
            "Для application logs поддерживается только OTLP/HTTP protobuf; удалённый журнал отключён"
        )
        return None

    max_queue_size = _bounded_int(
        "OTEL_BLRP_MAX_QUEUE_SIZE",
        _DEFAULT_MAX_QUEUE_SIZE,
        _MAX_QUEUE_SIZE,
    )
    max_export_batch_size = min(
        _bounded_int(
            "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
            _DEFAULT_MAX_EXPORT_BATCH_SIZE,
            _MAX_EXPORT_BATCH_SIZE,
        ),
        max_queue_size,
    )
    return _ObservabilityConfig(
        # Для signal-specific endpoint путь /v1/logs задаётся пользователем.
        # При общем endpoint передаём None в официальный exporter, чтобы он
        # сам применил стандартное добавление /v1/logs.
        signal_endpoint=signal_endpoint or None,
        handler_level=_handler_level(),
        timeout_millis=_bounded_int(
            "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
            _DEFAULT_EXPORT_TIMEOUT_MILLIS,
            _MAX_EXPORT_TIMEOUT_MILLIS,
            fallback_name="OTEL_EXPORTER_OTLP_TIMEOUT",
        ),
        schedule_delay_millis=_bounded_int(
            "OTEL_BLRP_SCHEDULE_DELAY",
            _DEFAULT_SCHEDULE_DELAY_MILLIS,
            _MAX_SCHEDULE_DELAY_MILLIS,
        ),
        max_queue_size=max_queue_size,
        max_export_batch_size=max_export_batch_size,
        processor_timeout_millis=_bounded_int(
            "OTEL_BLRP_EXPORT_TIMEOUT",
            _DEFAULT_PROCESSOR_TIMEOUT_MILLIS,
            _MAX_PROCESSOR_TIMEOUT_MILLIS,
        ),
    )


def _deployment_environment() -> str:
    raw = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip() == "deployment.environment.name":
            safe_value = _safe_context_value(value.strip())
            if safe_value:
                return safe_value
    return _DEFAULT_ENVIRONMENT


def _resource_attributes() -> dict[str, str]:
    attributes = {
        "service.name": "azurpilot",
        "deployment.environment.name": _deployment_environment(),
        "telemetry.sdk.language": "python",
        "telemetry.sdk.name": "opentelemetry",
    }
    try:
        attributes["telemetry.sdk.version"] = importlib.metadata.version(
            "opentelemetry-sdk"
        )
    except importlib.metadata.PackageNotFoundError:
        pass
    return attributes


def _build_runtime(
    target: logging.Logger,
    config: _ObservabilityConfig,
    *,
    default_profile: object,
    exporter_factory: Callable[[int], Any] | None = None,
) -> _Runtime:
    components = _load_otel_components()
    _silence_otel_transport_loggers()
    provider = components.logger_provider(
        resource=components.resource(_resource_attributes())
    )
    exporter = (
        exporter_factory(config.timeout_millis)
        if exporter_factory is not None
        else components.log_exporter(
            endpoint=config.signal_endpoint,
            timeout=config.timeout_millis / 1000,
        )
    )
    wrapped_exporter = _FailOpenExporter(exporter, _failure_reporter)
    processor = components.batch_processor(
        wrapped_exporter,
        schedule_delay_millis=config.schedule_delay_millis,
        max_export_batch_size=config.max_export_batch_size,
        export_timeout_millis=config.processor_timeout_millis,
        max_queue_size=config.max_queue_size,
    )
    provider.add_log_record_processor(processor)
    delegate = components.logging_handler(
        level=config.handler_level,
        logger_provider=provider,
        log_code_attributes=False,
    )
    handler = _SanitizedOTelHandler(
        delegate,
        provider,
        default_profile=default_profile,
        reporter=_failure_reporter,
    )
    handler.setLevel(config.handler_level)
    return _Runtime(target=target, provider=provider, handler=handler)


def _after_fork() -> None:
    global _state_lock
    _state_lock = threading.RLock()
    inherited = list(_runtimes.values())
    _runtimes.clear()
    # Не оставлять в дочернем процессе handler/provider с worker thread
    # родителя: новый process должен выполнить собственный explicit bootstrap.
    for runtime in inherited:
        try:
            runtime.target.removeHandler(runtime.handler)
        except Exception:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _register_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(_shutdown_at_exit)
    _atexit_registered = True


def configure_application_observability(
    target: logging.Logger,
    *,
    default_profile: object = None,
    _exporter_factory: Callable[[int], Any] | None = None,
) -> bool:
    """Идемпотентно включить OTLP logging для одного process-local logger."""
    if not isinstance(target, logging.Logger):
        return False
    process_id = os.getpid()
    with _state_lock:
        for handler in list(target.handlers):
            if not getattr(handler, _HANDLER_MARKER, False):
                continue
            if getattr(handler, "owner_pid", process_id) != process_id:
                target.removeHandler(handler)
                _runtimes.pop(id(target), None)

        config = _read_config()
        existing = next(
            (
                handler
                for handler in target.handlers
                if getattr(handler, _HANDLER_MARKER, False)
                and getattr(handler, "owner_pid", process_id) == process_id
            ),
            None,
        )
        if config is None:
            if existing is not None:
                target.removeHandler(existing)
                runtime = _runtimes.pop(id(target), None)
                if runtime is not None:
                    _shutdown_runtime(runtime, _SHUTDOWN_TIMEOUT_MILLIS)
            return False
        if existing is not None:
            existing.set_default_profile(default_profile)
            return True
        try:
            runtime = _build_runtime(
                target,
                config,
                default_profile=default_profile,
                exporter_factory=_exporter_factory,
            )
        except Exception as exc:
            _failure_reporter.report(
                "Не удалось инициализировать OTLP logging; локальный журнал продолжит работу",
                exc,
            )
            return False
        target.addHandler(runtime.handler)
        _runtimes[id(target)] = runtime
        _register_atexit()
        return True


def _shutdown_runtime(runtime: _Runtime, timeout_millis: int) -> bool:
    finished = threading.Event()

    def close_provider() -> None:
        try:
            runtime.provider.force_flush(timeout_millis=timeout_millis)
        except Exception as exc:
            _failure_reporter.report("Не удалось сбросить буфер OTLP logging", exc)
        try:
            runtime.provider.shutdown()
        except Exception as exc:
            _failure_reporter.report("Не удалось завершить OTLP logging provider", exc)
        finally:
            finished.set()

    worker = threading.Thread(
        target=close_provider,
        name="AzurPilotOtelShutdown",
        daemon=True,
    )
    worker.start()
    completed = finished.wait(max(0, timeout_millis) / 1000)
    if not completed:
        _failure_reporter.report(
            "Завершение OTLP logging остановлено по bounded timeout"
        )
    try:
        runtime.handler.close()
    except Exception:
        pass
    return completed


def shutdown_application_observability(
    target: logging.Logger | None = None,
    *,
    timeout_millis: int = _SHUTDOWN_TIMEOUT_MILLIS,
) -> bool:
    """Отключить OTLP handler и выполнить bounded flush без ошибки gameplay."""
    with _state_lock:
        if target is None:
            runtimes = list(_runtimes.values())
            _runtimes.clear()
        else:
            runtime = _runtimes.pop(id(target), None)
            runtimes = [runtime] if runtime is not None else []
        for runtime in runtimes:
            if runtime.handler in runtime.target.handlers:
                runtime.target.removeHandler(runtime.handler)

    completed = True
    for runtime in runtimes:
        if not _shutdown_runtime(runtime, timeout_millis):
            completed = False
    return completed


def _shutdown_at_exit() -> None:
    try:
        shutdown_application_observability()
    except Exception:
        pass


__all__ = (
    "configure_application_observability",
    "shutdown_application_observability",
)
