"""Ограниченная файловая проекция журналов рабочих процессов для клиентов только для чтения."""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from module.config.profile import profile_identity_from_name
from module.logging_core import sanitize_log_text, sanitize_traceback_text

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_RECORD_CHARS = 16 * 1024
_MAX_LOG_LINES = 2000
_EVENT_FORMAT_VERSION = 1
_LEGACY_LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r" │ (?P<level_name>[A-Z]+) │ (?P<message>.*)$"
)
_RICH_STYLE_NAMES = (
    "bold|dim|italic|underline|strike|reverse|blink|conceal|"
    "black|red|green|yellow|blue|magenta|cyan|white|"
    "bright_black|bright_red|bright_green|bright_yellow|bright_blue|"
    "bright_magenta|bright_cyan|bright_white"
)
_RICH_MARKUP_TOKEN = re.compile(
    rf"\[(?:/(?:bold|dim|italic|underline|strike|reverse|blink|conceal)|"
    rf"(?:{_RICH_STYLE_NAMES})(?:\s+(?:{_RICH_STYLE_NAMES}))*)\]"
)


def _sanitize_message(value: str, *, limit: int, markup: bool) -> str:
    # Сначала сводим поддерживаемые Rich-теги к обычному тексту, а потом
    # очищаем всю строку целиком: секрет может пересекать границу тегов.
    plain_text = _RICH_MARKUP_TOKEN.sub("", value) if markup else value
    return sanitize_log_text(plain_text, limit=limit)


@dataclass(frozen=True, slots=True)
class RuntimeLogEvent:
    """Нейтральное ограниченное событие журнала для клиентов только для чтения."""

    timestamp: str
    level: int
    level_name: str
    message: str
    kind: str = "log"
    section_level: int | None = None
    traceback: str | None = None


