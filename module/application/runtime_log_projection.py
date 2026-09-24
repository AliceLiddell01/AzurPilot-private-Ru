"""Ограниченная файловая проекция журналов worker для read-only клиентов."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from module.config.profile import profile_identity_from_name
from module.logging_core import sanitize_log_text

_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_RECORD_CHARS = 16 * 1024
_MAX_LOG_LINES = 2000


def _profile_name(profile: str) -> str:
    identity = profile_identity_from_name(profile)
    if identity is None:
        raise ValueError("Имя runtime-профиля имеет неверный формат")
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
        raise OSError("Корень репозитория runtime-журнала недоступен")
    cursor = root
    for part in ("config", "state", "bot-runtime", "logs"):
        cursor = cursor / part
        if _is_reparse_point(cursor):
            raise OSError("Каталог runtime-журналов не может быть ссылкой")
        cursor.mkdir(exist_ok=True)
        if not cursor.is_dir() or _is_reparse_point(cursor):
            raise OSError("Каталог runtime-журналов не прошёл проверку identity")
    if logs_root.resolve() != cursor.resolve():
        raise OSError("Каталог runtime-журналов вышел за пределы репозитория")


def _read_bounded(path: Path) -> bytes:
    if _is_reparse_point(path):
        raise OSError("Файл runtime-журнала не может быть ссылкой")
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
    """Прочитать bounded tail, скрывая секреты и абсолютные локальные пути."""

    profile = _profile_name(profile)
    if type(limit) is not int or not 0 <= limit <= _MAX_LOG_LINES:
        raise ValueError("limit вне допустимого диапазона")
    if limit == 0:
        return ()
    root, logs_root, active = _paths(profile, repository_root)
    if not root.is_dir():
        raise OSError("Корень репозитория runtime-журнала недоступен")
    for directory in (root / "config", root / "config" / "state", logs_root.parent, logs_root):
        if _is_reparse_point(directory):
            raise OSError("Каталог runtime-журналов не может быть ссылкой")
    if not logs_root.is_dir():
        return ()
    for path in (active.with_name(active.name + ".1"), active):
        if _is_reparse_point(path):
            raise OSError("Файл runtime-журнала не может быть ссылкой")
        if path.exists() and path.resolve().parent != logs_root.resolve():
            raise OSError("Файл runtime-журнала находится вне разрешённого каталога")
    payload = _read_bounded(active.with_name(active.name + ".1")) + _read_bounded(active)
    payload = payload[-_MAX_FILE_BYTES:]
    if payload and not payload.endswith((b"\n", b"\r")):
        payload = payload.rsplit(b"\n", 1)[0]
    text = payload.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)[-limit:]
    return tuple(sanitize_log_text(line, limit=_MAX_RECORD_CHARS) for line in lines)


def runtime_log_signature(
    profile: str,
    *,
    repository_root: Path | str | None = None,
) -> tuple[tuple[str, int, int] | tuple[str, None, None], ...]:
    """Вернуть bounded метаданные файлов для дешёвой проверки обновления UI."""

    root, logs_root, active = _paths(profile, repository_root)
    for directory in (
        root / "config",
        root / "config" / "state",
        logs_root.parent,
        logs_root,
    ):
        if _is_reparse_point(directory):
            raise OSError("Каталог runtime-журналов не может быть ссылкой")
    if not logs_root.exists():
        return ()
    if _is_reparse_point(logs_root) or not logs_root.is_dir():
        raise OSError("Каталог runtime-журналов не прошёл проверку identity")
    values = []
    for path in (active.with_name(active.name + ".1"), active):
        if _is_reparse_point(path):
            raise OSError("Файл runtime-журнала не может быть ссылкой")
        if not path.exists():
            values.append((path.name, None, None))
            continue
        if path.resolve().parent != logs_root.resolve():
            raise OSError("Файл runtime-журнала находится вне разрешённого каталога")
        stat = path.stat()
        values.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(values)


class _SanitizedFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_log_text(
            super().format(record),
            limit=_MAX_RECORD_CHARS,
        )


class RuntimeLogProjectionHandler(RotatingFileHandler):
    """Один worker пишет bounded sanitized tail без участия WebUI или брокера."""

    def __init__(
        self,
        profile: str,
        *,
        repository_root: Path | str | None = None,
    ) -> None:
        root, logs_root, active = _paths(profile, repository_root)
        _prepare_logs_root(root, logs_root)
        if _is_reparse_point(active):
            raise OSError("Файл runtime-журнала не может быть ссылкой")
        super().__init__(
            active,
            maxBytes=_MAX_FILE_BYTES,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        self.setLevel(logging.INFO)
        self.setFormatter(
            _SanitizedFormatter(
                fmt="%(asctime)s.%(msecs)03d │ %(levelname)s │ %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            # Сбой необязательной UI-проекции не должен прерывать worker.
            return


__all__ = [
    "RuntimeLogProjectionHandler",
    "read_runtime_log_tail",
    "runtime_log_signature",
]
