"""Постоянные ограниченные диагностические данные DevSession с учётом задач.

Модуль намеренно не импортирует MCP. Менеджер владеет публичным API выполнения,
а планировщик и рабочий процесс устройства используют небольшие функции-перехватчики
в конце файла. Все файлы ограничены текущей рабочей копией, а межпроцессные записи
используют блокировку и атомарную замену.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.dev_runtime.contracts import (
    DEV_PROFILE,
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionState,
)
from module.dev_runtime.sanitizer import MAX_SANITIZED_TEXT, redact_text
from module.dev_runtime.task_sandbox import (
    TASK_POLICY_ACTIVE,
    TASK_POLICY_FILE_ENV,
    TASK_POLICY_ROOT_ENV,
    TASK_POLICY_SESSION_ENV,
    TaskPolicyStore,
)

EVIDENCE_SCHEMA_VERSION = 1
TIMELINE_SCHEMA_VERSION = 1
EVIDENCE_HEALTH_COMPLETE = "complete"
EVIDENCE_HEALTH_DEGRADED = "degraded"
EVIDENCE_HEALTH_CORRUPT = "corrupt"
EVIDENCE_HEALTH_UNAVAILABLE = "unavailable"

_MAX_SESSION_LENGTH = 128
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_TIMELINE_BYTES = 2 * 1024 * 1024
_MAX_TIMELINE_EVENTS = 2048
_MAX_EVENT_FIELDS = 32
_MAX_EVENT_TEXT = 512
_MAX_CHANGED_PATHS = 256
_MAX_CHANGED_PATH_LENGTH = 260
_MAX_GIT_OUTPUT = 64 * 1024
_GIT_TIMEOUT = 3.0
_LOCK_TIMEOUT = 10.0
_LOCK_RETRY_INTERVAL = 0.05
_MAX_LOG_LINE_BYTES = 4096
_MAX_LOG_PAGE_BYTES = 64 * 1024
_MAX_LOG_PAGE_LINES = 200
_MAX_CURSOR_LENGTH = 2048
_MAX_IMAGE_WIDTH = 4096
_MAX_IMAGE_HEIGHT = 4096
_MAX_IMAGE_PIXELS = 8_388_608
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_ENCODE_SECONDS = 2.0
_MAX_SCREENSHOTS_PER_SESSION = 128
_SCREENSHOT_WAIT_SECONDS = 5.0
_SCREENSHOT_POLL_SECONDS = 0.05
_MAX_RETENTION_SESSIONS = 20
_MAX_RETENTION_AGE_SECONDS = 30 * 24 * 60 * 60
_MAX_RETENTION_BYTES = 50 * 1024 * 1024
_SAFE_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{7,128}$")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_EVENT_TYPES = frozenset(
    {
        "session_created",
        "policy_prepared",
        "process_started",
        "session_ready",
        "task_started",
        "task_finished",
        "dependency_registered",
        "runtime_warning",
        "runtime_error",
        "stop_requested",
        "process_stopped",
        "cleanup_started",
        "cleanup_completed",
        "session_stopped",
    }
)
_EVENT_FIELD_NAMES = frozenset(
    {
        "code",
        "confirmed",
        "current_task",
        "dependency_sequence",
        "exception_type",
        "mode",
        "outcome",
        "phase",
        "policy_state",
        "preserved",
        "profile",
        "reason",
        "reason_code",
        "required_by",
        "root",
        "source",
        "state",
        "task",
        "task_mode",
        "type",
    }
)

_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "profile",
        "created_at",
        "started_at",
        "stopped_at",
        "root_tasks",
        "excluded_tasks",
        "git_snapshot",
        "evidence_health",
        "timeline",
        "logs",
        "screenshots",
        "last_error",
        "dependency_summary",
        "current_task",
        "cleanup",
    }
)


class EvidenceError(RuntimeError):
    """Безопасная машиночитаемая ошибка чтения или записи диагностики."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceUnavailable(EvidenceError):
    """Диагностические данные для указанной сессии отсутствуют или ещё не собраны."""


class EvidenceCorrupt(EvidenceError):
    """Файл диагностики повреждён и не может считаться полным."""


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    head: str | None
    branch: str | None
    detached: bool | None
    dirty: bool | None
    changed_paths: tuple[str, ...]
    available: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "head": self.head,
            "branch": self.branch,
            "detached": self.detached,
            "dirty": self.dirty,
            "changed_paths": list(self.changed_paths),
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    sequence: int
    timestamp: str
    event_type: str
    fields: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.event_type,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True, slots=True)
class EvidenceScreenshot:
    """Результат, к которому MCP-адаптер может прикрепить официальный ImageContent."""

    result: DevResult
    image: bytes | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mtime_ns: int

    def as_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mtime_ns": self.mtime_ns,
        }

    def same_file(self, other: _FileIdentity) -> bool:
        """Сравнить устойчивую идентичность файла, не учитывая добавление и время изменения."""

        return self.device == other.device and self.inode == other.inode

    @classmethod
    def from_value(cls, value: object) -> _FileIdentity | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"device", "inode", "mtime_ns"}:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Идентификатор файла журнала повреждён")
        try:
            device = value["device"]
            inode = value["inode"]
            mtime_ns = value["mtime_ns"]
        except KeyError as exc:
            raise EvidenceCorrupt(
                "DEV_EVIDENCE_CORRUPT", "Идентификатор файла журнала неполон"
            ) from exc
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (device, inode, mtime_ns)
        ):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Идентификатор файла журнала некорректен")
        return cls(device, inode, mtime_ns)


def validate_session_id(value: object) -> str:
    """Проверить тот же безопасный для пути формат, что используется в корне диагностики."""

    if not isinstance(value, str) or not _SAFE_SESSION.fullmatch(value):
        raise ValueError("session_id имеет недопустимый формат")
    if ".." in value:
        raise ValueError("session_id содержит недопустимый путь")
    return value


