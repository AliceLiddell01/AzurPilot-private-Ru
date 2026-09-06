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
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from module.logging_context import get_logging_context, get_task_context
from module.logging_core import (
    REMOTE_LOG_TEXT_LIMIT,
    is_sensitive_name,
    sanitize_log_text,
    sanitize_traceback_text,
)
from module.observability.metrics import (
    MetricsConfig,
    MetricsRuntime,
    activate_metrics_runtime,
    build_metrics_runtime,
    deactivate_metrics_runtime,
    reset_metrics_runtime_after_fork,
)
from module.observability.tracing import (
    TracingRuntime,
    activate_tracing_runtime,
    build_tracing_runtime,
    deactivate_tracing_runtime,
    reset_tracing_runtime_after_fork,
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
_DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS = 60_000
_MAX_METRIC_EXPORT_INTERVAL_MILLIS = 3_600_000
_DEFAULT_METRIC_EXPORT_TIMEOUT_MILLIS = 30_000
_MAX_METRIC_EXPORT_TIMEOUT_MILLIS = 30_000
_REMOTE_ATTRIBUTE_LIMIT = 8 * 1024
_REMOTE_STACKTRACE_LIMIT = 32 * 1024
_MAX_MESSAGE_MAPPING_ITEMS = 64
_MAX_EXCEPTION_ARGUMENTS = 64
_MAX_EXCEPTION_CHAIN_DEPTH = 32
_MAX_EXCEPTION_FRAMES = 128
_SHUTDOWN_TIMEOUT_MILLIS = 1_000
_FAILURE_REPORT_INTERVAL = 60.0
_HANDLER_MARKER = "_azurpilot_observability_handler"
_EXPORTER_INTERNAL = ContextVar("azurpilot_observability_exporter", default=False)
_OTEL_INTERNAL_LOGGERS = (
    "opentelemetry.exporter.otlp",
    "opentelemetry.sdk._logs",
    "opentelemetry.sdk.metrics",
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
    """Проверенный bounded contract OTLP signal-ов приложения."""

    signal_endpoint: str | None
    handler_level: int
    timeout_millis: int
    schedule_delay_millis: int
    max_queue_size: int
    max_export_batch_size: int
    processor_timeout_millis: int
    logs_enabled: bool = False
    metrics: MetricsConfig | None = None
    traces: TracingConfig | None = None


@dataclass(frozen=True)
class TracingConfig:
    """Проверенный bounded contract traces exporter-а."""

    endpoint: str | None
    timeout_millis: int
    schedule_delay_millis: int
    max_queue_size: int
    max_export_batch_size: int
    processor_timeout_millis: int


@dataclass
class _Runtime:
    target: logging.Logger
    provider: Any | None
    handler: _SanitizedOTelHandler | None
    metrics: MetricsRuntime | None = None
    traces: TracingRuntime | None = None
    config: _ObservabilityConfig | None = None


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


def _safe_message_mapping(args: Mapping[object, object]) -> dict[object, object]:
    """Ограниченно скопировать mapping-аргументы с маскированием секретных полей."""
    safe_args: dict[object, object] = {}
    for index, (key, value) in enumerate(args.items()):
        if index >= _MAX_MESSAGE_MAPPING_ITEMS:
            break
        safe_args[key] = "***" if is_sensitive_name(key) else _safe_message_argument(value)
    return safe_args


def _safe_message(record: logging.LogRecord) -> str:
    try:
        if isinstance(record.msg, str):
            safe_record = copy.copy(record)
            args = record.args
            if isinstance(args, tuple):
                safe_record.args = tuple(_safe_message_argument(item) for item in args)
            elif isinstance(args, Mapping):
                safe_record.args = _safe_message_mapping(args)
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
    try:
        args = getattr(value, "args", ())
    except Exception:
        args = ()
    if not isinstance(args, tuple):
        try:
            args = tuple(args) if args else ()
        except Exception:
            args = ()
    args = args[:_MAX_EXCEPTION_ARGUMENTS]
    scalar_args = bool(args) and all(
        isinstance(item, (str, int, float, bool)) or item is None
        for item in args
    )
    if scalar_args:
        try:
            text = str(value)
        except Exception:
            text = ""
        if text:
            return sanitize_log_text(text, _REMOTE_ATTRIBUTE_LIMIT)
    if args:
        parts = []
        for item in args:
            safe_item = _safe_message_argument(item)
            if safe_item is None:
                parts.append("None")
            elif isinstance(safe_item, (str, int, float, bool)):
                parts.append(str(safe_item))
            else:
                parts.append(f"<объект {type(item).__name__}>")
        return sanitize_log_text(", ".join(parts), _REMOTE_ATTRIBUTE_LIMIT)
    try:
        text = str(value)
    except Exception:
        text = ""
    if text:
        return sanitize_log_text(text, _REMOTE_ATTRIBUTE_LIMIT)
    return f"<исключение {type(value).__name__}>"


def _exception_type_name(exception_type: object, value: BaseException | None) -> str:
    try:
        name = getattr(exception_type, "__name__", None)
    except Exception:
        name = None
    if not isinstance(name, str) and value is not None:
        name = type(value).__name__
    return _safe_context_value(name or "Exception", limit=256) or "Exception"


def _format_exception_fragment(
    value: BaseException,
    traceback_object: object,
) -> str:
    type_name = _exception_type_name(type(value), value)
    message = _safe_exception_message(value)
    try:
        frames = (
            traceback.format_tb(traceback_object, limit=_MAX_EXCEPTION_FRAMES)
            if traceback_object
            else ()
        )
    except Exception:
        frames = ()
    return "".join(frames) + f"{type_name}: {message}\n"


def _format_exception_chain(
    value: BaseException | None,
    traceback_object: object,
    seen: set[int] | None = None,
    depth: int = 0,
) -> str:
    """Сформировать chain без locals и без произвольного repr сложных args."""
    if value is None:
        return ""
    if seen is None:
        seen = set()
    if id(value) in seen or depth >= _MAX_EXCEPTION_CHAIN_DEPTH:
        return _format_exception_fragment(value, traceback_object)
    seen.add(id(value))
    try:
        cause = getattr(value, "__cause__", None)
    except Exception:
        cause = None
    try:
        suppress_context = bool(getattr(value, "__suppress_context__", False))
    except Exception:
        suppress_context = False
    try:
        context = getattr(value, "__context__", None)
    except Exception:
        context = None

    previous = None
    marker = None
    if isinstance(cause, BaseException) and id(cause) not in seen:
        previous = _format_exception_chain(
            cause,
            getattr(cause, "__traceback__", None),
            seen,
            depth + 1,
        )
        marker = "Предыдущее исключение является непосредственной причиной следующего исключения:"
    elif (
        isinstance(context, BaseException)
        and not suppress_context
        and id(context) not in seen
    ):
        previous = _format_exception_chain(
            context,
            getattr(context, "__traceback__", None),
            seen,
            depth + 1,
        )
        marker = "При обработке предыдущего исключения возникло другое исключение:"

    current = _format_exception_fragment(value, traceback_object)
    if previous and marker:
        return f"{previous}\n{marker}\n\n{current}"
    return current


def _bounded_exception_stacktrace(value: object) -> str:
    """Очистить stacktrace и сохранить начало и конец при обрезке."""
    sanitized = sanitize_traceback_text(value)
    if len(sanitized) <= _REMOTE_STACKTRACE_LIMIT:
        return sanitize_log_text(sanitized, _REMOTE_STACKTRACE_LIMIT)
    marker = "\n...[трассировка обрезана по ограничению удалённого журнала]\n"
    available = max(0, _REMOTE_STACKTRACE_LIMIT - len(marker))
    head = available // 2
    tail = available - head
    return sanitized[:head] + marker + sanitized[-tail:]


def _exception_attributes(record: logging.LogRecord) -> dict[str, str]:
    if not record.exc_info:
        return {}
    try:
        exception_type, exception_value, traceback_object = record.exc_info
    except (TypeError, ValueError):
        return {}

    type_name = _exception_type_name(exception_type, exception_value)
    message = _safe_exception_message(exception_value)
    attributes = {
        "exception.type": type_name,
        "exception.message": message,
    }
    try:
        stacktrace = _format_exception_chain(exception_value, traceback_object)
    except Exception:
        stacktrace = f"{type_name}: {message}"
    attributes["exception.stacktrace"] = _bounded_exception_stacktrace(stacktrace)
    return attributes


def _safe_process_command(record: logging.LogRecord) -> str:
    try:
        command = Path(sys.argv[0]).name
    except (OSError, RuntimeError, TypeError):
        command = ""
    if not command:
        command = getattr(record, "processName", "")
    return _safe_context_value(command, limit=256) or "unknown"


def _attributes_for_record(
    record: logging.LogRecord,
    default_profile: str | None,
    default_component: str | None,
) -> dict[str, object]:
    context = get_logging_context()
    profile = context.profile or default_profile
    attributes: dict[str, object] = {}
    for key, value in (
        ("azurpilot.profile", profile),
        ("azurpilot.task", get_task_context()),
        ("azurpilot.component", context.component or default_component or record.name),
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
    default_component: str | None,
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
    for key, value in _attributes_for_record(record, default_profile, default_component).items():
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
        default_component: object = None,
        reporter: _FailureReporter,
    ) -> None:
        super().__init__(level=logging.INFO)
        self._delegate = delegate
        self._provider = provider
        self._default_profile = _safe_context_value(default_profile)
        self._default_component = _safe_context_value(default_component)
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

    def set_default_component(self, value: object) -> None:
        self._default_component = _safe_context_value(value)

    def emit(self, record: logging.LogRecord) -> None:
        if self._owner_pid != os.getpid() or _EXPORTER_INTERNAL.get():
            return
        try:
            self._delegate.emit(
                _safe_record(record, self._default_profile, self._default_component)
            )
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


def _read_signal_config(
    *,
    endpoint_name: str,
    protocol_name: str,
    generic_endpoint: str,
    generic_protocol: str,
    signal_name: str,
) -> tuple[bool, str | None]:
    signal_endpoint = os.environ.get(endpoint_name, "").strip()
    endpoint = signal_endpoint or generic_endpoint
    if not endpoint:
        return False, None
    if not endpoint.lower().startswith(("http://", "https://")):
        _failure_reporter.report(
            f"OTLP {signal_name} endpoint имеет неподдержанный URL; сигнал отключён"
        )
        return False, None

    protocol = (
        os.environ.get(protocol_name) or generic_protocol or _SUPPORTED_PROTOCOL
    ).strip().lower()
    if protocol != _SUPPORTED_PROTOCOL:
        _failure_reporter.report(
            f"Для application {signal_name} поддерживается только OTLP/HTTP protobuf; сигнал отключён"
        )
        return False, None
    return True, signal_endpoint or None


def _read_config() -> _ObservabilityConfig | None:
    if _is_true(os.environ.get("OTEL_SDK_DISABLED")):
        return None
    generic_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    generic_protocol = (
        os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL") or _SUPPORTED_PROTOCOL
    )
    logs_enabled, logs_endpoint = _read_signal_config(
        endpoint_name="OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        protocol_name="OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        generic_endpoint=generic_endpoint,
        generic_protocol=generic_protocol,
        signal_name="logs",
    )
    metrics_enabled, metrics_endpoint = _read_signal_config(
        endpoint_name="OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        protocol_name="OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
        generic_endpoint=generic_endpoint,
        generic_protocol=generic_protocol,
        signal_name="metrics",
    )
    if metrics_enabled:
        temporality = os.environ.get(
            "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
            "",
        ).strip().lower()
        if temporality and temporality != "cumulative":
            _failure_reporter.report(
                "Для текущего Alloy metrics path поддерживается только cumulative temporality; metrics отключены"
            )
            metrics_enabled = False

    traces_enabled, traces_endpoint = _read_signal_config(
        endpoint_name="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        protocol_name="OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        generic_endpoint=generic_endpoint,
        generic_protocol=generic_protocol,
        signal_name="traces",
    )

    if not logs_enabled and not metrics_enabled and not traces_enabled:
        # Явный endpoint является opt-in и сохраняет обычный запуск offline.
        return None

    max_queue_size = (
        _bounded_int(
            "OTEL_BLRP_MAX_QUEUE_SIZE",
            _DEFAULT_MAX_QUEUE_SIZE,
            _MAX_QUEUE_SIZE,
        )
        if logs_enabled
        else _DEFAULT_MAX_QUEUE_SIZE
    )
    max_export_batch_size = (
        min(
            _bounded_int(
                "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE",
                _DEFAULT_MAX_EXPORT_BATCH_SIZE,
                _MAX_EXPORT_BATCH_SIZE,
            ),
            max_queue_size,
        )
        if logs_enabled
        else _DEFAULT_MAX_EXPORT_BATCH_SIZE
    )
    traces_max_queue_size = (
        _bounded_int(
            "OTEL_BSP_MAX_QUEUE_SIZE",
            _DEFAULT_MAX_QUEUE_SIZE,
            _MAX_QUEUE_SIZE,
        )
        if traces_enabled
        else _DEFAULT_MAX_QUEUE_SIZE
    )
    traces_max_export_batch_size = (
        min(
            _bounded_int(
                "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
                _DEFAULT_MAX_EXPORT_BATCH_SIZE,
                _MAX_EXPORT_BATCH_SIZE,
            ),
            traces_max_queue_size,
        )
        if traces_enabled
        else _DEFAULT_MAX_EXPORT_BATCH_SIZE
    )
    return _ObservabilityConfig(
        # Для signal-specific endpoint путь /v1/logs задаётся пользователем.
        # При общем endpoint передаём None в официальный exporter, чтобы он
        # сам применил стандартное добавление /v1/logs.
        signal_endpoint=logs_endpoint,
        handler_level=_handler_level() if logs_enabled else _DEFAULT_HANDLER_LEVEL,
        timeout_millis=_bounded_int(
            "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
            _DEFAULT_EXPORT_TIMEOUT_MILLIS,
            _MAX_EXPORT_TIMEOUT_MILLIS,
            fallback_name="OTEL_EXPORTER_OTLP_TIMEOUT",
        ) if logs_enabled else _DEFAULT_EXPORT_TIMEOUT_MILLIS,
        schedule_delay_millis=_bounded_int(
            "OTEL_BLRP_SCHEDULE_DELAY",
            _DEFAULT_SCHEDULE_DELAY_MILLIS,
            _MAX_SCHEDULE_DELAY_MILLIS,
        ) if logs_enabled else _DEFAULT_SCHEDULE_DELAY_MILLIS,
        max_queue_size=max_queue_size,
        max_export_batch_size=max_export_batch_size,
        processor_timeout_millis=_bounded_int(
            "OTEL_BLRP_EXPORT_TIMEOUT",
            _DEFAULT_PROCESSOR_TIMEOUT_MILLIS,
            _MAX_PROCESSOR_TIMEOUT_MILLIS,
        ) if logs_enabled else _DEFAULT_PROCESSOR_TIMEOUT_MILLIS,
        logs_enabled=logs_enabled,
        metrics=(
            MetricsConfig(
                endpoint=metrics_endpoint,
                timeout_millis=_bounded_int(
                    "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
                    _DEFAULT_EXPORT_TIMEOUT_MILLIS,
                    _MAX_EXPORT_TIMEOUT_MILLIS,
                    fallback_name="OTEL_EXPORTER_OTLP_TIMEOUT",
                ),
                export_interval_millis=_bounded_int(
                    "OTEL_METRIC_EXPORT_INTERVAL",
                    _DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS,
                    _MAX_METRIC_EXPORT_INTERVAL_MILLIS,
                ),
                export_timeout_millis=_bounded_int(
                    "OTEL_METRIC_EXPORT_TIMEOUT",
                    _DEFAULT_METRIC_EXPORT_TIMEOUT_MILLIS,
                    _MAX_METRIC_EXPORT_TIMEOUT_MILLIS,
                ),
            )
            if metrics_enabled
            else None
        ),
        traces=(
            TracingConfig(
                endpoint=traces_endpoint,
                timeout_millis=_bounded_int(
                    "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
                    _DEFAULT_EXPORT_TIMEOUT_MILLIS,
                    _MAX_EXPORT_TIMEOUT_MILLIS,
                    fallback_name="OTEL_EXPORTER_OTLP_TIMEOUT",
                ),
                schedule_delay_millis=_bounded_int(
                    "OTEL_BSP_SCHEDULE_DELAY",
                    _DEFAULT_SCHEDULE_DELAY_MILLIS,
                    _MAX_SCHEDULE_DELAY_MILLIS,
                ),
                max_queue_size=traces_max_queue_size,
                max_export_batch_size=traces_max_export_batch_size,
                processor_timeout_millis=_bounded_int(
                    "OTEL_BSP_EXPORT_TIMEOUT",
                    _DEFAULT_PROCESSOR_TIMEOUT_MILLIS,
                    _MAX_PROCESSOR_TIMEOUT_MILLIS,
                ),
            )
            if traces_enabled
            else None
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
    default_component: object,
    exporter_factory: Callable[[int], Any] | None = None,
    metrics_exporter_factory: Callable[[int], Any] | None = None,
    metrics_reader_factory: Callable[[Any, int, int], Any] | None = None,
    traces_exporter_factory: Callable[[int], Any] | None = None,
) -> _Runtime:
    _silence_otel_transport_loggers()
    resource_attributes = _resource_attributes()
    log_components = _load_otel_components() if config.logs_enabled else None
    if log_components is not None:
        resource_type = log_components.resource
    else:
        from opentelemetry.sdk.resources import Resource

        resource_type = Resource
    resource = resource_type(resource_attributes)

    provider = None
    handler = None
    if config.logs_enabled and log_components is not None:
        try:
            provider = log_components.logger_provider(resource=resource)
            exporter = (
                exporter_factory(config.timeout_millis)
                if exporter_factory is not None
                else log_components.log_exporter(
                    endpoint=config.signal_endpoint,
                    timeout=config.timeout_millis / 1000,
                )
            )
            wrapped_exporter = _FailOpenExporter(exporter, _failure_reporter)
            processor = log_components.batch_processor(
                wrapped_exporter,
                schedule_delay_millis=config.schedule_delay_millis,
                max_export_batch_size=config.max_export_batch_size,
                export_timeout_millis=config.processor_timeout_millis,
                max_queue_size=config.max_queue_size,
            )
            provider.add_log_record_processor(processor)
            delegate = log_components.logging_handler(
                level=config.handler_level,
                logger_provider=provider,
                log_code_attributes=False,
            )
            handler = _SanitizedOTelHandler(
                delegate,
                provider,
                default_profile=default_profile,
                default_component=default_component,
                reporter=_failure_reporter,
            )
            handler.setLevel(config.handler_level)
        except Exception as exc:
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    pass
            provider = None
            handler = None
            _failure_reporter.report(
                "Не удалось инициализировать application logs; metrics продолжат работу",
                exc,
            )

    metrics_runtime = None
    if config.metrics is not None:
        try:
            metrics_runtime = build_metrics_runtime(
                config.metrics,
                resource=resource,
                reporter=_failure_reporter,
                exporter_factory=metrics_exporter_factory,
                reader_factory=metrics_reader_factory,
            )
            activate_metrics_runtime(metrics_runtime)
        except Exception as exc:
            _failure_reporter.report(
                "Не удалось инициализировать application metrics; logs продолжат работу",
                exc,
            )

    traces_runtime = None
    if config.traces is not None:
        try:
            traces_runtime = build_tracing_runtime(
                config.traces,
                resource=resource,
                reporter=_failure_reporter,
                exporter_factory=traces_exporter_factory,
            )
            activate_tracing_runtime(traces_runtime)
        except Exception as exc:
            _failure_reporter.report(
                "Не удалось инициализировать application traces; остальные сигналы продолжат работу",
                exc,
            )

    if provider is None and metrics_runtime is None and traces_runtime is None:
        raise RuntimeError("Не удалось создать ни одного application observability signal")
    return _Runtime(
        target=target,
        provider=provider,
        handler=handler,
        metrics=metrics_runtime,
        traces=traces_runtime,
        config=config,
    )


def _after_fork() -> None:
    global _state_lock
    _state_lock = threading.RLock()
    reset_metrics_runtime_after_fork()
    reset_tracing_runtime_after_fork()
    inherited = list(_runtimes.values())
    _runtimes.clear()
    # Не оставлять в дочернем процессе handler/provider с worker thread
    # родителя: новый process должен выполнить собственный explicit bootstrap.
    for runtime in inherited:
        deactivate_metrics_runtime(runtime.metrics)
        if runtime.metrics is not None:
            runtime.metrics.after_fork()
        deactivate_tracing_runtime(runtime.traces)
        if runtime.traces is not None:
            runtime.traces.after_fork()
        try:
            if runtime.handler is not None:
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
    default_component: object = None,
    _exporter_factory: Callable[[int], Any] | None = None,
    _metrics_exporter_factory: Callable[[int], Any] | None = None,
    _metrics_reader_factory: Callable[[Any, int, int], Any] | None = None,
    _traces_exporter_factory: Callable[[int], Any] | None = None,
) -> bool:
    """Идемпотентно включить независимые OTLP logs, metrics и traces."""
    if not isinstance(target, logging.Logger):
        return False
    process_id = os.getpid()
    with _state_lock:
        for handler in list(target.handlers):
            if not getattr(handler, _HANDLER_MARKER, False):
                continue
            if getattr(handler, "owner_pid", process_id) != process_id:
                target.removeHandler(handler)
                inherited_runtime = _runtimes.pop(id(target), None)
                if inherited_runtime is not None:
                    deactivate_metrics_runtime(inherited_runtime.metrics)
                    if inherited_runtime.metrics is not None:
                        inherited_runtime.metrics.after_fork()
                    deactivate_tracing_runtime(inherited_runtime.traces)
                    if inherited_runtime.traces is not None:
                        inherited_runtime.traces.after_fork()

        config = _read_config()
        runtime = _runtimes.get(id(target))
        if config is None:
            if runtime is not None:
                _runtimes.pop(id(target), None)
                deactivate_metrics_runtime(runtime.metrics)
                deactivate_tracing_runtime(runtime.traces)
                if runtime.handler is not None:
                    target.removeHandler(runtime.handler)
                _shutdown_runtime(runtime, _SHUTDOWN_TIMEOUT_MILLIS)
            for handler in list(target.handlers):
                if getattr(handler, _HANDLER_MARKER, False):
                    target.removeHandler(handler)
            return False

        if runtime is not None and runtime.config == config:
            if runtime.handler is not None:
                runtime.handler.set_default_profile(default_profile)
                runtime.handler.set_default_component(default_component)
            if runtime.metrics is not None:
                activate_metrics_runtime(runtime.metrics)
            if runtime.traces is not None:
                activate_tracing_runtime(runtime.traces)
            return True

        if runtime is not None:
            _runtimes.pop(id(target), None)
            deactivate_metrics_runtime(runtime.metrics)
            deactivate_tracing_runtime(runtime.traces)
            if runtime.handler is not None:
                target.removeHandler(runtime.handler)
            _shutdown_runtime(runtime, _SHUTDOWN_TIMEOUT_MILLIS)

        # Удалить оставшийся marker без известного runtime, чтобы не создавать duplicate handler.
        for handler in list(target.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                target.removeHandler(handler)
        try:
            runtime = _build_runtime(
                target,
                config,
                default_profile=default_profile,
                default_component=default_component,
                exporter_factory=_exporter_factory,
                metrics_exporter_factory=_metrics_exporter_factory,
                metrics_reader_factory=_metrics_reader_factory,
                traces_exporter_factory=_traces_exporter_factory,
            )
        except Exception as exc:
            _failure_reporter.report(
                "Не удалось инициализировать application observability; локальный журнал продолжит работу",
                exc,
            )
            return False
        if runtime.handler is not None:
            target.addHandler(runtime.handler)
        _runtimes[id(target)] = runtime
        _register_atexit()
        return True


def _shutdown_runtime(runtime: _Runtime, timeout_millis: int) -> bool:
    finished = threading.Event()
    deadline = time.monotonic() + max(0, timeout_millis) / 1000

    def remaining_timeout_millis() -> int:
        return max(0, int((deadline - time.monotonic()) * 1000))

    def close_provider() -> None:
        try:
            if runtime.provider is not None:
                try:
                    runtime.provider.force_flush(
                        timeout_millis=remaining_timeout_millis()
                    )
                except Exception as exc:
                    _failure_reporter.report("Не удалось сбросить буфер OTLP logging", exc)
                try:
                    try:
                        runtime.provider.shutdown(
                            timeout_millis=remaining_timeout_millis()
                        )
                    except TypeError:
                        runtime.provider.shutdown()
                except Exception as exc:
                    _failure_reporter.report("Не удалось завершить OTLP logging provider", exc)
            if runtime.metrics is not None:
                runtime.metrics.shutdown(remaining_timeout_millis())
            if runtime.traces is not None:
                runtime.traces.shutdown(remaining_timeout_millis())
        finally:
            finished.set()

    worker = threading.Thread(
        target=close_provider,
        name="AzurPilotOtelShutdown",
        daemon=True,
    )
    worker.start()
    completed = finished.wait(max(0, deadline - time.monotonic()))
    if not completed:
        _failure_reporter.report(
            "Завершение application observability остановлено по bounded timeout"
        )
    try:
        if runtime.handler is not None:
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
            deactivate_metrics_runtime(runtime.metrics)
            deactivate_tracing_runtime(runtime.traces)
            if runtime.handler is not None and runtime.handler in runtime.target.handlers:
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
