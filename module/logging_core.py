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
    r"""(?ix)
    (?<![\w-])
    (?P<key>[\"']?(?:authorization|credential|access[_-]?token|api[_-]?key|token|password|
    passwd|secret|cookie|session|private[_-]?key)[\"']?)
    \s*(?P<separator>[:=])\s*
    (?:bearer\s+)?
    (?P<value>
        \"(?:\\.|[^\"\\])*\"
        |'(?:\\.|[^'\\])*'
        |[^\s,;}\]]+
    )
    """
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_ABSOLUTE_PATH_PLACEHOLDER = "<ABSOLUTE_PATH>"
_PATH_HARD_BOUNDARY = frozenset(",;:!?()[]{}<>|\"'\r\n")
_PATH_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\s*[:=]")
_PATH_URL_RE = re.compile(r"(?:https?|file)://", re.IGNORECASE)
_FILE_URI_RE = re.compile(
    r"""(?<![A-Za-z0-9+.-])file://[^\r\n\"'<>|]*?(?=$|[,\r\n\"'<>|]|\s+(?=[A-Za-z_][A-Za-z0-9_.-]*\s*[:=])|\s+(?=(?:https?|file)://))""",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH_START_RE = re.compile(
    r"(?<![A-Za-z0-9_/:?])[A-Za-z]:[\\/]",
    re.IGNORECASE,
)
_UNC_ABSOLUTE_PATH_START_RE = re.compile(
    r"(?<![A-Za-z0-9_/:?])\\\\",
)
_POSIX_ABSOLUTE_PATH_START_RE = re.compile(
    r"(?<![/:A-Za-z0-9_<])/",
)
_ABSOLUTE_PATH_STARTS = (
    ("windows", _WINDOWS_ABSOLUTE_PATH_START_RE),
    ("unc", _UNC_ABSOLUTE_PATH_START_RE),
    ("posix", _POSIX_ABSOLUTE_PATH_START_RE),
)


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


def _is_absolute_path_start(text: str, index: int) -> bool:
    return any(pattern.match(text, index) for _, pattern in _ABSOLUTE_PATH_STARTS)


def _path_continuation_is_boundary(text: str, index: int) -> bool:
    probe = index
    while probe < len(text) and text[probe] in " \t":
        probe += 1
    return (
        probe != index
        and (
            probe >= len(text)
            or _PATH_FIELD_RE.match(text, probe) is not None
            or _PATH_URL_RE.match(text, probe) is not None
            or _is_absolute_path_start(text, probe)
        )
    )


def _consume_absolute_path(text: str, start: int, kind: str) -> int | None:
    index = start
    while index < len(text):
        character = text[index]
        if character in _PATH_HARD_BOUNDARY:
            break
        if character in " \t" and _path_continuation_is_boundary(text, index):
            break
        index += 1

    end = index
    while end > start and text[end - 1] in " \t":
        end -= 1
    if kind == "unc":
        components = text[start:end].split("\\")
        if len(components) < 2 or not all(component.strip() for component in components[:2]):
            return None
    return end


def _redact_absolute_paths(text: str) -> str:
    fragments: list[str] = []
    cursor = 0
    while cursor < len(text):
        candidates = [
            (match.start(), match.end(), kind)
            for kind, pattern in _ABSOLUTE_PATH_STARTS
            for match in [pattern.search(text, cursor)]
            if match is not None
        ]
        if not candidates:
            fragments.append(text[cursor:])
            break
        start, prefix_end, kind = min(candidates)
        end = _consume_absolute_path(text, prefix_end, kind)
        if end is None:
            fragments.append(text[cursor:prefix_end])
            cursor = prefix_end
            continue
        fragments.append(text[cursor:start])
        fragments.append(_ABSOLUTE_PATH_PLACEHOLDER)
        cursor = end
    return "".join(fragments)


def sanitize_traceback_text(value) -> str:
    """Скрыть секреты, абсолютные пути и управляющие последовательности."""
    try:
        text = str("" if value is None else value)
    except Exception:
        text = "<значение не удалось безопасно преобразовать>"
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _UNSAFE_CONTROL_RE.sub("", text)
    text = _BIDI_CONTROL_RE.sub("", text)
    text = _FILE_URI_RE.sub(_ABSOLUTE_PATH_PLACEHOLDER, text)
    text = _URL_USERINFO_RE.sub(r"\g<scheme>***@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2***", text)
    for path_pattern, alias in _TRACEBACK_PATH_ALIASES:
        text = path_pattern.sub(alias, text)
    return _redact_absolute_paths(text)


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


def is_sensitive_name(value: object) -> bool:
    """Проверить, обозначает ли имя поля потенциально секретное значение."""
    try:
        return bool(_SENSITIVE_NAME_RE.search(str(value)[:_TASK_METADATA_LIMIT]))
    except Exception:
        return False


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
    """Потокобезопасный bounded ring для контекста реального incident-а.

    Обработчик никогда не создаёт и не открывает файл. Текущий контекст
    хранится в памяти до ошибки, после которой атомарно становится
    ``last_failure`` для единственного incident producer-а.
    """

    def __init__(
        self,
        *,
        capacity: int = 200,
        sanitizer: Callable[[object], str] = str,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity должен быть положительным")
        if max_bytes <= 0:
            raise ValueError("max_bytes должен быть положительным")
        super().__init__(level=logging.DEBUG)
        self.capacity = capacity
        self._buffer: deque[logging.LogRecord] = deque(maxlen=capacity)
        self._buffer_bytes = 0
        self._last_failure: tuple[logging.LogRecord, ...] = ()
        self._sanitizer = sanitizer
        self._max_bytes = max_bytes

    def _bounded_message(self, message: object) -> str:
        text = str(message)
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self._max_bytes:
            return text
        return encoded[: self._max_bytes].decode("utf-8", errors="ignore")

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
            msg=self._bounded_message(self._sanitizer(record.getMessage())),
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

    @staticmethod
    def _record_bytes(record: logging.LogRecord) -> int:
        return len(record.getMessage().encode("utf-8", errors="replace"))

    def _append(self, record: logging.LogRecord) -> None:
        if len(self._buffer) == self.capacity:
            self._buffer_bytes -= self._record_bytes(self._buffer[0])
        self._buffer.append(record)
        self._buffer_bytes += self._record_bytes(record)
        while self._buffer and self._buffer_bytes > self._max_bytes:
            removed = self._buffer.popleft()
            self._buffer_bytes -= self._record_bytes(removed)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self.lock:
                self._emit(record)
        except Exception:
            # Ошибка самого диагностического контура не должна прерывать игровой код.
            self.handleError(record)
            with self.lock:
                self._buffer.clear()
                self._buffer_bytes = 0

    def _emit(self, record: logging.LogRecord) -> None:
        cloned = self._clone_record(record)
        if record.levelno >= logging.ERROR:
            failure = tuple(self._buffer) + (cloned,)
            self._last_failure = self._bounded_snapshot(failure)
            self._buffer.clear()
            self._buffer_bytes = 0
            return
        self._append(cloned)

    def _bounded_snapshot(
        self,
        records: tuple[logging.LogRecord, ...],
    ) -> tuple[logging.LogRecord, ...]:
        selected: deque[logging.LogRecord] = deque()
        total_bytes = 0
        for record in reversed(records):
            if len(selected) >= self.capacity:
                break
            record_bytes = self._record_bytes(record)
            if selected and total_bytes + record_bytes > self._max_bytes:
                break
            selected.appendleft(record)
            total_bytes += record_bytes
        return tuple(selected)

    def snapshot(self, *, last_failure: bool = False) -> tuple[logging.LogRecord, ...]:
        with self.lock:
            source = self._last_failure if last_failure else tuple(self._buffer)
            return tuple(copy.copy(record) for record in source)

    def reset(self) -> None:
        with self.lock:
            self._buffer.clear()
            self._buffer_bytes = 0
            self._last_failure = ()

    def close(self) -> None:
        with self.lock:
            self._buffer.clear()
            self._buffer_bytes = 0
            self._last_failure = ()
        super().close()
