"""Переиспользуемые примитивы центрального логирования AzurPilot."""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

_TASK_METADATA_LIMIT = 128
REMOTE_LOG_TEXT_LIMIT = 32 * 1024

_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:authorization|credential|access[_-]?token|api[_-]?key|token|password|passwd|secret|cookie|session|private[_-]?key)"
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|token|password|passwd|secret)=)[^&#\s]+"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|access[_-]?token|api[_-]?key|token|password|passwd|secret)"
    r"\s*([:=])\s*(?:bearer\s+)?[^\s,;]+"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _build_traceback_path_aliases():
    """Безопасно вычислить локальные path-aliases для удалённого журнала."""
    aliases = []
    for resolver, alias in (
        (lambda: Path(__file__).resolve().parent.parent, "<PROJECT_ROOT>"),
        (lambda: Path.home().resolve(), "<USER_HOME>"),
    ):
        try:
            local_path = str(resolver())
        except (OSError, RuntimeError):
            continue
        if local_path:
            aliases.append((re.compile(re.escape(local_path), re.IGNORECASE), alias))
    return tuple(aliases)


_TRACEBACK_PATH_ALIASES = _build_traceback_path_aliases()


def sanitize_traceback_text(value) -> str:
    """Скрыть типовые секреты, локальные пути и управляющие последовательности."""
    try:
        text = str("" if value is None else value)
    except Exception:
        text = "<значение не удалось безопасно преобразовать>"
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _UNSAFE_CONTROL_RE.sub("", text)
    text = _BIDI_CONTROL_RE.sub("", text)
    text = _URL_USERINFO_RE.sub(r"\g<scheme>***@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2***", text)
    for path_pattern, alias in _TRACEBACK_PATH_ALIASES:
        text = path_pattern.sub(alias, text)
    return text


def sanitize_log_text(value, limit: int = REMOTE_LOG_TEXT_LIMIT) -> str:
    """Очистить и ограничить текст перед сохранением за пределами процесса."""
    if limit <= 0:
        return ""
    text = sanitize_traceback_text(value)
    if len(text) <= limit:
        return text
    marker = "\n...[текст обрезан по ограничению удалённого журнала]"
    if len(marker) >= limit:
        return text[:limit]
    return text[: limit - len(marker)] + marker


@dataclass(frozen=True)
class SuppressionDecision:
    """Результат обработки потенциально повторяющегося события."""

    emit: bool
    summary_count: int = 0
    summary_duration: float = 0.0
    summary_level: int = logging.INFO
    summary_message: str = ""


@dataclass
class _SuppressionState:
    payload: object
    level: int
    message: str
    first_at: float
    last_emit_at: float
    last_seen_at: float
    suppressed: int = 0


class RepeatedEventSuppressor:
    """Bounded LRU-состояние для подавления идентичных повторов."""

    def __init__(self, *, max_keys: int = 256, default_window: float = 5.0) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys должен быть положительным")
        if default_window < 0:
            raise ValueError("default_window не может быть отрицательным")
        self.max_keys = max_keys
        self.default_window = default_window
        self._states: OrderedDict[Hashable, _SuppressionState] = OrderedDict()
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    @staticmethod
    def _summary(state: _SuppressionState, now: float) -> SuppressionDecision:
        if state.suppressed <= 0:
            return SuppressionDecision(emit=True)
        return SuppressionDecision(
            emit=True,
            summary_count=state.suppressed,
            summary_duration=max(0.0, now - state.first_at),
            summary_level=state.level,
            summary_message=state.message,
        )

    @staticmethod
    def _payload_equal(left: object, right: object) -> bool:
        """Безопасно сравнить payload, не полагаясь на скалярный результат ``==``."""
        if left is right:
            return True
        try:
            return bool(left == right)
        except Exception:
            return False

    def observe(
        self,
        key: Hashable,
        *,
        payload: object,
        level: int,
        message: str,
        window: float | None = None,
        now: float | None = None,
    ) -> SuppressionDecision:
        if window is None:
            window = self.default_window
        if window < 0:
            raise ValueError("window не может быть отрицательным")
        if now is None:
            now = time.monotonic()

        with self._lock:
            state = self._states.get(key)
            if state is None:
                self._states[key] = _SuppressionState(
                    payload=payload,
                    level=level,
                    message=message,
                    first_at=now,
                    last_emit_at=now,
                    last_seen_at=now,
                )
                self._states.move_to_end(key)
                while len(self._states) > self.max_keys:
                    self._states.popitem(last=False)
                return SuppressionDecision(emit=True)

            severity_escalated = level > state.level
            payload_changed = not self._payload_equal(payload, state.payload)
            window_elapsed = now - state.last_emit_at >= window
            never_suppress = level >= logging.ERROR

            if payload_changed or severity_escalated or window_elapsed or never_suppress:
                decision = self._summary(state, now)
                self._states[key] = _SuppressionState(
                    payload=payload,
                    level=level,
                    message=message,
                    first_at=now,
                    last_emit_at=now,
                    last_seen_at=now,
                )
                self._states.move_to_end(key)
                return decision

            state.suppressed += 1
            state.last_seen_at = now
            self._states.move_to_end(key)
            return SuppressionDecision(emit=False)

    def finish(
        self,
        key: Hashable,
        *,
        now: float | None = None,
    ) -> SuppressionDecision:
        if now is None:
            now = time.monotonic()
        with self._lock:
            state = self._states.pop(key, None)
            if state is None or state.suppressed <= 0:
                return SuppressionDecision(emit=False)
            return SuppressionDecision(
                emit=False,
                summary_count=state.suppressed,
                summary_duration=max(0.0, now - state.first_at),
                summary_level=state.level,
                summary_message=state.message,
            )

    def reset(self, key: Hashable | None = None) -> None:
        with self._lock:
            if key is None:
                self._states.clear()
            else:
                self._states.pop(key, None)


class DiagnosticContextHandler(logging.Handler):
    """Bounded DEBUG-ring, сохраняемый в отдельный файл только при ERROR/CRITICAL."""

    def __init__(
        self,
        *,
        capacity: int = 200,
        sanitizer: Callable[[object], str] = str,
        max_bytes: int = 2 * 1024 * 1024,
        backup_count: int = 2,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity должен быть положительным")
        super().__init__(level=logging.DEBUG)
        self.capacity = capacity
        self._buffer: deque[logging.LogRecord] = deque(maxlen=capacity)
        self._last_failure: tuple[logging.LogRecord, ...] = ()
        self._sanitizer = sanitizer
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._target: RotatingFileHandler | None = None
        self._failure_target: logging.Handler | None = None

    def _clone_record(self, record: logging.LogRecord) -> logging.LogRecord:
        # Не копируем __dict__ исходного LogRecord: произвольный ``extra`` может
        # удерживать секреты, изображения, NumPy-массивы и другие тяжёлые объекты.
        # Полный pathname также не нужен текущему formatter: оставляем только имя
        # файла, а дорогостоящую sanitization выполняем один раз — для сообщения.
        cloned = logging.LogRecord(
            name=record.name,
            level=record.levelno,
            pathname=record.filename,
            lineno=record.lineno,
            msg=self._sanitizer(record.getMessage()),
            args=(),
            exc_info=None,
            func=record.funcName,
            sinfo=None,
        )
        cloned.created = record.created
        cloned.msecs = record.msecs
        cloned.relativeCreated = record.relativeCreated
        cloned.thread = record.thread
        cloned.threadName = record.threadName
        cloned.process = record.process
        cloned.processName = record.processName
        if hasattr(record, "taskName"):
            cloned.taskName = record.taskName
        alas_task = getattr(record, "alas_task", None)
        if isinstance(alas_task, str):
            cloned.alas_task = alas_task[:_TASK_METADATA_LIMIT]
        return cloned

    def configure_output(self, path: str | Path, formatter: logging.Formatter) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if self._target is not None:
                self._target.close()
            target = RotatingFileHandler(
                output,
                maxBytes=self._max_bytes,
                backupCount=self._backup_count,
                encoding="utf-8",
                delay=True,
            )
            target.setLevel(logging.DEBUG)
            target.setFormatter(formatter)
            self._target = target

    def configure_failure_target(self, target: logging.Handler | None) -> None:
        """Задать normal file handler для условного DEBUG-dump при реальном сбое."""
        with self.lock:
            self._failure_target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit(record)
        except Exception:
            # Ошибка самого диагностического контура не должна прерывать игровой код.
            self.handleError(record)
            if record.levelno >= logging.ERROR:
                self._buffer.clear()

    def _emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.INFO:
            self._buffer.append(self._clone_record(record))
            return
        if record.levelno < logging.ERROR:
            return

        buffered = tuple(self._buffer)
        if buffered:
            self._last_failure = buffered
            header = logging.LogRecord(
                name=record.name,
                level=logging.INFO,
                pathname=record.filename,
                lineno=record.lineno,
                msg=(
                    "[Диагностика] Контекст перед %s: %s"
                    % (record.levelname, self._sanitizer(record.getMessage()))
                ),
                args=(),
                exc_info=None,
                func=record.funcName,
            )
            header.created = record.created
            header.msecs = record.msecs
            failure_target = self._failure_target
            if failure_target is None:
                owner_logger = logging.getLogger(record.name)
                for candidate in owner_logger.handlers:
                    if candidate is self:
                        continue
                    if isinstance(candidate, logging.FileHandler):
                        failure_target = candidate
                        break

            targets = []
            for target in (self._target, failure_target):
                if target is not None and all(target is not item for item in targets):
                    targets.append(target)
            for target in targets:
                try:
                    target.handle(copy.copy(header))
                    for buffered_record in buffered:
                        target.handle(copy.copy(buffered_record))
                    target.flush()
                except Exception:
                    self.handleError(record)
        self._buffer.clear()

    def snapshot(self, *, last_failure: bool = False) -> tuple[logging.LogRecord, ...]:
        with self.lock:
            source = self._last_failure if last_failure else tuple(self._buffer)
            return tuple(copy.copy(record) for record in source)

    def reset(self) -> None:
        with self.lock:
            self._buffer.clear()
            self._last_failure = ()

    def close(self) -> None:
        with self.lock:
            self._buffer.clear()
            self._last_failure = ()
            if self._target is not None:
                self._target.close()
                self._target = None
            self._failure_target = None
        super().close()