def _safe_selector(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Выбор задачи имеет недопустимый формат")
    if (
        value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "/" in value
        or "\\" in value
        or ".." in value
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Выбор задачи содержит путь или управляющий символ")
    return value


def _utc_timestamp(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > 80:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метка времени имеет недопустимый формат")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метка времени не является датой ISO") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метка времени должна содержать часовой пояс UTC")
    return value


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _absolute_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        try:
            return _absolute_path(left) == _absolute_path(right)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError as exc:
        raise EvidenceError(
            "DEV_EVIDENCE_UNSAFE_PATH", "Нельзя проверить ссылку или junction в пути диагностики"
        ) from exc


def _ensure_scoped_path(path: Path, repository_root: Path, *, label: str) -> Path:
    try:
        root = Path(os.path.abspath(repository_root))
        candidate = Path(os.path.abspath(path))
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(
            "DEV_EVIDENCE_FOREIGN_PATH", f"{label} выходит за пределы рабочей копии"
        ) from exc

    if _is_reparse_point(root):
        raise EvidenceError("DEV_EVIDENCE_UNSAFE_PATH", "Корень рабочей копии является ссылкой или junction")
    current = root
    for component in relative.parts:
        current /= component
        if _is_reparse_point(current):
            raise EvidenceError(
                "DEV_EVIDENCE_UNSAFE_PATH", f"{label} проходит через ссылку или junction"
            )
    return candidate


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    temporary = to_tmp_file(str(path))
    try:
        file_write(
            temporary,
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        )
        replace_tmp(temporary, str(path))
    finally:
        try:
            Path(temporary).unlink()
        except (FileNotFoundError, OSError):
            pass


def _read_json(path: Path, *, max_bytes: int) -> object:
    try:
        if _is_reparse_point(path):
            raise EvidenceError(
                "DEV_EVIDENCE_UNSAFE_PATH", "Файл диагностики не должен быть ссылкой или junction"
            )
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EvidenceUnavailable("DEV_EVIDENCE_NOT_COLLECTED", "Манифест диагностики отсутствует") from exc
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError("DEV_EVIDENCE_UNREADABLE", "Файл диагностики невозможно прочитать") from exc
    if len(raw) > max_bytes:
        raise EvidenceCorrupt("DEV_EVIDENCE_TOO_LARGE", "Файл диагностики превышает допустимый размер")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Файл диагностики содержит некорректный JSON") from exc


@contextmanager
def _exclusive_lock(path: Path, repository_root: Path) -> Iterator[None]:
    _ensure_scoped_path(path, repository_root, label="путь блокировки диагностики")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(path):
        raise EvidenceError("DEV_EVIDENCE_UNSAFE_PATH", "Блокировка диагностики не должна быть ссылкой")
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + _LOCK_TIMEOUT
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время ожидания блокировки диагностики")
                    time.sleep(_LOCK_RETRY_INTERVAL)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время ожидания блокировки диагностики")
                    time.sleep(_LOCK_RETRY_INTERVAL)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _file_identity(path: Path) -> _FileIdentity:
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise EvidenceError("DEV_EVIDENCE_LOG_BOUNDARY_LOST", "Идентификатор файла журнала недоступен") from exc
    return _FileIdentity(
        device=int(stat_result.st_dev),
        inode=int(stat_result.st_ino),
        mtime_ns=int(stat_result.st_mtime_ns),
    )


def _clip_output(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    return value[:_MAX_GIT_OUTPUT]


def _git_failure(reason: str) -> GitSnapshot:
    return GitSnapshot(
        head=None,
        branch=None,
        detached=None,
        dirty=None,
        changed_paths=(),
        available=False,
        reason=reason,
    )


def capture_git_snapshot(
    repository_root: Path,
    *,
    runner: Callable[..., object] | None = None,
) -> GitSnapshot:
    """Получить только локальные HEAD/branch/status без сети и выгрузки конфигурации."""

    root = Path(os.path.abspath(repository_root))
    try:
        _ensure_scoped_path(root, root, label="корень репозитория Git")
    except EvidenceError:
        return _git_failure("git_snapshot_failed")

    def run_git(args: tuple[str, ...]) -> object:
        command = ["git", *args]
        if runner is not None:
            return runner(
                command,
                cwd=str(root),
                shell=False,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )
        process = subprocess.Popen(
            command,
            cwd=str(root),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        captured = bytearray()

        def drain_stdout() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                remaining = _MAX_GIT_OUTPUT + 1 - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])

        reader = threading.Thread(target=drain_stdout, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
            reader.join(timeout=1.0)
            raise
        reader.join(timeout=1.0)
        return SimpleNamespace(returncode=returncode, stdout=bytes(captured))

    try:
        head_result = run_git(("rev-parse", "--verify", "HEAD"))
    except subprocess.TimeoutExpired:
        return _git_failure("git_snapshot_timeout")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _git_failure("git_snapshot_unavailable")
    head_code = getattr(head_result, "returncode", None)
    head = _clip_output(getattr(head_result, "stdout", "")).strip()
    if head_code != 0 or not _SAFE_SHA.fullmatch(head):
        return _git_failure("git_snapshot_nonzero")

    try:
        branch_result = run_git(("symbolic-ref", "--quiet", "--short", "HEAD"))
    except subprocess.TimeoutExpired:
        return _git_failure("git_snapshot_timeout")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _git_failure("git_snapshot_unavailable")
    branch_code = getattr(branch_result, "returncode", None)
    branch = _clip_output(getattr(branch_result, "stdout", "")).strip()
    if branch_code not in (0, 1):
        return _git_failure("git_snapshot_nonzero")
    if branch_code == 0:
        if not branch or len(branch) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in branch):
            return _git_failure("git_snapshot_invalid_branch")
        detached = False
    else:
        branch = "detached"
        detached = True

    try:
        status_result = run_git(("status", "--porcelain=v1", "--untracked-files=no"))
    except subprocess.TimeoutExpired:
        return _git_failure("git_snapshot_timeout")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _git_failure("git_snapshot_unavailable")
    if getattr(status_result, "returncode", None) != 0:
        return _git_failure("git_snapshot_nonzero")

    changed_paths: list[str] = []
    for raw_line in _clip_output(getattr(status_result, "stdout", "")).splitlines():
        if len(raw_line) < 4:
            continue
        path_value = raw_line[3:]
        if " -> " in path_value:
            path_value = path_value.rsplit(" -> ", 1)[-1]
        path_value = path_value.replace("\\", "/")
        if (
            not path_value
            or len(path_value) > _MAX_CHANGED_PATH_LENGTH
            or path_value.startswith("/")
            or re.match(r"^[A-Za-z]:/", path_value)
            or path_value == ".."
            or path_value.startswith("../")
            or "/../" in path_value
        ):
            return _git_failure("git_snapshot_invalid_path")
        if raw_line[:2] == "??":
            continue
        changed_paths.append(path_value)
        if len(changed_paths) >= _MAX_CHANGED_PATHS:
            break

    return GitSnapshot(
        head=head.lower(),
        branch=branch,
        detached=detached,
        dirty=bool(changed_paths),
        changed_paths=tuple(changed_paths),
        available=True,
    )


def _validate_git_snapshot(value: object) -> dict[str, object]:
    required = {"head", "branch", "detached", "dirty", "changed_paths", "available", "reason"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Снимок Git имеет неполную структуру")
    head = value.get("head")
    branch = value.get("branch")
    detached = value.get("detached")
    dirty = value.get("dirty")
    available = value.get("available")
    reason = value.get("reason")
    paths = value.get("changed_paths")
    if head is not None and (not isinstance(head, str) or not _SAFE_SHA.fullmatch(head)):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "HEAD Git имеет некорректный формат")
    if branch is not None and (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in branch)
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Ветка Git имеет некорректный формат")
    if detached is not None and not isinstance(detached, bool):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Признак detached Git имеет некорректный тип")
    if dirty is not None and not isinstance(dirty, bool):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Признак изменённого состояния Git имеет некорректный тип")
    if not isinstance(available, bool):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Признак доступности Git имеет некорректный тип")
    if reason is not None and (
        not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,128}", reason)
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Причина состояния Git имеет некорректный тип")
    if not isinstance(paths, list) or len(paths) > _MAX_CHANGED_PATHS:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Список изменённых путей Git имеет некорректный тип")
    safe_paths: list[str] = []
    for path_value in paths:
        normalized = path_value.replace("\\", "/") if isinstance(path_value, str) else ""
        if (
            not normalized
            or len(normalized) > _MAX_CHANGED_PATH_LENGTH
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:/", normalized)
            or normalized in {".", ".."}
            or normalized.startswith("../")
            or "/../" in normalized
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        ):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Путь изменения Git имеет некорректный формат")
        safe_paths.append(normalized)
    if available:
        if (
            not isinstance(head, str)
            or not isinstance(branch, str)
            or not isinstance(detached, bool)
            or not isinstance(dirty, bool)
            or reason is not None
            or dirty != bool(safe_paths)
        ):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Доступный снимок Git имеет неполные поля")
    elif (
        any(item is not None for item in (head, branch, detached, dirty))
        or safe_paths
        or reason is None
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Недоступный снимок Git содержит недопустимые данные")
    return {
        "head": head,
        "branch": branch,
        "detached": detached,
        "dirty": dirty,
        "changed_paths": safe_paths,
        "available": available,
        "reason": reason,
    }


def _validate_event_fields(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or len(value) > _MAX_EVENT_FIELDS:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Поля события хронологии имеют некорректный формат")
    safe: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _EVENT_FIELD_NAMES:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Событие хронологии содержит неизвестное поле")
        if isinstance(item, str):
            if len(item) > _MAX_EVENT_TEXT:
                raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Текст события хронологии слишком длинный")
            safe[key] = redact_text(item, max_length=_MAX_EVENT_TEXT)
        elif isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, int) and not isinstance(item, bool) and abs(item) <= 10**12:
            safe[key] = item
        elif isinstance(item, (list, tuple)) and len(item) <= 32 and all(
            isinstance(part, str) and len(part) <= _MAX_EVENT_TEXT for part in item
        ):
            safe[key] = [redact_text(part, max_length=_MAX_EVENT_TEXT) for part in item]
        else:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Поле события хронологии имеет небезопасный тип")
    return safe


def _validate_event(value: object) -> TimelineEvent:
    if not isinstance(value, Mapping) or set(value) != {"sequence", "timestamp", "type", "fields"}:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Событие хронологии имеет некорректную структуру")
    sequence = value.get("sequence")
    event_type = value.get("type")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 < sequence <= 10**12
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Порядковый номер хронологии имеет некорректный формат")
    if not isinstance(event_type, str) or not _SAFE_EVENT_TYPE.fullmatch(event_type):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Тип события хронологии имеет некорректный формат")
    if event_type not in _EVENT_TYPES:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Хронология содержит неизвестный тип события")
    timestamp = _utc_timestamp(value.get("timestamp"))
    assert isinstance(timestamp, str)
    return TimelineEvent(sequence, timestamp, event_type, _validate_event_fields(value.get("fields")))


def _validate_timeline(value: object) -> list[TimelineEvent]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "events", "truncated"}
        or value.get("schema_version") != TIMELINE_SCHEMA_VERSION
        or not isinstance(value.get("truncated"), bool)
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Хронология имеет неподдерживаемую схему")
    raw_events = value.get("events")
    if not isinstance(raw_events, list) or len(raw_events) > _MAX_TIMELINE_EVENTS:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Список событий хронологии имеет некорректный размер")
    events: list[TimelineEvent] = []
    previous = None
    for raw_event in raw_events:
        event = _validate_event(raw_event)
        if previous is not None and event.sequence != previous + 1:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Порядковые номера хронологии имеют пропуск или повтор")
        previous = event.sequence
        events.append(event)
    return events


def _empty_timeline() -> dict[str, object]:
    return {"schema_version": TIMELINE_SCHEMA_VERSION, "events": [], "truncated": False}


def _timeline_metadata(events: list[TimelineEvent], *, truncated: bool = False) -> dict[str, object]:
    return {
        "relative_file": "timeline.json",
        "event_count": len(events),
        "first_sequence": events[0].sequence if events else None,
        "last_sequence": events[-1].sequence if events else 0,
        "last_timestamp": events[-1].timestamp if events else None,
        "truncated": truncated,
    }


def _safe_log_source() -> str:
    return "config/state/dev-runtime-gui.log"


def _validate_health(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"status", "reasons"}:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Состояние диагностики должно быть объектом")
    status = value.get("status")
    reasons = value.get("reasons")
    if status not in {
        EVIDENCE_HEALTH_COMPLETE,
        EVIDENCE_HEALTH_DEGRADED,
        EVIDENCE_HEALTH_CORRUPT,
        EVIDENCE_HEALTH_UNAVAILABLE,
    }:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Статус состояния диагностики неизвестен")
    if not isinstance(reasons, list) or len(reasons) > 32 or not all(
        isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,128}", reason) is not None
        for reason in reasons
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Причины состояния диагностики имеют некорректный формат")
    unique_reasons = list(dict.fromkeys(reasons))
    if status == EVIDENCE_HEALTH_COMPLETE and unique_reasons:
        raise EvidenceCorrupt(
            "DEV_EVIDENCE_CORRUPT",
            "Полное состояние диагностики не может иметь причины деградации",
        )
    if status != EVIDENCE_HEALTH_COMPLETE and not unique_reasons:
        raise EvidenceCorrupt(
            "DEV_EVIDENCE_CORRUPT",
            "Неполное состояние диагностики должно иметь причину",
        )
    return {"status": status, "reasons": unique_reasons}


def _validate_timeline_metadata(value: object) -> dict[str, object]:
    required = {
        "relative_file",
        "event_count",
        "first_sequence",
        "last_sequence",
        "last_timestamp",
        "truncated",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест хронологии имеет неполную структуру")
    if value.get("relative_file") != "timeline.json":
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест хронологии указывает на чужой файл")
    event_count = value.get("event_count")
    first_sequence = value.get("first_sequence")
    last_sequence = value.get("last_sequence")
    truncated = value.get("truncated")
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or not 0 <= event_count <= _MAX_TIMELINE_EVENTS
        or isinstance(last_sequence, bool)
        or not isinstance(last_sequence, int)
        or not 0 <= last_sequence <= 10**12
        or not isinstance(truncated, bool)
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест хронологии имеет некорректные границы")
    if first_sequence is not None and (
        isinstance(first_sequence, bool)
        or not isinstance(first_sequence, int)
        or not 0 < first_sequence <= 10**12
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Первый порядковый номер в манифесте хронологии некорректен")
    last_timestamp = _utc_timestamp(value.get("last_timestamp"), allow_none=True)
    if event_count == 0 and (first_sequence is not None or last_sequence != 0 or last_timestamp is not None):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Пустой манифест хронологии имеет неверные метаданные")
    if event_count > 0 and (first_sequence is None or last_sequence < first_sequence or last_timestamp is None):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Непустой манифест хронологии имеет неверные метаданные")
    if event_count > 0 and not truncated and (first_sequence != 1 or last_sequence != event_count):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Полная хронология имеет неверный диапазон порядковых номеров")
    if event_count > 0 and truncated and last_sequence - first_sequence + 1 != event_count:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Усечённая хронология имеет неверный диапазон порядковых номеров")
    return {
        "relative_file": "timeline.json",
        "event_count": event_count,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "last_timestamp": last_timestamp,
        "truncated": truncated,
    }


def _validate_log_metadata(value: object) -> dict[str, object]:
    required = {"source", "available", "boundary_offset", "boundary_identity", "truncated"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест журнала имеет неполную структуру")
    source = value.get("source")
    available = value.get("available")
    boundary_offset = value.get("boundary_offset")
    boundary_identity = _FileIdentity.from_value(value.get("boundary_identity"))
    truncated = value.get("truncated")
    if source != _safe_log_source() or not isinstance(available, bool) or not isinstance(truncated, bool):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест журнала имеет небезопасные поля")
    if boundary_offset is not None and (
        isinstance(boundary_offset, bool) or not isinstance(boundary_offset, int) or boundary_offset < 0
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Смещение границы журнала имеет неверный тип")
    if available and (boundary_offset is None or boundary_identity is None):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Активная граница журнала неполна")
    if not available and (boundary_offset is not None or boundary_identity is not None):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Недоступный журнал содержит границу")
    return {
        "source": source,
        "available": available,
        "boundary_offset": boundary_offset,
        "boundary_identity": boundary_identity.as_dict() if boundary_identity is not None else None,
        "truncated": truncated,
    }


def _validate_screenshot_metadata(value: object) -> dict[str, object]:
    required = {"screenshot_id", "timestamp", "mime", "width", "height", "byte_size", "sha256"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные снимка экрана имеют неполную структуру")
    screenshot_id = value.get("screenshot_id")
    validate_session_id(screenshot_id)
    timestamp = _utc_timestamp(value.get("timestamp"))
    if value.get("mime") != "image/png":
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Формат снимка экрана не поддерживается")
    width, height, byte_size = value.get("width"), value.get("height"), value.get("byte_size")
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in (width, height, byte_size)
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Размеры снимка экрана имеют неверный тип")
    if (
        width <= 0
        or height <= 0
        or width > _MAX_IMAGE_WIDTH
        or height > _MAX_IMAGE_HEIGHT
        or width * height > _MAX_IMAGE_PIXELS
        or byte_size <= 0
        or byte_size > _MAX_IMAGE_BYTES
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные снимка экрана выходят за допустимые границы")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "SHA-256 снимка экрана имеет неверный формат")
    return {
        "screenshot_id": screenshot_id,
        "timestamp": timestamp,
        "mime": "image/png",
        "width": width,
        "height": height,
        "byte_size": byte_size,
        "sha256": sha256,
    }


def _validate_screenshot_summary(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"count", "latest"}:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест снимков экрана имеет неполную структуру")
    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _MAX_SCREENSHOTS_PER_SESSION:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Число снимков экрана выходит за допустимые границы")
    latest = value.get("latest")
    if latest is not None:
        latest = _validate_screenshot_metadata(latest)
    if count == 0 and latest is not None:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Пустая сводка снимков экрана содержит последний снимок")
    if count > 0 and latest is None:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Сводка снимков экрана не содержит последний снимок")
    return {"count": count, "latest": latest}


def _validate_dependency_summary(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"count", "last"}:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест зависимостей имеет неполную структуру")
    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _MAX_TIMELINE_EVENTS:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Число зависимостей имеет неверный тип")
    last = value.get("last")
    if last is not None:
        required = {"task", "required_by", "root", "reason", "sequence", "timestamp"}
        if not isinstance(last, Mapping) or set(last) != required:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Данные о последней зависимости неполны")
        for name in ("task", "required_by", "root"):
            _safe_selector(last.get(name))
        if last.get("reason") not in {"dependency", "dependency_override"}:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Причина зависимости неизвестна")
        sequence = last.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 < sequence <= 10**12
        ):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Порядковый номер зависимости имеет неверный тип")
        timestamp = _utc_timestamp(last.get("timestamp"))
        last = {
            "task": last["task"],
            "required_by": last["required_by"],
            "root": last["root"],
            "reason": last["reason"],
            "sequence": sequence,
            "timestamp": timestamp,
        }
    if count == 0 and last is not None:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Пустая сводка зависимостей содержит последнюю запись")
    if count > 0 and last is None:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Сводка зависимостей не содержит последнюю запись")
    return {"count": count, "last": last}


def _validate_cleanup(value: object) -> dict[str, object]:
    required = {"status", "confirmed", "preserved", "updated_at"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест очистки имеет неполную структуру")
    status = value.get("status")
    if status not in {"pending", "complete", "preserved"}:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Статус очистки в манифесте неизвестен")
    confirmed = value.get("confirmed")
    preserved = value.get("preserved")
    if not isinstance(confirmed, bool) or not isinstance(preserved, bool):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Признаки очистки в манифесте имеют неверный тип")
    updated_at = _utc_timestamp(value.get("updated_at"))
    if status == "pending" and (confirmed or preserved):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Ожидающая очистка не может быть подтверждённой или сохранённой")
    if status == "complete" and (not confirmed or preserved):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Завершённая очистка имеет несовместимые признаки")
    if status == "preserved" and (confirmed or not preserved):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Сохранённая очистка имеет несовместимые признаки")
    return {
        "status": status,
        "confirmed": confirmed,
        "preserved": preserved,
        "updated_at": updated_at,
    }


def _validate_structured_error(value: object) -> dict[str, object]:
    required = {"type", "message", "phase", "task", "timestamp", "sequence", "frames"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Последняя ошибка имеет неполную структуру")
    for name, max_length in (("type", 128), ("message", MAX_SANITIZED_TEXT), ("phase", 128)):
        item = value.get(name)
        if not isinstance(item, str) or not item or len(item) > max_length:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", f"Поле последней ошибки {name} имеет неверный тип")
    task = value.get("task")
    if task is not None:
        _safe_selector(task)
    sequence = value.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 < sequence <= 10**12
    ):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Порядковый номер последней ошибки имеет неверный тип")
    timestamp = _utc_timestamp(value.get("timestamp"))
    frames = value.get("frames")
    if not isinstance(frames, list) or len(frames) > 32:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Кадры последней ошибки имеют неверный размер")
    safe_frames: list[dict[str, object]] = []
    for frame in frames:
        if not isinstance(frame, Mapping) or set(frame) not in ({"path", "line", "function"}, {"module"}):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Кадр последней ошибки имеет небезопасную структуру")
        if set(frame) == {"module"}:
            module = frame.get("module")
            if not isinstance(module, str) or not module or len(module) > 128:
                raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Модуль последней ошибки имеет неверный тип")
            safe_frames.append({"module": redact_text(module, max_length=128)})
            continue
        path, line, function = frame.get("path"), frame.get("line"), frame.get("function")
        if not isinstance(path, str) or not path or len(path) > 260:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Путь последней ошибки имеет неверный тип")
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Строка последней ошибки имеет неверный тип")
        if not isinstance(function, str) or not function or len(function) > 128:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Функция последней ошибки имеет неверный тип")
        safe_frames.append(
            {
                "path": redact_text(path, max_length=260),
                "line": line,
                "function": redact_text(function, max_length=128),
            }
        )
    return {
        "type": redact_text(value["type"], max_length=128),
        "message": redact_text(value["message"], max_length=MAX_SANITIZED_TEXT),
        "phase": redact_text(value["phase"], max_length=128),
        "task": task,
        "timestamp": timestamp,
        "sequence": sequence,
        "frames": safe_frames,
    }


def _validate_manifest(value: object, expected_session_id: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_KEYS:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест имеет неполную или неизвестную структуру")
    if value.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Манифест имеет неподдерживаемую схему")
    if value.get("session_id") != expected_session_id or value.get("profile") != DEV_PROFILE:
        raise EvidenceCorrupt("DEV_EVIDENCE_FOREIGN_SESSION", "Манифест принадлежит другой сессии или профилю")
    validate_session_id(value.get("session_id"))
    timestamps = {
        field_name: _utc_timestamp(value.get(field_name), allow_none=field_name == "stopped_at")
        for field_name in ("created_at", "started_at", "stopped_at")
    }
    created_at = datetime.fromisoformat(timestamps["created_at"])
    started_at = datetime.fromisoformat(timestamps["started_at"])
    stopped_at = (
        datetime.fromisoformat(timestamps["stopped_at"])
        if timestamps["stopped_at"] is not None
        else None
    )
    if started_at < created_at or (stopped_at is not None and stopped_at < started_at):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Временные метки жизненного цикла идут в неверном порядке")
    for field_name in ("root_tasks", "excluded_tasks"):
        values = value.get(field_name)
        if not isinstance(values, list) or len(values) > 256:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", f"Поле манифеста {field_name} имеет некорректный формат")
        for item in values:
            _safe_selector(item)
    roots = value.get("root_tasks")
    excluded = value.get("excluded_tasks")
    if len(set(roots)) != len(roots) or len(set(excluded)) != len(excluded) or set(roots) & set(excluded):
        raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Корневые и исключённые задачи манифеста конфликтуют")
    manifest = dict(value)
    manifest["git_snapshot"] = _validate_git_snapshot(value.get("git_snapshot"))
    manifest["evidence_health"] = _validate_health(value.get("evidence_health"))
    manifest["timeline"] = _validate_timeline_metadata(value.get("timeline"))
    manifest["logs"] = _validate_log_metadata(value.get("logs"))
    manifest["screenshots"] = _validate_screenshot_summary(value.get("screenshots"))
    manifest["dependency_summary"] = _validate_dependency_summary(value.get("dependency_summary"))
    current_task = value.get("current_task")
    if current_task is not None:
        _safe_selector(current_task)
    manifest["cleanup"] = _validate_cleanup(value.get("cleanup"))
    last_error = value.get("last_error")
    manifest["last_error"] = None if last_error is None else _validate_structured_error(last_error)
    return manifest


def _validate_image_bytes(data: bytes) -> tuple[int, int]:
    if not isinstance(data, bytes) or not data or len(data) > _MAX_IMAGE_BYTES:
        raise EvidenceError("DEV_SCREENSHOT_TOO_LARGE", "Данные снимка экрана превышают допустимый размер")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("Снимок экрана должен быть PNG")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > _MAX_IMAGE_WIDTH
                or height > _MAX_IMAGE_HEIGHT
                or width * height > _MAX_IMAGE_PIXELS
            ):
                raise EvidenceError("DEV_SCREENSHOT_TOO_LARGE", "Размеры снимка экрана превышают допустимые границы")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("Снимок экрана должен быть PNG")
            image.load()
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError("DEV_SCREENSHOT_INVALID", "Данные снимка экрана не являются допустимым изображением") from exc
    return width, height


def _encode_png(image: object) -> tuple[bytes, int, int]:
    started = time.monotonic()
    try:
        import numpy as np
        from PIL import Image

        Image.init()
        array = np.asarray(image)
        if array.dtype != np.uint8 or array.ndim not in (2, 3):
            raise ValueError("Изображение должно быть uint8 в формате grayscale/RGB/RGBA")
        if array.ndim == 3 and array.shape[2] not in (1, 3, 4):
            raise ValueError("Каналы изображения не поддерживаются")
        height, width = int(array.shape[0]), int(array.shape[1])
        if (
            width <= 0
            or height <= 0
            or width > _MAX_IMAGE_WIDTH
            or height > _MAX_IMAGE_HEIGHT
            or width * height > _MAX_IMAGE_PIXELS
            or int(array.nbytes) > _MAX_IMAGE_BYTES * 4
        ):
            raise EvidenceError("DEV_SCREENSHOT_TOO_LARGE", "Размеры снимка экрана превышают допустимые границы")
        output = io.BytesIO()
        Image.fromarray(array).save(output, format="PNG")
        data = output.getvalue()
        if time.monotonic() - started > _MAX_IMAGE_ENCODE_SECONDS:
            raise EvidenceError("DEV_SCREENSHOT_ENCODE_TIMEOUT", "Кодирование снимка экрана превысило временной лимит")
        if len(data) > _MAX_IMAGE_BYTES:
            raise EvidenceError("DEV_SCREENSHOT_TOO_LARGE", "PNG снимка экрана превышает допустимый размер")
        decoded_width, decoded_height = _validate_image_bytes(data)
        if (decoded_width, decoded_height) != (width, height):
            raise EvidenceError("DEV_SCREENSHOT_INVALID", "Размер закодированного снимка экрана не совпал с кадром")
        return data, width, height
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError("DEV_SCREENSHOT_ENCODE_FAILED", "Кодирование снимка экрана завершилось ошибкой") from exc


def _frame_error(
    repository_root: Path,
    exception: BaseException,
    *,
    phase: str,
    task: str | None,
    timestamp: str,
    sequence: int,
) -> dict[str, object]:
    frames: list[dict[str, object]] = []
    try:
        extracted = traceback.extract_tb(exception.__traceback__)
    except Exception:
        extracted = []
    root = Path(os.path.abspath(repository_root))
    for frame in extracted[-32:]:
        filename = str(frame.filename)
        try:
            absolute = Path(os.path.abspath(filename))
            relative = absolute.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            relative = None
        if relative is not None and relative != "." and not relative.startswith("../"):
            frames.append(
                {
                    "path": redact_text(relative, max_length=260),
                    "line": int(frame.lineno),
                    "function": redact_text(str(frame.name), max_length=128),
                }
            )
        else:
            frames.append({"module": redact_text(Path(filename).name, max_length=128)})
    safe_task = None
    if isinstance(task, str):
        try:
            safe_task = _safe_selector(task)
        except EvidenceError:
            pass
    return {
        "type": redact_text(type(exception).__name__, max_length=128),
        "message": redact_text(str(exception), max_length=MAX_SANITIZED_TEXT) or type(exception).__name__,
        "phase": redact_text(phase, max_length=128),
        "task": safe_task,
        "timestamp": timestamp,
        "sequence": sequence,
        "frames": frames,
    }


class EvidenceStore:
    """Одно хранилище диагностики в рабочей копии, привязанное к точному session_id."""

    def __init__(
        self,
        environment: DevEnvironment,
        session_id: str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.environment = environment
        self.session_id = validate_session_id(session_id)
        self.now = now or (lambda: datetime.now(UTC))
        self.root = _ensure_scoped_path(
            environment.evidence_root / self.session_id,
            environment.repository_root,
            label="корень сессии диагностики",
        )
        self.manifest_path = _ensure_scoped_path(
            self.root / "manifest.json", environment.repository_root, label="путь манифеста диагностики"
        )
        self.timeline_path = _ensure_scoped_path(
            self.root / "timeline.json", environment.repository_root, label="путь хронологии диагностики"
        )
        self.screenshot_dir = _ensure_scoped_path(
            self.root / "screenshots", environment.repository_root, label="путь снимков экрана диагностики"
        )
        self.request_path = _ensure_scoped_path(
            self.root / "screenshot-request.json",
            environment.repository_root,
            label="путь запроса снимка экрана диагностики",
        )
        self.lock_path = _ensure_scoped_path(
            self.root / "evidence.lock", environment.repository_root, label="путь блокировки сессии диагностики"
        )

    @classmethod
    def create(
        cls,
        environment: DevEnvironment,
        *,
        session_id: str,
        root_tasks: Iterable[str],
        excluded_tasks: Iterable[str],
        timestamp: str,
        now: Callable[[], datetime] | None = None,
    ) -> EvidenceStore:
        store = cls(environment, session_id, now=now)
        roots = sorted({_safe_selector(item) for item in root_tasks})
        excluded = sorted({_safe_selector(item) for item in excluded_tasks})
        if not roots or set(roots) & set(excluded):
            raise EvidenceError("DEV_EVIDENCE_TASKS_INVALID", "Корневые и исключённые задачи диагностики конфликтуют")
        git_snapshot = capture_git_snapshot(environment.repository_root)
        health = {
            "status": EVIDENCE_HEALTH_COMPLETE,
            "reasons": [] if git_snapshot.available else [git_snapshot.reason or "git_snapshot_failed"],
        }
        if health["reasons"]:
            health["status"] = EVIDENCE_HEALTH_DEGRADED
        manifest: dict[str, object] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "session_id": store.session_id,
            "profile": DEV_PROFILE,
            "created_at": _utc_timestamp(timestamp),
            "started_at": _utc_timestamp(timestamp),
            "stopped_at": None,
            "root_tasks": roots,
            "excluded_tasks": excluded,
            "git_snapshot": git_snapshot.as_dict(),
            "evidence_health": health,
            "timeline": _timeline_metadata([]),
            "logs": {
                "source": _safe_log_source(),
                "available": False,
                "boundary_offset": None,
                "boundary_identity": None,
                "truncated": False,
            },
            "screenshots": {"count": 0, "latest": None},
            "last_error": None,
            "dependency_summary": {"count": 0, "last": None},
            "current_task": None,
            "cleanup": {
                "status": "pending",
                "confirmed": False,
                "preserved": False,
                "updated_at": _utc_timestamp(timestamp),
            },
        }
        evidence_root = _ensure_scoped_path(
            environment.evidence_root,
            environment.repository_root,
            label="корень диагностики",
        )
        with _exclusive_lock(environment.evidence_lock_file, environment.repository_root):
            evidence_root.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(store.root):
                raise EvidenceError(
                    "DEV_EVIDENCE_UNSAFE_PATH",
                    "Корень существующей диагностики является ссылкой или junction",
                )
            if store.root.exists():
                for existing_path in (store.manifest_path, store.timeline_path, store.screenshot_dir):
                    if os.path.lexists(existing_path) and _is_reparse_point(existing_path):
                        raise EvidenceError(
                            "DEV_EVIDENCE_UNSAFE_PATH",
                            "Файл существующей диагностики является ссылкой или junction",
                        )
                try:
                    with os.scandir(store.root) as entries:
                        has_entries = next(entries, None) is not None
                except OSError as exc:
                    raise EvidenceError(
                        "DEV_EVIDENCE_UNREADABLE",
                        "Каталог существующей диагностики невозможно прочитать",
                    ) from exc
                if has_entries:
                    raise EvidenceError(
                        "DEV_EVIDENCE_SESSION_EXISTS",
                        "Сессия диагностики с таким session_id уже существует",
                    )
            else:
                store.root.mkdir()
            _ensure_scoped_path(
                store.screenshot_dir,
                environment.repository_root,
                label="путь снимков экрана диагностики",
            )
            with _exclusive_lock(store.lock_path, environment.repository_root):
                _atomic_json_write(store.manifest_path, manifest)
                _atomic_json_write(store.timeline_path, _empty_timeline())
                store.screenshot_dir.mkdir(parents=True, exist_ok=True)
        return store

    @property
    def exists(self) -> bool:
        try:
            return self.manifest_path.is_file()
        except OSError:
            return False

    def _manifest_locked(self) -> dict[str, object]:
        raw = _read_json(self.manifest_path, max_bytes=_MAX_MANIFEST_BYTES)
        return _validate_manifest(raw, self.session_id)

    def _timeline_locked(self) -> tuple[list[TimelineEvent], bool]:
        raw = _read_json(self.timeline_path, max_bytes=_MAX_TIMELINE_BYTES)
        if not isinstance(raw, Mapping):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Хронология должна быть объектом")
        events = _validate_timeline(raw)
        truncated = bool(raw.get("truncated", False))
        if not isinstance(raw.get("truncated", False), bool):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Признак усечения хронологии имеет некорректный тип")
        return events, truncated

    def _write_manifest_locked(self, manifest: dict[str, object]) -> None:
        _validate_manifest(manifest, self.session_id)
        _atomic_json_write(self.manifest_path, manifest)

    def _set_health_locked(
        self,
        manifest: dict[str, object],
        reason: str,
        *,
        status: str = EVIDENCE_HEALTH_DEGRADED,
    ) -> None:
        if re.fullmatch(r"[a-z0-9_]{1,128}", reason) is None:
            raise EvidenceError("DEV_EVIDENCE_REASON_INVALID", "Причина состояния диагностики имеет неверный формат")
        health = _validate_health(manifest["evidence_health"])
        reasons = list(health["reasons"])
        if reason not in reasons:
            reasons.append(reason)
        health["reasons"] = reasons[:32]
        if health["status"] != EVIDENCE_HEALTH_CORRUPT:
            health["status"] = status
        manifest["evidence_health"] = health

    def mark_degraded(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            return
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                self._set_health_locked(manifest, reason)
                self._write_manifest_locked(manifest)
        except Exception:
            return

    def mark_corrupt(self, reason: str = "evidence_corrupt") -> None:
        """Зафиксировать повреждённое состояние, если манифест ещё читается."""

        if not isinstance(reason, str) or not reason or len(reason) > 128:
            return
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                self._set_health_locked(
                    manifest,
                    reason,
                    status=EVIDENCE_HEALTH_CORRUPT,
                )
                self._write_manifest_locked(manifest)
        except Exception:
            return

    def capture_log_boundary(self) -> None:
        """Зафиксировать границу журнала перед запуском корневого процесса gui.py."""

        try:
            log_path = _ensure_scoped_path(
                self.environment.log_file,
                self.environment.repository_root,
                label="путь журнала сессии",
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                with log_path.open("ab"):
                    pass
            identity = _file_identity(log_path)
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                manifest["logs"] = {
                    "source": _safe_log_source(),
                    "available": True,
                    "boundary_offset": int(log_path.stat().st_size),
                    "boundary_identity": identity.as_dict(),
                    "truncated": False,
                }
                self._write_manifest_locked(manifest)
        except Exception as exc:
            self.mark_degraded("log_boundary_lost")
            raise EvidenceError("DEV_EVIDENCE_LOG_BOUNDARY_LOST", "Не удалось зафиксировать границу журнала") from exc

    def _append_event_locked(
        self,
        manifest: dict[str, object],
        events: list[TimelineEvent],
        truncated: bool,
        event_type: str,
        fields: Mapping[str, object],
        timestamp: str,
    ) -> tuple[TimelineEvent, list[TimelineEvent], bool]:
        if event_type not in _EVENT_TYPES or not _SAFE_EVENT_TYPE.fullmatch(event_type):
            raise EvidenceError("DEV_EVIDENCE_EVENT_INVALID", "Неизвестное событие хронологии")
        if manifest.get("timeline") != _timeline_metadata(events, truncated=truncated):
            raise EvidenceCorrupt(
                "DEV_EVIDENCE_CORRUPT",
                "Метаданные хронологии не соответствуют событиям до записи",
            )
        event_timestamp = _utc_timestamp(timestamp)
        assert isinstance(event_timestamp, str)
        safe_fields = _validate_event_fields(fields)
        sequence = events[-1].sequence + 1 if events else 1
        if sequence > 10**12:
            raise EvidenceError("DEV_EVIDENCE_SEQUENCE_INVALID", "Порядковые номера хронологии достигли предела")
        event = TimelineEvent(sequence, event_timestamp, event_type, safe_fields)
        updated = [*events, event]
        if len(updated) > _MAX_TIMELINE_EVENTS:
            updated = updated[-_MAX_TIMELINE_EVENTS:]
            truncated = True
            self._set_health_locked(manifest, "timeline_truncated")
        payload: dict[str, object] = {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "events": [item.as_dict() for item in updated],
            "truncated": truncated,
        }
        _atomic_json_write(self.timeline_path, payload)
        manifest["timeline"] = _timeline_metadata(updated, truncated=truncated)
        self._write_manifest_locked(manifest)
        return event, updated, truncated

    def append_event(
        self,
        event_type: str,
        fields: Mapping[str, object] | None = None,
        *,
        timestamp: str | None = None,
    ) -> TimelineEvent:
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                events, truncated = self._timeline_locked()
                event, _events, _truncated = self._append_event_locked(
                    manifest,
                    events,
                    truncated,
                    event_type,
                    fields or {},
                    timestamp or self.now().astimezone(UTC).isoformat(),
                )
                return event
        except (EvidenceError, ValueError):
            raise
        except Exception as exc:
            self.mark_degraded("timeline_write_failed")
            raise EvidenceError("DEV_EVIDENCE_TIMELINE_WRITE_FAILED", "Событие хронологии не записано") from exc

    def record_error(
        self,
        exception: BaseException,
        *,
        phase: str,
        task: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        event_timestamp = timestamp or self.now().astimezone(UTC).isoformat()
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                events, truncated = self._timeline_locked()
                sequence = events[-1].sequence + 1 if events else 1
                error = _frame_error(
                    self.environment.repository_root,
                    exception,
                    phase=phase,
                    task=task,
                    timestamp=event_timestamp,
                    sequence=sequence,
                )
                event, _events, _truncated = self._append_event_locked(
                    manifest,
                    events,
                    truncated,
                    "runtime_error",
                    {
                        "exception_type": error["type"],
                        "phase": error["phase"],
                        "task": error["task"] or "",
                    },
                    event_timestamp,
                )
                error["sequence"] = event.sequence
                manifest["last_error"] = error
                self._write_manifest_locked(manifest)
        except (EvidenceError, ValueError):
            self.mark_degraded("error_record_failed")
            raise
        except Exception as exc:
            self.mark_degraded("error_record_failed")
            raise EvidenceError("DEV_EVIDENCE_ERROR_RECORD_FAILED", "Структурированная ошибка не записана") from exc

    def record_task(self, task: str, *, outcome: str | None = None, timestamp: str | None = None) -> None:
        task_name = _safe_selector(task)
        event_type = "task_started" if outcome is None else "task_finished"
        fields: dict[str, object] = {"task": task_name}
        if outcome is not None:
            fields["outcome"] = redact_text(outcome, max_length=64)
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                events, truncated = self._timeline_locked()
                _event, _events, _truncated = self._append_event_locked(
                    manifest,
                    events,
                    truncated,
                    event_type,
                    fields,
                    timestamp or self.now().astimezone(UTC).isoformat(),
                )
                if event_type == "task_started":
                    manifest["current_task"] = task_name
                elif manifest.get("current_task") == task_name:
                    manifest["current_task"] = None
                self._write_manifest_locked(manifest)
        except (EvidenceError, ValueError):
            raise
        except Exception as exc:
            self.mark_degraded("timeline_write_failed")
            raise EvidenceError("DEV_EVIDENCE_TIMELINE_WRITE_FAILED", "Текущее задание не записано") from exc

    def record_dependency(self, provenance: Mapping[str, object]) -> None:
        required = ("task", "required_by", "root", "reason", "sequence", "timestamp")
        if set(provenance) != set(required):
            raise EvidenceError("DEV_EVIDENCE_DEPENDENCY_INVALID", "Данные о зависимости неполны")
        for name in ("task", "required_by", "root", "reason"):
            _safe_selector(provenance[name])
        if provenance["reason"] not in {"dependency", "dependency_override"}:
            raise EvidenceError("DEV_EVIDENCE_DEPENDENCY_INVALID", "Причина зависимости неизвестна")
        sequence = provenance["sequence"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 < sequence <= 10**12
        ):
            raise EvidenceError("DEV_EVIDENCE_DEPENDENCY_INVALID", "Порядковый номер зависимости некорректен")
        timestamp = _utc_timestamp(provenance["timestamp"])
        assert isinstance(timestamp, str)
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                events, truncated = self._timeline_locked()
                if len(events) >= _MAX_TIMELINE_EVENTS and not truncated:
                    self._set_health_locked(manifest, "timeline_truncated")
                self._append_event_locked(
                    manifest,
                    events,
                    truncated,
                    "dependency_registered",
                    {
                        "task": provenance["task"],
                        "required_by": provenance["required_by"],
                        "root": provenance["root"],
                        "reason": provenance["reason"],
                        "dependency_sequence": sequence,
                    },
                    timestamp,
                )
                manifest["dependency_summary"] = {
                    "count": int(manifest["dependency_summary"].get("count", 0)) + 1,
                    "last": {
                        "task": provenance["task"],
                        "required_by": provenance["required_by"],
                        "root": provenance["root"],
                        "reason": provenance["reason"],
                        "sequence": sequence,
                        "timestamp": timestamp,
                    },
                }
                self._write_manifest_locked(manifest)
        except (EvidenceError, ValueError):
            raise
        except Exception as exc:
            self.mark_degraded("dependency_record_failed")
            raise EvidenceError("DEV_EVIDENCE_DEPENDENCY_WRITE_FAILED", "Данные о зависимости не записаны") from exc

    def finalize(
        self,
        *,
        stopped_at: str | None,
        cleanup_confirmed: bool,
        preserved: bool = False,
    ) -> None:
        timestamp = stopped_at or self.now().astimezone(UTC).isoformat()
        _utc_timestamp(timestamp)
        with _exclusive_lock(self.lock_path, self.environment.repository_root):
            manifest = self._manifest_locked()
            manifest["stopped_at"] = timestamp
            manifest["current_task"] = None
            manifest["cleanup"] = {
                "status": "preserved" if preserved else ("complete" if cleanup_confirmed else "pending"),
                "confirmed": cleanup_confirmed,
                "preserved": preserved,
                "updated_at": timestamp,
            }
            self._write_manifest_locked(manifest)

    def _read_screenshot_metadata_locked(self, screenshot_id: str) -> tuple[dict[str, object], bytes]:
        validate_session_id(screenshot_id)
        metadata_path = _ensure_scoped_path(
            self.screenshot_dir / f"{screenshot_id}.json",
            self.environment.repository_root,
            label="путь метаданных снимка экрана",
        )
        image_path = _ensure_scoped_path(
            self.screenshot_dir / f"{screenshot_id}.png",
            self.environment.repository_root,
            label="путь файла снимка экрана",
        )
        raw = _read_json(metadata_path, max_bytes=32 * 1024)
        if not isinstance(raw, Mapping) or set(raw) != {
            "screenshot_id",
            "timestamp",
            "mime",
            "width",
            "height",
            "byte_size",
            "sha256",
        }:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные снимка экрана имеют некорректную структуру")
        if raw.get("screenshot_id") != screenshot_id or raw.get("mime") != "image/png":
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные снимка экрана не соответствуют файлу")
        _utc_timestamp(raw.get("timestamp"))
        width, height, byte_size = raw.get("width"), raw.get("height"), raw.get("byte_size")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (width, height, byte_size)):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Размеры снимка экрана имеют некорректный тип")
        if byte_size < 1 or byte_size > _MAX_IMAGE_BYTES:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Размер снимка экрана выходит за допустимые границы")
        sha256 = raw.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "SHA-256 снимка экрана имеет некорректный формат")
        try:
            data = image_path.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Файл снимка экрана отсутствует") from exc
        if len(data) != byte_size or hashlib.sha256(data).hexdigest() != sha256:
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "SHA-256 или размер снимка экрана не совпадает")
        decoded_width, decoded_height = _validate_image_bytes(data)
        if (decoded_width, decoded_height) != (width, height):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Размеры снимка экрана не совпадают")
        return dict(raw), data

    def _persist_screenshot_bytes_locked(
        self,
        manifest: dict[str, object],
        data: bytes,
        *,
        timestamp: str,
        screenshot_id: str,
        width: int,
        height: int,
    ) -> dict[str, object]:
        validate_session_id(screenshot_id)
        _utc_timestamp(timestamp)
        if len(data) > _MAX_IMAGE_BYTES:
            raise EvidenceError("DEV_SCREENSHOT_TOO_LARGE", "PNG снимка экрана превышает допустимый размер")
        image_path = _ensure_scoped_path(
            self.screenshot_dir / f"{screenshot_id}.png",
            self.environment.repository_root,
            label="путь файла снимка экрана",
        )
        metadata_path = _ensure_scoped_path(
            self.screenshot_dir / f"{screenshot_id}.json",
            self.environment.repository_root,
            label="путь метаданных снимка экрана",
        )
        screenshots = dict(manifest["screenshots"])
        current_count = int(screenshots.get("count", 0))
        if current_count >= _MAX_SCREENSHOTS_PER_SESSION:
            raise EvidenceError(
                "DEV_SCREENSHOT_LIMIT",
                "Число снимков экрана для сессии достигло допустимого предела",
            )
        temporary = to_tmp_file(str(image_path))
        try:
            file_write(temporary, data)
            replace_tmp(temporary, str(image_path))
        finally:
            try:
                Path(temporary).unlink()
            except (FileNotFoundError, OSError):
                pass
        metadata = {
            "screenshot_id": screenshot_id,
            "timestamp": timestamp,
            "mime": "image/png",
            "width": width,
            "height": height,
            "byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        _atomic_json_write(metadata_path, metadata)
        screenshots["count"] = current_count + 1
        screenshots["latest"] = metadata
        manifest["screenshots"] = screenshots
        self._write_manifest_locked(manifest)
        return metadata

    def persist_screenshot(self, image: object, *, timestamp: str | None = None) -> EvidenceScreenshot:
        timestamp = timestamp or self.now().astimezone(UTC).isoformat()
        try:
            data, width, height = (
                (image, *_validate_image_bytes(image)) if isinstance(image, bytes) else _encode_png(image)
            )
            screenshot_id = str(uuid.uuid4())
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                metadata = self._persist_screenshot_bytes_locked(
                    manifest,
                    data,
                    timestamp=timestamp,
                    screenshot_id=screenshot_id,
                    width=width,
                    height=height,
                )
            return EvidenceScreenshot(
                DevResult(
                    True,
                    "DEV_SCREENSHOT_READY",
                    "Текущий кадр сохранён в диагностические данные",
                    DevSessionState.RUNNING.value,
                    self.session_id,
                    {"screenshot": metadata},
                ),
                data,
                "image/png",
            )
        except EvidenceError as exc:
            self.mark_degraded("screenshot_failed")
            return EvidenceScreenshot(
                DevResult(False, exc.code, str(exc), "failed", self.session_id, {}),
            )

    def request_screenshot(self, *, timeout: float = _SCREENSHOT_WAIT_SECONDS) -> EvidenceScreenshot:
        timeout = min(max(float(timeout), 0.1), _SCREENSHOT_WAIT_SECONDS)
        request_id = str(uuid.uuid4())
        created_at = self.now().astimezone(UTC).isoformat()
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                try:
                    existing = _read_json(self.request_path, max_bytes=16 * 1024)
                except EvidenceUnavailable:
                    existing = None
                if isinstance(existing, Mapping) and existing.get("status") in {"pending", "processing"}:
                    return EvidenceScreenshot(
                        DevResult(
                            False,
                            "DEV_SCREENSHOT_BUSY",
                            "Уже ожидается явный запрос снимка экрана",
                            "running",
                            self.session_id,
                            {},
                        )
                    )
                _atomic_json_write(
                    self.request_path,
                    {
                        "schema_version": 1,
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "status": "pending",
                        "created_at": created_at,
                    },
                )
                del manifest
        except EvidenceError as exc:
            self.mark_degraded("screenshot_failed")
            return EvidenceScreenshot(DevResult(False, exc.code, str(exc), "failed", self.session_id, {}))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with _exclusive_lock(self.lock_path, self.environment.repository_root):
                    request = _read_json(self.request_path, max_bytes=16 * 1024)
                    if not isinstance(request, Mapping) or request.get("request_id") != request_id:
                        return EvidenceScreenshot(
                            DevResult(
                                False,
                                "DEV_SCREENSHOT_REQUEST_CHANGED",
                                "Запрос снимка экрана изменился до ответа",
                                "failed",
                                self.session_id,
                                {},
                            )
                        )
                    status = request.get("status")
                    if status == "failed":
                        reason = request.get("reason_code", "DEV_SCREENSHOT_FAILED")
                        return EvidenceScreenshot(
                            DevResult(False, str(reason), "Рабочий процесс не смог сохранить снимок экрана", "failed", self.session_id, {})
                        )
                    if status == "completed":
                        screenshot_id = request.get("screenshot_id")
                        if not isinstance(screenshot_id, str):
                            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Ответ снимка экрана не содержит идентификатор")
                        metadata, data = self._read_screenshot_metadata_locked(screenshot_id)
                        return EvidenceScreenshot(
                            DevResult(
                                True,
                                "DEV_SCREENSHOT_READY",
                                "Текущий кадр сохранён в диагностические данные",
                                "running",
                                self.session_id,
                                {"screenshot": metadata},
                            ),
                            data,
                            "image/png",
                        )
            except EvidenceUnavailable:
                pass
            except EvidenceError as exc:
                self.mark_degraded("screenshot_failed")
                return EvidenceScreenshot(DevResult(False, exc.code, str(exc), "failed", self.session_id, {}))
            time.sleep(_SCREENSHOT_POLL_SECONDS)
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                request = _read_json(self.request_path, max_bytes=16 * 1024)
                if isinstance(request, Mapping) and request.get("request_id") == request_id:
                    _atomic_json_write(
                        self.request_path,
                        {
                            "schema_version": 1,
                            "session_id": self.session_id,
                            "request_id": request_id,
                            "status": "expired",
                            "created_at": created_at,
                        },
                    )
        except Exception:
            pass
        self.mark_degraded("screenshot_failed")
        return EvidenceScreenshot(
            DevResult(
                False,
                "DEV_SCREENSHOT_TIMEOUT",
                "Рабочий процесс не предоставил текущий кадр за отведённый временной лимит",
                "failed",
                self.session_id,
                {},
            )
        )

    def serve_pending_screenshot(self, image: object) -> None:
        """Обработать на стороне рабочего процесса уже созданный явный запрос."""

        try:
            if not self.request_path.exists():
                return
        except OSError:
            return
        try:
            with _exclusive_lock(self.lock_path, self.environment.repository_root):
                manifest = self._manifest_locked()
                session_state = manifest.get("stopped_at")
                if session_state is not None:
                    return
                request = _read_json(self.request_path, max_bytes=16 * 1024)
                if not isinstance(request, Mapping) or request.get("session_id") != self.session_id:
                    return
                if request.get("status") != "pending":
                    return
                request_id = request.get("request_id")
                try:
                    request_id = validate_session_id(request_id)
                except ValueError:
                    return
                _atomic_json_write(
                    self.request_path,
                    {
                        "schema_version": 1,
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "status": "processing",
                        "created_at": request.get("created_at"),
                    },
                )
                data, width, height = (
                    (image, *_validate_image_bytes(image)) if isinstance(image, bytes) else _encode_png(image)
                )
                metadata = self._persist_screenshot_bytes_locked(
                    manifest,
                    data,
                    timestamp=self.now().astimezone(UTC).isoformat(),
                    screenshot_id=request_id,
                    width=width,
                    height=height,
                )
                _atomic_json_write(
                    self.request_path,
                    {
                        "schema_version": 1,
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "status": "completed",
                        "created_at": request.get("created_at"),
                        "screenshot_id": metadata["screenshot_id"],
                    },
                )
        except Exception as exc:
            try:
                with _exclusive_lock(self.lock_path, self.environment.repository_root):
                    request = _read_json(self.request_path, max_bytes=16 * 1024)
                    if isinstance(request, Mapping) and request.get("status") in {"pending", "processing"}:
                        _atomic_json_write(
                            self.request_path,
                            {
                                "schema_version": 1,
                                "session_id": self.session_id,
                                "request_id": request.get("request_id"),
                                "status": "failed",
                                "reason_code": (
                                    exc.code if isinstance(exc, EvidenceError) else "DEV_SCREENSHOT_FAILED"
                                ),
                                "created_at": request.get("created_at"),
                            },
                        )
                    manifest = self._manifest_locked()
                    self._set_health_locked(manifest, "screenshot_failed")
                    self._write_manifest_locked(manifest)
            except Exception:
                return

    def timeline_page(self, *, after_sequence: int = 0, limit: int = 100) -> dict[str, object]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise EvidenceError("DEV_EVIDENCE_SEQUENCE_INVALID", "after_sequence должен быть неотрицательным целым числом")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_TIMELINE_EVENTS:
            raise EvidenceError("DEV_EVIDENCE_LIMIT_INVALID", "Ограничение хронологии выходит за допустимые границы")
        with _exclusive_lock(self.lock_path, self.environment.repository_root):
            manifest = self._manifest_locked()
            events, truncated = self._timeline_locked()
        if manifest["timeline"] != _timeline_metadata(events, truncated=truncated):
            raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные хронологии не соответствуют событиям")
        selected = [event for event in events if event.sequence > after_sequence]
        page = selected[:limit]
        more = len(selected) > len(page)
        first_sequence = events[0].sequence if events else None
        return {
            "session_id": self.session_id,
            "events": [event.as_dict() for event in page],
            "next_after_sequence": page[-1].sequence if page else after_sequence,
            "more": more,
            "truncated": truncated or (first_sequence is not None and after_sequence < first_sequence - 1),
            "health": dict(manifest["evidence_health"]),
        }

    def _cursor(self, *, offset: int, identity: _FileIdentity) -> str:
        payload = {
            "session_id": self.session_id,
            "offset": offset,
            "identity": identity.as_dict(),
        }
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_cursor(self, value: str) -> tuple[int, _FileIdentity]:
        if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR_LENGTH:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала имеет некорректный формат")
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала невозможно проверить") from exc
        if not isinstance(payload, Mapping) or set(payload) != {"session_id", "offset", "identity"}:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала имеет неизвестные поля")
        if payload.get("session_id") != self.session_id:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала принадлежит другой сессии")
        offset = payload.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Смещение курсора журнала некорректно")
        try:
            identity = _FileIdentity.from_value(payload.get("identity"))
        except EvidenceCorrupt as exc:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор содержит повреждённый идентификатор файла") from exc
        if identity is None:
            raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала не содержит идентификатор файла")
        return offset, identity

    def logs_page(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, object]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LOG_PAGE_LINES:
            raise EvidenceError("DEV_EVIDENCE_LIMIT_INVALID", "Ограничение журнала выходит за допустимые границы")
        with _exclusive_lock(self.lock_path, self.environment.repository_root):
            manifest = self._manifest_locked()
            logs = manifest["logs"]
            if not isinstance(logs, Mapping) or not logs.get("available"):
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                return {
                    "session_id": self.session_id,
                    "items": [],
                    "next_cursor": None,
                    "more": False,
                    "truncated": False,
                    "health": dict(manifest["evidence_health"]),
                }
            boundary_offset = logs.get("boundary_offset")
            boundary_identity = _FileIdentity.from_value(logs.get("boundary_identity"))
            if isinstance(boundary_offset, bool) or not isinstance(boundary_offset, int) or boundary_identity is None:
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                raise EvidenceError("DEV_EVIDENCE_LOG_BOUNDARY_LOST", "Граница журнала повреждена")
            log_path = _ensure_scoped_path(
                self.environment.log_file,
                self.environment.repository_root,
                label="путь журнала сессии",
            )
            try:
                current_identity = _file_identity(log_path)
                current_size = log_path.stat().st_size
            except EvidenceError:
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                return {
                    "session_id": self.session_id,
                    "items": [],
                    "next_cursor": None,
                    "more": False,
                    "truncated": False,
                    "health": dict(manifest["evidence_health"]),
                }
            if not current_identity.same_file(boundary_identity) or current_size < boundary_offset:
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                raise EvidenceError("DEV_EVIDENCE_LOG_BOUNDARY_LOST", "Файл журнала был заменён или обрезан")
            offset = boundary_offset
            if cursor is not None:
                offset, cursor_identity = self._decode_cursor(cursor)
                if (
                    not cursor_identity.same_file(boundary_identity)
                    or offset < boundary_offset
                    or offset > current_size
                ):
                    raise EvidenceError("DEV_EVIDENCE_CURSOR_INVALID", "Курсор журнала выходит за границу сессии")

            items: list[dict[str, object]] = []
            page_bytes = 0
            truncated = False
            try:
                with log_path.open("rb") as handle:
                    handle.seek(offset)
                    for _index in range(limit):
                        line_offset = handle.tell()
                        raw_line = handle.readline(_MAX_LOG_LINE_BYTES + 1)
                        if not raw_line:
                            break
                        if items and handle.tell() - offset > _MAX_LOG_PAGE_BYTES:
                            handle.seek(line_offset)
                            truncated = True
                            break
                        line_truncated = len(raw_line) > _MAX_LOG_LINE_BYTES and not raw_line.endswith(b"\n")
                        if len(raw_line) > _MAX_LOG_LINE_BYTES:
                            raw_line = raw_line[:_MAX_LOG_LINE_BYTES]
                        text_value = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        safe_text = redact_text(text_value, max_length=_MAX_LOG_LINE_BYTES)
                        safe_text_bytes = len(safe_text.encode("utf-8"))
                        if items and page_bytes + safe_text_bytes > _MAX_LOG_PAGE_BYTES:
                            handle.seek(line_offset)
                            truncated = True
                            break
                        items.append(
                            {
                                "text": safe_text,
                                "truncated": line_truncated,
                            }
                        )
                        page_bytes += safe_text_bytes
                        truncated = truncated or line_truncated
                    next_offset = handle.tell()
                    probe = handle.read(1)
                    more = bool(probe)
            except OSError as exc:
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                raise EvidenceError("DEV_EVIDENCE_LOG_READ_FAILED", "Файл журнала невозможно прочитать") from exc
            try:
                after_identity = _file_identity(log_path)
                after_size = log_path.stat().st_size
            except EvidenceError:
                after_identity = current_identity
                after_size = current_size
                more = False
                truncated = True
            if (
                not after_identity.same_file(current_identity)
                or after_size < boundary_offset
                or after_size < next_offset
            ):
                self._set_health_locked(manifest, "log_boundary_lost")
                self._write_manifest_locked(manifest)
                truncated = True
                more = False
            previous_truncated = bool(logs.get("truncated"))
            if truncated and not previous_truncated:
                updated_logs = dict(logs)
                updated_logs["truncated"] = True
                manifest["logs"] = updated_logs
                self._write_manifest_locked(manifest)
            truncated = truncated or previous_truncated
            next_cursor = self._cursor(offset=next_offset, identity=boundary_identity) if more else None
            return {
                "session_id": self.session_id,
                "items": items,
                "next_cursor": next_cursor,
                "more": more,
                "truncated": truncated,
                "health": dict(manifest["evidence_health"]),
            }

    def summary(self, *, active_owned: bool = False) -> dict[str, object]:
        with _exclusive_lock(self.lock_path, self.environment.repository_root):
            manifest = self._manifest_locked()
            events, truncated = self._timeline_locked()
            if manifest["timeline"] != _timeline_metadata(events, truncated=truncated):
                raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные хронологии не соответствуют событиям")
            if truncated:
                self._set_health_locked(manifest, "timeline_truncated")
                manifest["timeline"] = _timeline_metadata(events, truncated=True)
                self._write_manifest_locked(manifest)
            screenshots = manifest["screenshots"]
            if isinstance(screenshots, Mapping) and screenshots.get("latest") is not None:
                latest = screenshots["latest"]
                if not isinstance(latest, Mapping):
                    raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Последний снимок экрана имеет неверную структуру")
                screenshot_id = latest.get("screenshot_id")
                if not isinstance(screenshot_id, str):
                    raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Последний снимок экрана не содержит идентификатор")
                actual, _data = self._read_screenshot_metadata_locked(screenshot_id)
                if actual != dict(latest):
                    raise EvidenceCorrupt("DEV_EVIDENCE_CORRUPT", "Метаданные последнего снимка экрана не совпадают")
        current_task = manifest.get("current_task") if active_owned and manifest.get("stopped_at") is None else None
        logs = manifest["logs"]
        started_at = datetime.fromisoformat(manifest["started_at"])
        stopped_at = manifest["stopped_at"]
        end_at = datetime.fromisoformat(stopped_at) if isinstance(stopped_at, str) else self.now()
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)
        duration_seconds = max(0, int((end_at.astimezone(UTC) - started_at).total_seconds()))
        return {
            "session_id": self.session_id,
            "profile": DEV_PROFILE,
            "lifecycle": {
                "created_at": manifest["created_at"],
                "started_at": manifest["started_at"],
                "stopped_at": manifest["stopped_at"],
                "duration_seconds": duration_seconds,
            },
            "roots": list(manifest["root_tasks"]),
            "excluded": list(manifest["excluded_tasks"]),
            "current_task": current_task,
            "dependency_summary": dict(manifest["dependency_summary"]),
            "git_snapshot": dict(manifest["git_snapshot"]),
            "evidence_health": dict(manifest["evidence_health"]),
            "timeline": dict(manifest["timeline"]),
            "logs": {
                "available": bool(logs.get("available")) if isinstance(logs, Mapping) else False,
                "source": _safe_log_source(),
                "truncated": bool(logs.get("truncated")) if isinstance(logs, Mapping) else False,
            },
            "screenshots": {
                "count": int(screenshots.get("count", 0)) if isinstance(screenshots, Mapping) else 0,
                "latest": screenshots.get("latest") if isinstance(screenshots, Mapping) else None,
            },
            "last_error": manifest["last_error"],
            "cleanup": dict(manifest["cleanup"]),
        }

    @classmethod
    def for_session(cls, environment: DevEnvironment, session_id: str) -> EvidenceStore:
        return cls(environment, validate_session_id(session_id))

    @classmethod
    def prune(
        cls,
        environment: DevEnvironment,
        *,
        active_session_id: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> bool:
        """Удалить только старые каталоги собственных сессий, не переходя по ссылкам."""

        now_value = (now or (lambda: datetime.now(UTC)))()
        cutoff = now_value.timestamp() - _MAX_RETENTION_AGE_SECONDS
        root = _ensure_scoped_path(environment.evidence_root, environment.repository_root, label="корень диагностики")
        if not root.exists():
            return True
        active = validate_session_id(active_session_id) if active_session_id is not None else None
        candidates: list[tuple[Path, float, int]] = []
        try:
            with _exclusive_lock(environment.evidence_lock_file, environment.repository_root):
                for entry in os.scandir(root):
                    path = Path(entry.path)
                    if _is_reparse_point(path) or not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        session_id = validate_session_id(path.name)
                        store = cls.for_session(environment, session_id)
                        store._manifest_locked()
                        stat_result = path.stat()
                        size = _safe_tree_size(path, environment.repository_root)
                    except (EvidenceError, OSError, ValueError):
                        continue
                    candidates.append((path, stat_result.st_mtime, size))
                candidates.sort(key=lambda item: item[1], reverse=True)
                total = sum(item[2] for item in candidates)
                keep_count = 0
                success = True
                for path, modified, size in candidates:
                    is_active = active is not None and path.name == active
                    too_old = modified < cutoff
                    over_count = keep_count >= _MAX_RETENTION_SESSIONS
                    over_bytes = total > _MAX_RETENTION_BYTES
                    if is_active or not (too_old or over_count or over_bytes):
                        keep_count += 1
                        continue
                    if not _safe_remove_tree(path, environment.repository_root):
                        success = False
                        keep_count += 1
                        continue
                    total -= size
                return success
        except (EvidenceError, OSError, TimeoutError):
            return False


def _safe_tree_size(path: Path, repository_root: Path) -> int:
    _ensure_scoped_path(path, repository_root, label="путь хранения диагностики")
    if _is_reparse_point(path):
        raise EvidenceError("DEV_EVIDENCE_UNSAFE_PATH", "Путь хранения диагностики является ссылкой")
    total = 0
    for entry in os.scandir(path):
        child = Path(entry.path)
        if _is_reparse_point(child):
            raise EvidenceError("DEV_EVIDENCE_UNSAFE_PATH", "Путь хранения диагностики содержит ссылку")
        if entry.is_dir(follow_symlinks=False):
            total += _safe_tree_size(child, repository_root)
        elif entry.is_file(follow_symlinks=False):
            total += int(entry.stat(follow_symlinks=False).st_size)
    return total


def _safe_remove_tree(path: Path, repository_root: Path) -> bool:
    try:
        _ensure_scoped_path(path, repository_root, label="путь удаления диагностики")
        if _is_reparse_point(path):
            return False
        for entry in os.scandir(path):
            child = Path(entry.path)
            if _is_reparse_point(child):
                return False
            if entry.is_dir(follow_symlinks=False):
                if not _safe_remove_tree(child, repository_root):
                    return False
            elif entry.is_file(follow_symlinks=False):
                child.unlink()
            else:
                return False
        path.rmdir()
        return True
    except (EvidenceError, OSError):
        return False


_ACTIVE_CACHE_LOCK = threading.Lock()
_ACTIVE_CACHE: tuple[
    str,
    str,
    float,
    tuple[int, int, int, int] | None,
    tuple[int, int, int, int] | None,
    EvidenceStore | None,
] | None = None


def _path_marker(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_size),
    )


def _read_active_session(environment: DevEnvironment) -> DevSession | None:
    try:
        state_path = _ensure_scoped_path(
            environment.state_file,
            environment.repository_root,
            label="путь состояния активной сессии",
        )
        raw = state_path.read_bytes()
        if len(raw) > 64 * 1024:
            return None
        payload = json.loads(raw.decode("utf-8"))
        session = DevSession.from_dict(payload)
        if not _same_path(session.repository_root, environment.repository_root):
            return None
        return session
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def active_evidence_store() -> EvidenceStore | None:
    """Проверить политику, жизненный цикл и корень, а не доверять одному env."""

    global _ACTIVE_CACHE
    session_id = os.environ.get(TASK_POLICY_SESSION_ENV)
    configured_root = os.environ.get(TASK_POLICY_ROOT_ENV)
    configured_policy = os.environ.get(TASK_POLICY_FILE_ENV)
    if not isinstance(session_id, str) or not isinstance(configured_root, str) or not isinstance(configured_policy, str):
        return None
    try:
        session_id = validate_session_id(session_id)
        environment = DevEnvironment.current()
        if not _same_path(configured_root, environment.repository_root):
            return None
        if not _same_path(configured_policy, environment.task_policy_file):
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    cache_key = (session_id, _absolute_path(environment.repository_root))
    state_marker = _path_marker(environment.state_file)
    policy_marker = _path_marker(environment.task_policy_file)
    now_monotonic = time.monotonic()
    with _ACTIVE_CACHE_LOCK:
        if (
            _ACTIVE_CACHE is not None
            and _ACTIVE_CACHE[0] == cache_key[0]
            and _ACTIVE_CACHE[1] == cache_key[1]
            and now_monotonic < _ACTIVE_CACHE[2]
            and _ACTIVE_CACHE[3] == state_marker
            and _ACTIVE_CACHE[4] == policy_marker
        ):
            return _ACTIVE_CACHE[5]
    store: EvidenceStore | None = None
    try:
        session = _read_active_session(environment)
        if (
            session is None
            or session.session_id != session_id
            or not session.is_task_aware
            or session.state not in {DevSessionState.STARTING, DevSessionState.RUNNING}
        ):
            raise ValueError("активный жизненный цикл не совпадает")
        policy = TaskPolicyStore(environment).read()
        if policy is None or policy.state != TASK_POLICY_ACTIVE or policy.session_id != session_id:
            raise ValueError("активная политика не совпадает")
        store = EvidenceStore.for_session(environment, session_id)
        if not store.exists:
            store = None
    except (EvidenceError, OSError, RuntimeError, TypeError, ValueError):
        store = None
    with _ACTIVE_CACHE_LOCK:
        _ACTIVE_CACHE = (
            cache_key[0],
            cache_key[1],
            now_monotonic + 0.5,
            state_marker,
            policy_marker,
            store,
        )
    return store


def _active_store_for_config(config_name: object) -> EvidenceStore | None:
    if config_name != DEV_PROFILE:
        return None
    return active_evidence_store()


def record_task_started(config_name: object, task: object) -> None:
    store = _active_store_for_config(config_name)
    if store is None or not isinstance(task, str):
        return
    try:
        policy = TaskPolicyStore(store.environment).read()
        if policy is None or task not in policy.allowed_tasks:
            return
        store.record_task(task)
    except Exception:
        store.mark_degraded("timeline_write_failed")


def record_task_finished(config_name: object, task: object, outcome: object) -> None:
    store = _active_store_for_config(config_name)
    if store is None or not isinstance(task, str) or not isinstance(outcome, str):
        return
    try:
        policy = TaskPolicyStore(store.environment).read()
        if policy is None or task not in policy.allowed_tasks:
            return
        store.record_task(task, outcome=outcome)
    except Exception:
        store.mark_degraded("timeline_write_failed")


def record_dependency_registered(
    config_name: object,
    *,
    caller: object,
    target: object,
    timestamp: object,
) -> None:
    store = _active_store_for_config(config_name)
    if store is None or not all(isinstance(value, str) for value in (caller, target, timestamp)):
        return
    try:
        policy = TaskPolicyStore(store.environment).read()
        if policy is None or policy.state != TASK_POLICY_ACTIVE or policy.session_id != store.session_id:
            return
        matching = [
            item
            for item in policy.dependencies
            if item.required_by == caller and item.task == target and item.timestamp == timestamp
        ]
        if not matching:
            store.mark_degraded("dependency_record_failed")
            return
        store.record_dependency(matching[-1].as_dict())
    except Exception:
        store.mark_degraded("dependency_record_failed")


def record_runtime_error(
    config_name: object,
    exception: BaseException,
    *,
    phase: str,
    task: object = None,
) -> None:
    store = _active_store_for_config(config_name)
    if store is None:
        return
    try:
        task_name = _safe_selector(task) if isinstance(task, str) else None
    except EvidenceError:
        task_name = None
    try:
        store.record_error(exception, phase=phase, task=task_name)
    except Exception:
        store.mark_degraded("error_record_failed")


def serve_pending_screenshot(image: object) -> None:
    try:
        store = active_evidence_store()
        if store is None:
            return
        store.serve_pending_screenshot(image)
    except Exception:
        return


__all__ = [
    "EVIDENCE_HEALTH_COMPLETE",
    "EVIDENCE_HEALTH_CORRUPT",
    "EVIDENCE_HEALTH_DEGRADED",
    "EVIDENCE_HEALTH_UNAVAILABLE",
    "EvidenceCorrupt",
    "EvidenceError",
    "EvidenceScreenshot",
    "EvidenceStore",
    "EvidenceUnavailable",
    "GitSnapshot",
    "TimelineEvent",
    "capture_git_snapshot",
    "record_dependency_registered",
    "record_runtime_error",
    "record_task_finished",
    "record_task_started",
    "serve_pending_screenshot",
    "validate_session_id",
]