def _decode_event(line: str) -> RuntimeLogEvent:
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict) and value.get("version") == _EVENT_FORMAT_VERSION:
        try:
            kind = value.get("kind")
            if kind not in {"log", "section"}:
                raise ValueError
            section_level = value.get("section_level")
            if section_level is not None and (
                type(section_level) is not int or section_level not in range(4)
            ):
                raise ValueError
            if (kind == "section") != (section_level is not None):
                raise ValueError
            level = value.get("level")
            if type(level) is not int:
                raise ValueError
            level_name = value.get("level_name")
            message = value.get("message")
            timestamp = value.get("timestamp")
            markup = value.get("markup")
            trace = value.get("traceback")
            if not all(
                isinstance(item, str) for item in (level_name, message, timestamp)
            ):
                raise ValueError
            if ("markup" in value and type(markup) is not bool) or (
                trace is not None and not isinstance(trace, str)
            ):
                raise ValueError
            return RuntimeLogEvent(
                timestamp=sanitize_log_text(timestamp, limit=64),
                level=level,
                level_name=sanitize_log_text(level_name, limit=64),
                message=_sanitize_message(
                    message,
                    limit=_MAX_RECORD_CHARS,
                    markup=markup is True,
                ),
                kind=kind,
                section_level=section_level,
                traceback=(
                    sanitize_log_text(trace, limit=_MAX_RECORD_CHARS)
                    if trace is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            pass
    # Сохраняем читаемость файлов прежнего форматировщика строк при ротации.
    legacy = _LEGACY_LOG_PATTERN.fullmatch(line)
    if legacy is not None:
        level_name = legacy.group("level_name")
        level = logging.getLevelName(level_name)
        if not isinstance(level, int):
            level = logging.INFO
        return RuntimeLogEvent(
            timestamp=legacy.group("timestamp"),
            level=level,
            level_name=level_name,
            message=_sanitize_message(
                legacy.group("message"),
                limit=_MAX_RECORD_CHARS,
                markup=True,
            ),
        )
    return RuntimeLogEvent(
        timestamp="",
        level=logging.INFO,
        level_name="",
        message=sanitize_log_text(line, limit=_MAX_RECORD_CHARS),
    )


def _read_event_lines(
    profile: str,
    limit: int,
    *,
    repository_root: Path | str | None,
) -> tuple[RuntimeLogEvent, ...]:
    profile = _profile_name(profile)
    if type(limit) is not int or not 0 <= limit <= _MAX_LOG_LINES:
        raise ValueError("limit вне допустимого диапазона")
    if limit == 0:
        return ()
    root, logs_root, active = _paths(profile, repository_root)
    if not root.is_dir():
            raise OSError("Корень репозитория журнала среды выполнения недоступен")
    for directory in (
        root / "config",
        root / "config" / "state",
        logs_root.parent,
        logs_root,
    ):
        if _is_reparse_point(directory):
            raise OSError("Каталог журналов среды выполнения не может быть ссылкой")
    if not logs_root.is_dir():
        return ()
    for path in (active.with_name(active.name + ".1"), active):
        if _is_reparse_point(path):
            raise OSError("Файл журнала среды выполнения не может быть ссылкой")
        if path.exists() and path.resolve().parent != logs_root.resolve():
            raise OSError("Файл журнала среды выполнения находится вне разрешённого каталога")
    payload = _read_bounded(active.with_name(active.name + ".1")) + _read_bounded(active)
    payload = payload[-_MAX_FILE_BYTES:]
    if payload and not payload.endswith((b"\n", b"\r")):
        payload = payload.rsplit(b"\n", 1)[0]
    text = (
        payload.decode("utf-8", errors="replace")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = text.splitlines()[-limit:]
    return tuple(_decode_event(line) for line in lines)


def _profile_name(profile: str) -> str:
    identity = profile_identity_from_name(profile)
    if identity is None:
        raise ValueError("Имя профиля среды выполнения имеет неверный формат")
    return identity.name


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _paths(
    profile: str, repository_root: Path | str | None
) -> tuple[Path, Path, Path]:
    profile = _profile_name(profile)
    root = Path(
        repository_root
        or os.environ.get("AZURPILOT_REPOSITORY_ROOT")
        or Path(__file__).resolve().parents[2]
    ).resolve()
    state_root = root / "config" / "state" / "bot-runtime"
    logs_root = state_root / "logs"
    active = logs_root / f"{profile}.log"
    return root, logs_root, active


def _prepare_logs_root(root: Path, logs_root: Path) -> None:
    if not root.is_dir():
        raise OSError("Корень репозитория журнала среды выполнения недоступен")
    cursor = root
    for part in ("config", "state", "bot-runtime", "logs"):
        cursor = cursor / part
        if _is_reparse_point(cursor):
            raise OSError("Каталог журналов среды выполнения не может быть ссылкой")
        cursor.mkdir(exist_ok=True)
        if not cursor.is_dir() or _is_reparse_point(cursor):
            raise OSError("Принадлежность каталога журналов среде выполнения не подтверждена")
    if logs_root.resolve() != cursor.resolve():
        raise OSError("Каталог журналов среды выполнения находится за пределами репозитория")


def _read_bounded(path: Path) -> bytes:
    if _is_reparse_point(path):
        raise OSError("Файл журнала среды выполнения не может быть ссылкой")
    if not path.is_file():
        return b""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        offset = max(0, size - _MAX_FILE_BYTES)
        handle.seek(offset)
        data = handle.read(_MAX_FILE_BYTES)
        if offset:
            handle.seek(offset - 1)
            previous = handle.read(1)
        else:
            previous = b"\n"
    if offset and previous not in (b"\n", b"\r"):
        data = data.split(b"\n", 1)[-1]
    return data


def read_runtime_log_tail(
    profile: str,
    limit: int = _MAX_LOG_LINES,
    *,
    repository_root: Path | str | None = None,
) -> tuple[str, ...]:
    """Прочитать ограниченный хвост в прежнем текстовом виде для старых потребителей."""

    events = _read_event_lines(profile, limit, repository_root=repository_root)
    lines: list[str] = []
    for event in reversed(events):
        for line in reversed(_plain_event_line(event).splitlines(keepends=True)):
            lines.append(line)
            if len(lines) == limit:
                return tuple(reversed(lines))
    return tuple(reversed(lines))


def read_runtime_log_events(
    profile: str,
    limit: int = _MAX_LOG_LINES,
    *,
    repository_root: Path | str | None = None,
) -> tuple[RuntimeLogEvent, ...]:
    """Прочитать ограниченные нейтральные события журнала среды выполнения."""

    return _read_event_lines(profile, limit, repository_root=repository_root)


def format_runtime_log_timestamp(value: str) -> str:
    """Привести timestamp события к каноническому виду runtime-журнала."""

    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except ValueError:
        return value


def _plain_event_line(event: RuntimeLogEvent) -> str:
    prefix = ""
    if event.timestamp or event.level_name:
        timestamp = format_runtime_log_timestamp(event.timestamp)
        prefix = f"{timestamp} │ {event.level_name} │ "
    text = prefix + event.message
    if event.traceback:
        text += "\n" + event.traceback
    return text + "\n"


def runtime_log_signature(
    profile: str,
    *,
    repository_root: Path | str | None = None,
) -> tuple[tuple[str, int, int] | tuple[str, None, None], ...]:
    """Вернуть ограниченные метаданные файлов для быстрой проверки обновления WebUI."""

    root, logs_root, active = _paths(profile, repository_root)
    for directory in (
        root / "config",
        root / "config" / "state",
        logs_root.parent,
        logs_root,
    ):
        if _is_reparse_point(directory):
            raise OSError("Каталог журналов среды выполнения не может быть ссылкой")
    if not logs_root.exists():
        return ()
    if _is_reparse_point(logs_root) or not logs_root.is_dir():
        raise OSError("Принадлежность каталога журналов среде выполнения не подтверждена")
    values = []
    for path in (active.with_name(active.name + ".1"), active):
        if _is_reparse_point(path):
            raise OSError("Файл журнала среды выполнения не может быть ссылкой")
        if not path.exists():
            values.append((path.name, None, None))
            continue
        if path.resolve().parent != logs_root.resolve():
            raise OSError("Файл журнала среды выполнения находится вне разрешённого каталога")
        stat = path.stat()
        values.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(values)


def _event_for_record(record: logging.LogRecord) -> dict[str, object]:
    try:
        message = record.getMessage()
    except Exception:
        message = "<сообщение не удалось безопасно сформировать>"
    markup = getattr(record, "markup", False) is True
    kind = getattr(record, "azurpilot_log_kind", "log")
    if kind != "section":
        kind = "log"
    section_level = getattr(record, "azurpilot_section_level", None)
    if type(section_level) is not int or section_level not in range(4):
        section_level = None
    if kind != "section" or section_level is None:
        kind = "log"
        section_level = None
    section_title = getattr(record, "azurpilot_section_title", None)
    if kind == "section" and isinstance(section_title, str):
        title = _sanitize_message(
            section_title,
            limit=_MAX_RECORD_CHARS // 2,
            markup=False,
        )
        message = f"<<< {title} >>>" if section_level == 3 else title
    else:
        message = _sanitize_message(
            message,
            limit=_MAX_RECORD_CHARS // 2,
            markup=markup,
        )
    trace = None
    if record.exc_info:
        try:
            trace = sanitize_traceback_text(
                "".join(traceback.format_exception(*record.exc_info))
            )
        except Exception:
            trace = "<трассировку не удалось безопасно сформировать>"
    if record.stack_info:
        stack = sanitize_traceback_text(record.stack_info)
        trace = f"{trace}\n{stack}" if trace else stack
    if trace:
        trace = sanitize_log_text(trace, limit=_MAX_RECORD_CHARS // 2)
    try:
        timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds"
        )
    except (OverflowError, OSError, ValueError):
        timestamp = ""
    event: dict[str, object] = {
        "version": _EVENT_FORMAT_VERSION,
        "timestamp": timestamp,
        "level": int(record.levelno),
        "level_name": sanitize_log_text(record.levelname, limit=64),
        "message": message,
        "kind": kind,
        "section_level": section_level,
        "traceback": trace,
    }
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    while len(encoded) > _MAX_RECORD_CHARS and (message or trace):
        excess = len(encoded) - _MAX_RECORD_CHARS
        if len(message) >= len(trace or ""):
            message = message[: max(0, len(message) - excess)]
            event["message"] = message
        else:
            trace = (trace or "")[: max(0, len(trace or "") - excess)]
            event["traceback"] = trace or None
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return event


class _SanitizedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            _event_for_record(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )


class RuntimeLogProjectionHandler(RotatingFileHandler):
    """Один рабочий процесс пишет ограниченный очищенный хвост без участия WebUI или брокера."""

    def __init__(
        self,
        profile: str,
        *,
        repository_root: Path | str | None = None,
    ) -> None:
        root, logs_root, active = _paths(profile, repository_root)
        _prepare_logs_root(root, logs_root)
        if _is_reparse_point(active):
            raise OSError("Файл журнала среды выполнения не может быть ссылкой")
        super().__init__(
            active,
            maxBytes=_MAX_FILE_BYTES,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        self.setLevel(logging.INFO)
        self.setFormatter(_SanitizedFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if (
                getattr(record, "azurpilot_log_kind", "log") != "section"
                and not record.exc_info
                and not record.stack_info
                and not record.getMessage().strip()
            ):
                return
            # За отложенное открытие, блокировку и ротацию отвечает RotatingFileHandler.
            super().emit(record)
        except Exception:
            # Сбой необязательной проекции для WebUI не должен прерывать рабочий процесс.
            return


__all__ = [
    "RuntimeLogEvent",
    "RuntimeLogProjectionHandler",
    "format_runtime_log_timestamp",
    "read_runtime_log_events",
    "read_runtime_log_tail",
    "runtime_log_signature",
]
