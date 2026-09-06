"""Общие bounded-нормализаторы для application observability signals."""

from __future__ import annotations

import re
import traceback

from module.config.profile import profile_identity_from_filename
from module.logging_core import sanitize_log_text, sanitize_traceback_text

_METRIC_LABEL_LIMIT = 64
_METRIC_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OUTCOMES = frozenset({"success", "recoverable", "failure", "stopped", "unknown"})
_REMOTE_ATTRIBUTE_LIMIT = 8 * 1024
_REMOTE_STACKTRACE_LIMIT = 32 * 1024
_MAX_EXCEPTION_ARGUMENTS = 64
_MAX_EXCEPTION_CHAIN_DEPTH = 32
_MAX_EXCEPTION_FRAMES = 128


def _metric_label(value: object) -> str:
    """Оставить только bounded ASCII task label или вернуть sentinel."""
    if not isinstance(value, str):
        return "unknown"
    try:
        value = value.strip()
        if not value or len(value) > _METRIC_LABEL_LIMIT:
            return "unknown"
        if _METRIC_LABEL_RE.fullmatch(value) is None:
            return "unknown"
    except Exception:
        return "unknown"
    return value


def _profile_label(value: object) -> str:
    """Сохранить canonical profile identity без task-only ASCII ограничения."""
    if not isinstance(value, str):
        return "unknown"
    try:
        if profile_identity_from_filename(f"{value}.json") is None:
            return "unknown"
    except Exception:
        return "unknown"
    return value


def _outcome_from_result(result: object) -> str:
    if result is True:
        return "success"
    if result is False:
        return "failure"
    if isinstance(result, str):
        try:
            if result == "recoverable":
                return "recoverable"
        except Exception:
            return "unknown"
    return "unknown"


def _outcome_from_exception(exception: BaseException) -> str:
    try:
        from module.config.config import TaskEnd

        if isinstance(exception, TaskEnd):
            return "stopped"
    except Exception:
        pass
    if isinstance(exception, KeyboardInterrupt):
        return "stopped"
    if isinstance(exception, SystemExit):
        try:
            return "stopped" if exception.code is None or exception.code == 0 else "failure"
        except Exception:
            return "failure"
    return "failure"


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
