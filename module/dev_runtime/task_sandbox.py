"""Типизированный каталог задач и fail-closed policy для Dev Runtime профиля ``ap``."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from deploy.atomic import atomic_remove, file_write, replace_tmp, to_tmp_file
from module.config.time_sentinel import LEGACY_DEFAULT_TIME
from module.dev_runtime.contracts import DEV_PROFILE, DevEnvironment, DevSession

TASK_POLICY_SCHEMA_VERSION = 1
TASK_POLICY_ACTIVE = "active"
TASK_POLICY_CLEANUP_PENDING = "cleanup_pending"
TASK_POLICY_PRESERVED = "preserved"
TASK_POLICY_STATES = frozenset(
    {TASK_POLICY_ACTIVE, TASK_POLICY_CLEANUP_PENDING, TASK_POLICY_PRESERVED}
)
TASK_POLICY_SESSION_ENV = "AZURPILOT_DEV_SESSION_ID"
TASK_POLICY_ROOT_ENV = "AZURPILOT_DEV_REPOSITORY_ROOT"
TASK_POLICY_FILE_ENV = "AZURPILOT_DEV_POLICY_FILE"
SCHEDULER_RESET_TIME = LEGACY_DEFAULT_TIME.strftime("%Y-%m-%d %H:%M:%S")

_MAX_PROFILE_BYTES = 1024 * 1024
_MAX_POLICY_BYTES = 256 * 1024
_MAX_SELECTOR_LENGTH = 128
_MAX_SESSION_LENGTH = 128
_POLICY_LOCK_TIMEOUT = 10.0
_POLICY_LOCK_RETRY_INTERVAL = 0.05
_policy_thread_lock = threading.RLock()


class TaskSandboxError(ValueError):
    """Машиночитаемая ошибка проверки каталога или policy state."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": str(self), **self.details}


def _safe_selector(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TaskSandboxError(
            "DEV_TASK_SELECTOR_INVALID",
            f"{field} должен быть непустой строкой",
            field=field,
        )
    if len(value) > _MAX_SELECTOR_LENGTH or value != value.strip():
        raise TaskSandboxError(
            "DEV_TASK_SELECTOR_INVALID",
            f"{field} имеет недопустимую длину или пробелы",
            field=field,
        )
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or ".." in value
    ):
        raise TaskSandboxError(
            "DEV_TASK_SELECTOR_UNSAFE",
            f"{field} содержит недопустимый путь или управляющий символ",
            field=field,
        )
    return value


def _safe_session_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SESSION_LENGTH:
        raise TaskSandboxError("DEV_TASK_SESSION_INVALID", "session_id имеет недопустимый формат")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\x00" in value:
        raise TaskSandboxError("DEV_TASK_SESSION_INVALID", "session_id содержит управляющий символ")
    return value


def _safe_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise TaskSandboxError("DEV_TASK_TIMESTAMP_INVALID", f"{field} имеет недопустимый формат")
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\x00" in value:
        raise TaskSandboxError("DEV_TASK_TIMESTAMP_INVALID", f"{field} содержит управляющий символ")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise TaskSandboxError(
            "DEV_TASK_TIMESTAMP_INVALID", f"{field} не является timestamp"
        ) from exc
    return value


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        try:
            return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
                os.path.abspath(os.fspath(right))
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            return False


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            getattr(path, "is_junction", lambda: False)()
        )
    except OSError as exc:
        raise TaskSandboxError(
            "DEV_TASK_STATE_UNREADABLE", "Нельзя проверить ссылку или junction"
        ) from exc


def _ensure_scoped_path(path: Path, repository_root: Path, *, label: str) -> Path:
    """Проверить, что путь и его существующие parents остаются внутри checkout."""

    try:
        root = Path(os.path.abspath(repository_root))
        candidate = Path(os.path.abspath(path))
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TaskSandboxError(
            "DEV_TASK_STATE_FOREIGN_PATH",
            f"{label} выходит за пределы рабочей копии",
        ) from exc

    current = root
    for component in relative.parts:
        current /= component
        if _is_reparse_point(current):
            raise TaskSandboxError(
                "DEV_TASK_STATE_UNSAFE_PATH",
                f"{label} проходит через ссылку или junction",
            )
    return candidate


def _read_json(path: Path, *, max_bytes: int, missing_ok: bool) -> object | None:
    if _is_reparse_point(path):
        raise TaskSandboxError(
            "DEV_TASK_STATE_UNSAFE_PATH", "Состояние Dev Runtime не должно быть ссылкой или junction"
        )
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise TaskSandboxError("DEV_TASK_STATE_MISSING", f"Файл состояния отсутствует: {path.name}")
    except OSError as exc:
        raise TaskSandboxError(
            "DEV_TASK_STATE_UNREADABLE", "Файл состояния невозможно прочитать"
        ) from exc
    if len(raw) > max_bytes:
        raise TaskSandboxError("DEV_TASK_STATE_TOO_LARGE", "Файл состояния превышает допустимый размер")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TaskSandboxError("DEV_TASK_STATE_CORRUPT", "Файл состояния содержит некорректный JSON") from exc


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    if _is_reparse_point(path):
        raise TaskSandboxError(
            "DEV_TASK_STATE_UNSAFE_PATH", "Состояние Dev Runtime не должно быть ссылкой или junction"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    target = str(path)
    temporary = to_tmp_file(target)
    try:
        file_write(
            temporary,
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        )
        replace_tmp(temporary, target)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


@contextmanager
def _exclusive_policy_lock(path: Path) -> Iterator[None]:
    if _is_reparse_point(path):
        raise TaskSandboxError(
            "DEV_TASK_STATE_UNSAFE_PATH", "Блокировка task policy не должна быть ссылкой или junction"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + _POLICY_LOCK_TIMEOUT
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
                        raise TimeoutError("Истекло время ожидания блокировки task policy")
                    time.sleep(_POLICY_LOCK_RETRY_INTERVAL)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время ожидания блокировки task policy")
                    time.sleep(_POLICY_LOCK_RETRY_INTERVAL)
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


def _selector_values(value: Iterable[str] | str | None, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values: Iterable[object] = (value,)
    elif isinstance(value, (Mapping, bytes, bytearray)):
        raise TaskSandboxError(
            "DEV_TASK_SELECTOR_INVALID", f"{field} должен быть строкой или последовательностью строк", field=field
        )
    else:
        if not isinstance(value, Iterable):
            raise TaskSandboxError(
                "DEV_TASK_SELECTOR_INVALID", f"{field} должен быть строкой или последовательностью строк", field=field
            )
        raw_values = value
    normalized: list[str] = []
    for item in raw_values:
        normalized.append(_safe_selector(item, field=field))
    return tuple(sorted(set(normalized)))


def _policy_selector_values(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", f"{field} должен быть массивом")
    normalized = tuple(_safe_selector(item, field=field) for item in value)
    if len(set(normalized)) != len(normalized):
        raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", f"{field} содержит дубликаты")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class TaskDescriptor:
    """Публичное несекретное описание одной schedulable config section."""

    section: str
    command: str
    enabled: bool
    next_run: str

    def as_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "command": self.command,
            "enabled": self.enabled,
            "next_run": self.next_run,
        }


@dataclass(frozen=True, slots=True)
class TaskCatalog:
    tasks: tuple[TaskDescriptor, ...]

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(task.command for task in self.tasks)

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": DEV_PROFILE,
            "tasks": [task.as_dict() for task in self.tasks],
        }

    def contains(self, command: str) -> bool:
        return command in self.commands

    @classmethod
    def from_path(
        cls, path: Path, *, repository_root: Path | None = None
    ) -> TaskCatalog:
        if repository_root is not None:
            path = _ensure_scoped_path(path, repository_root, label="profile path")
        payload = _read_json(path, max_bytes=_MAX_PROFILE_BYTES, missing_ok=False)
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: object) -> TaskCatalog:
        if not isinstance(payload, Mapping):
            raise TaskSandboxError("DEV_TASK_CATALOG_INVALID", "config/ap.json должен быть JSON-объектом")
        descriptors: list[TaskDescriptor] = []
        for section, section_payload in payload.items():
            if not isinstance(section, str):
                raise TaskSandboxError("DEV_TASK_CATALOG_INVALID", "Имя секции task должно быть строкой")
            if not isinstance(section_payload, Mapping):
                continue
            if "Scheduler" not in section_payload:
                continue
            _safe_selector(section, field="section")
            scheduler = section_payload["Scheduler"]
            if not isinstance(scheduler, Mapping):
                raise TaskSandboxError(
                    "DEV_TASK_SCHEDULER_MALFORMED",
                    f"Секция {section} содержит некорректный Scheduler",
                    task=section,
                )
            enabled = scheduler.get("Enable")
            command = scheduler.get("Command")
            next_run = scheduler.get("NextRun")
            if not isinstance(enabled, bool):
                raise TaskSandboxError(
                    "DEV_TASK_SCHEDULER_MALFORMED",
                    f"Секция {section} содержит некорректный Scheduler.Enable",
                    task=section,
                )
            if not isinstance(command, str) or not command:
                raise TaskSandboxError(
                    "DEV_TASK_COMMAND_EMPTY",
                    f"Секция {section} содержит пустой Scheduler.Command",
                    task=section,
                )
            command = _safe_selector(command, field="Scheduler.Command")
            if command != section:
                raise TaskSandboxError(
                    "DEV_TASK_COMMAND_CONFLICT",
                    f"Scheduler.Command секции {section} не совпадает с именем секции",
                    task=section,
                    command=command,
                )
            if not isinstance(next_run, str) or not next_run:
                raise TaskSandboxError(
                    "DEV_TASK_SCHEDULER_MALFORMED",
                    f"Секция {section} содержит некорректный Scheduler.NextRun",
                    task=section,
                )
            try:
                datetime.fromisoformat(next_run.replace("T", " "))
            except ValueError as exc:
                raise TaskSandboxError(
                    "DEV_TASK_SCHEDULER_MALFORMED",
                    f"Секция {section} содержит нераспознаваемый Scheduler.NextRun",
                    task=section,
                ) from exc
            descriptors.append(
                TaskDescriptor(
                    section=section,
                    command=command,
                    enabled=enabled,
                    next_run=next_run,
                )
            )

        descriptors.sort(key=lambda item: item.command)
        seen: set[str] = set()
        for descriptor in descriptors:
            folded = descriptor.command.casefold()
            if folded in seen:
                raise TaskSandboxError(
                    "DEV_TASK_COMMAND_DUPLICATE",
                    f"Каталог содержит неоднозначную task command: {descriptor.command}",
                    task=descriptor.command,
                )
            seen.add(folded)
        if not descriptors:
            raise TaskSandboxError(
                "DEV_TASK_CATALOG_EMPTY", "В config/ap.json не найдено schedulable task sections"
            )
        return cls(tasks=tuple(descriptors))


def read_profile_payload(
    path: Path, *, repository_root: Path | None = None
) -> object:
    """Прочитать raw profile без миграций или записей через ConfigUpdater."""

    if repository_root is not None:
        path = _ensure_scoped_path(path, repository_root, label="profile path")
    return _read_json(path, max_bytes=_MAX_PROFILE_BYTES, missing_ok=False)


def write_profile_payload(
    path: Path, payload: object, *, repository_root: Path | None = None
) -> None:
    if not isinstance(payload, Mapping):
        raise TaskSandboxError("DEV_TASK_PROFILE_INVALID", "Профиль должен быть JSON-объектом")
    if repository_root is not None:
        path = _ensure_scoped_path(path, repository_root, label="profile path")
    _atomic_json_write(path, dict(payload))


@dataclass(frozen=True, slots=True)
class TaskPlan:
    profile: str
    root_tasks: tuple[str, ...]
    excluded_tasks: tuple[str, ...]
    catalog: TaskCatalog

    @property
    def allowed_roots(self) -> tuple[str, ...]:
        return self.root_tasks

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "root_tasks": list(self.root_tasks),
            "excluded_tasks": list(self.excluded_tasks),
            "catalog": list(self.catalog.commands),
        }

    @classmethod
    def from_catalog(
        cls,
        catalog: TaskCatalog,
        root_tasks: Iterable[str] | str | None,
        excluded_tasks: Iterable[str] | str | None,
    ) -> TaskPlan:
        roots = _selector_values(root_tasks, field="root_tasks")
        excluded = _selector_values(excluded_tasks, field="excluded_tasks")
        known = set(catalog.commands)
        unknown_roots = sorted(set(roots) - known)
        unknown_excluded = sorted(set(excluded) - known)
        if unknown_roots:
            raise TaskSandboxError(
                "DEV_TASK_UNKNOWN_ROOT",
                "root task отсутствует в каталоге",
                tasks=unknown_roots,
            )
        if unknown_excluded:
            raise TaskSandboxError(
                "DEV_TASK_UNKNOWN_EXCLUDED",
                "excluded task отсутствует в каталоге",
                tasks=unknown_excluded,
            )
        conflict = sorted(set(roots) & set(excluded))
        if conflict:
            raise TaskSandboxError(
                "DEV_TASK_ROOT_EXCLUDED_CONFLICT",
                "Одна task одновременно выбрана root и excluded",
                tasks=conflict,
            )
        if not roots:
            raise TaskSandboxError(
                "DEV_TASK_ROOTS_EMPTY", "Task-aware DevSession требует хотя бы один root task"
            )
        return cls(
            profile=DEV_PROFILE,
            root_tasks=roots,
            excluded_tasks=excluded,
            catalog=catalog,
        )


@dataclass(frozen=True, slots=True)
class TaskProvenance:
    task: str
    required_by: str
    root: str
    reason: str
    sequence: int
    timestamp: str

    def as_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "required_by": self.required_by,
            "root": self.root,
            "reason": self.reason,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_payload(cls, payload: object) -> TaskProvenance:
        if not isinstance(payload, Mapping):
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance должен быть объектом")
        task = _safe_selector(payload.get("task"), field="provenance.task")
        required_by = _safe_selector(payload.get("required_by"), field="provenance.required_by")
        root = _safe_selector(payload.get("root"), field="provenance.root")
        reason = payload.get("reason")
        if not isinstance(reason, str) or reason not in {"dependency", "dependency_override"}:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance.reason не поддерживается")
        sequence_value = payload.get("sequence")
        if isinstance(sequence_value, bool) or not isinstance(sequence_value, int):
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance.sequence некорректен")
        sequence = sequence_value
        if sequence <= 0:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance.sequence должен быть положительным")
        timestamp = _safe_timestamp(payload.get("timestamp"), field="provenance.timestamp")
        return cls(task, required_by, root, str(reason), sequence, timestamp)


@dataclass(frozen=True, slots=True)
class TaskAuthorization:
    allowed: bool
    new_dependency: bool
    code: str
    reason: str | None = None
    root: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "new_dependency": self.new_dependency,
            "code": self.code,
            "reason": self.reason,
            "root": self.root,
        }


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    session_id: str
    repository_root: str
    profile: str
    state: str
    root_tasks: tuple[str, ...]
    excluded_tasks: tuple[str, ...]
    catalog: tuple[str, ...]
    dependencies: tuple[TaskProvenance, ...]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": TASK_POLICY_SCHEMA_VERSION,
            "session_id": self.session_id,
            "repository_root": self.repository_root,
            "profile": self.profile,
            "state": self.state,
            "root_tasks": list(self.root_tasks),
            "excluded_tasks": list(self.excluded_tasks),
            "catalog": list(self.catalog),
            "dependencies": [item.as_dict() for item in self.dependencies],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def allowed_tasks(self) -> tuple[str, ...]:
        return self.root_tasks + tuple(item.task for item in self.dependencies)

    def root_for(self, task: str) -> str | None:
        if task in self.root_tasks:
            return task
        for item in self.dependencies:
            if item.task == task:
                return item.root
        return None

    def authorize(self, caller: object, target: object) -> TaskAuthorization:
        if self.state != TASK_POLICY_ACTIVE:
            return TaskAuthorization(False, False, "DEV_TASK_POLICY_NOT_ACTIVE")
        try:
            caller_name = _safe_selector(caller, field="caller")
            target_name = _safe_selector(target, field="target")
        except TaskSandboxError as exc:
            return TaskAuthorization(False, False, exc.code)
        if target_name not in self.catalog:
            return TaskAuthorization(False, False, "DEV_TASK_UNKNOWN_TARGET")
        caller_root = self.root_for(caller_name)
        if caller_root is None:
            return TaskAuthorization(False, False, "DEV_TASK_CALLER_NOT_ALLOWED")
        existing_root = self.root_for(target_name)
        if existing_root is not None:
            return TaskAuthorization(True, False, "DEV_TASK_ALREADY_ALLOWED", root=existing_root)
        reason = (
            "dependency_override"
            if target_name in self.excluded_tasks
            else "dependency"
        )
        return TaskAuthorization(True, True, "DEV_TASK_DEPENDENCY_ALLOWED", reason, caller_root)

    @classmethod
    def from_payload(cls, payload: object, *, expected_root: Path | None = None) -> TaskPolicy:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != TASK_POLICY_SCHEMA_VERSION:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy имеет неподдерживаемую schema")
        session_id = _safe_session_id(payload.get("session_id"))
        repository_root = payload.get("repository_root")
        if not isinstance(repository_root, str) or not repository_root:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy не содержит repository_root")
        if any(ord(char) < 32 or ord(char) == 127 for char in repository_root) or "\x00" in repository_root:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит небезопасный repository_root")
        if expected_root is not None and not _same_path(repository_root, expected_root):
            raise TaskSandboxError("DEV_TASK_POLICY_FOREIGN_REPOSITORY", "task policy принадлежит другой рабочей копии")
        profile = payload.get("profile")
        if profile != DEV_PROFILE:
            raise TaskSandboxError("DEV_TASK_POLICY_FOREIGN_PROFILE", "task policy принадлежит другому профилю")
        state = payload.get("state")
        if not isinstance(state, str) or state not in TASK_POLICY_STATES:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит неизвестное состояние")
        roots = _policy_selector_values(payload.get("root_tasks"), field="root_tasks")
        excluded = _policy_selector_values(payload.get("excluded_tasks"), field="excluded_tasks")
        catalog = _policy_selector_values(payload.get("catalog"), field="catalog")
        if not catalog:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит пустой catalog")
        if not roots:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит пустой root_tasks")
        catalog_set = set(catalog)
        if not set(roots) <= catalog_set or not set(excluded) <= catalog_set:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит task вне catalog")
        if set(roots) & set(excluded):
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy содержит root/excluded conflict")
        dependencies_payload = payload.get("dependencies")
        if not isinstance(dependencies_payload, list):
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "task policy.dependencies должен быть массивом")
        dependencies: list[TaskProvenance] = []
        allowed = set(roots)
        roots_by_task = {task: task for task in roots}
        previous_sequence = 0
        for item_payload in dependencies_payload:
            item = TaskProvenance.from_payload(item_payload)
            if item.task in allowed or item.task not in catalog_set:
                raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance task неоднозначна или отсутствует в catalog")
            if item.required_by not in allowed:
                raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance required_by не разрешена предыдущей цепочкой")
            expected_reason = (
                "dependency_override"
                if item.task in excluded
                else "dependency"
            )
            if item.reason != expected_reason:
                raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance reason не соответствует excluded policy")
            if item.sequence <= previous_sequence:
                raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance sequence должна быть строго возрастающей")
            expected_root_for_caller = roots_by_task.get(item.required_by)
            if expected_root_for_caller is None:
                for prior in dependencies:
                    if prior.task == item.required_by:
                        expected_root_for_caller = prior.root
                        break
            if expected_root_for_caller != item.root:
                raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "provenance root не соответствует цепочке")
            allowed.add(item.task)
            roots_by_task[item.task] = item.root
            dependencies.append(item)
            previous_sequence = item.sequence
        if len({task.casefold() for task in catalog}) != len(catalog):
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "catalog содержит неоднозначные task commands")
        created_at = _safe_timestamp(payload.get("created_at"), field="created_at")
        updated_at = _safe_timestamp(payload.get("updated_at"), field="updated_at")
        return cls(
            session_id=session_id,
            repository_root=repository_root,
            profile=profile,
            state=state,
            root_tasks=roots,
            excluded_tasks=excluded,
            catalog=catalog,
            dependencies=tuple(dependencies),
            created_at=created_at,
            updated_at=updated_at,
        )

    def with_dependency(
        self,
        *,
        caller: str,
        target: str,
        timestamp: str,
        authorization: TaskAuthorization,
    ) -> TaskPolicy:
        if not authorization.allowed or not authorization.new_dependency:
            return self
        if authorization.reason is None or authorization.root is None:
            raise TaskSandboxError("DEV_TASK_POLICY_CORRUPT", "Недостаточно provenance для dependency")
        caller = _safe_selector(caller, field="provenance.required_by")
        target = _safe_selector(target, field="provenance.task")
        timestamp = _safe_timestamp(timestamp, field="provenance.timestamp")
        dependency = TaskProvenance(
            task=target,
            required_by=caller,
            root=authorization.root,
            reason=authorization.reason,
            sequence=(self.dependencies[-1].sequence + 1 if self.dependencies else 1),
            timestamp=timestamp,
        )
        return replace(self, dependencies=self.dependencies + (dependency,), updated_at=timestamp)


@dataclass(frozen=True, slots=True)
class TaskPolicyContext:
    """Результат проверки worker context без выдачи privilege только по env."""

    enforced: bool
    policy: TaskPolicy | None
    code: str


class TaskPolicyStore:
    """Repository-scoped atomic store для active task policy."""

    def __init__(self, environment: DevEnvironment) -> None:
        self.environment = environment
        self.path = environment.task_policy_file
        self.lock_path = environment.task_policy_lock_file

    def _scoped_paths(self) -> tuple[Path, Path]:
        return (
            _ensure_scoped_path(
                self.path,
                self.environment.repository_root,
                label="task policy path",
            ),
            _ensure_scoped_path(
                self.lock_path,
                self.environment.repository_root,
                label="task policy lock path",
            ),
        )

    def read(self) -> TaskPolicy | None:
        path, _lock_path = self._scoped_paths()
        payload = _read_json(path, max_bytes=_MAX_POLICY_BYTES, missing_ok=True)
        if payload is None:
            return None
        return TaskPolicy.from_payload(payload, expected_root=self.environment.repository_root)

    def inspect(self) -> dict[str, object]:
        try:
            policy = self.read()
        except TaskSandboxError as exc:
            return {"present": True, "valid": False, "code": exc.code}
        if policy is None:
            return {"present": False, "valid": True, "state": None}
        return {
            "present": True,
            "valid": True,
            "state": policy.state,
            "session_id": policy.session_id,
            "profile": policy.profile,
            "root_tasks": list(policy.root_tasks),
            "excluded_tasks": list(policy.excluded_tasks),
            "allowed_tasks": list(policy.allowed_tasks),
            "dependencies": [item.as_dict() for item in policy.dependencies],
        }

    def create(self, plan: TaskPlan, *, session_id: str, timestamp: str) -> TaskPolicy:
        if plan.profile != DEV_PROFILE or not plan.root_tasks:
            raise TaskSandboxError("DEV_TASK_POLICY_INVALID", "task policy требует профиль ap и root task")
        policy = TaskPolicy(
            session_id=_safe_session_id(session_id),
            repository_root=str(self.environment.repository_root),
            profile=DEV_PROFILE,
            state=TASK_POLICY_ACTIVE,
            root_tasks=plan.root_tasks,
            excluded_tasks=plan.excluded_tasks,
            catalog=plan.catalog.commands,
            dependencies=(),
            created_at=_safe_timestamp(timestamp, field="created_at"),
            updated_at=_safe_timestamp(timestamp, field="updated_at"),
        )
        TaskPolicy.from_payload(
            policy.as_dict(), expected_root=self.environment.repository_root
        )
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            _atomic_json_write(path, policy.as_dict())
        return policy

    def write(self, policy: TaskPolicy) -> None:
        TaskPolicy.from_payload(policy.as_dict(), expected_root=self.environment.repository_root)
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            _atomic_json_write(path, policy.as_dict())

    def remove(self) -> None:
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            atomic_remove(str(path))

    def mark_cleanup_pending(self, *, timestamp: str) -> TaskPolicy | None:
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            policy = self.read()
            if policy is None:
                return None
            updated = replace(
                policy,
                state=TASK_POLICY_CLEANUP_PENDING,
                updated_at=_safe_timestamp(timestamp, field="updated_at"),
            )
            _atomic_json_write(path, updated.as_dict())
            return updated

    def mark_preserved(self, *, timestamp: str) -> TaskPolicy | None:
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            policy = self.read()
            if policy is None:
                return None
            updated = replace(
                policy,
                state=TASK_POLICY_PRESERVED,
                updated_at=_safe_timestamp(timestamp, field="updated_at"),
            )
            _atomic_json_write(path, updated.as_dict())
            return updated

    def register_dependency(
        self,
        *,
        session_id: str,
        caller: str,
        target: str,
        timestamp: str,
    ) -> TaskAuthorization:
        path, lock_path = self._scoped_paths()
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            try:
                policy = self.read()
            except TaskSandboxError as exc:
                return TaskAuthorization(False, False, exc.code)
            if policy is None or policy.session_id != session_id:
                return TaskAuthorization(False, False, "DEV_TASK_POLICY_NOT_ACTIVE")
            try:
                session = _read_session(self.environment)
            except TaskSandboxError:
                return TaskAuthorization(False, False, "DEV_TASK_SESSION_NOT_ACTIVE")
            if session is None or session.session_id != session_id or session.state.value not in {
                "starting",
                "running",
                "stopping",
            }:
                return TaskAuthorization(False, False, "DEV_TASK_SESSION_NOT_ACTIVE")
            authorization = policy.authorize(caller, target)
            if not authorization.allowed:
                return authorization
            if not authorization.new_dependency:
                return authorization
            updated = policy.with_dependency(
                caller=caller,
                target=target,
                timestamp=timestamp,
                authorization=authorization,
            )
            _atomic_json_write(path, updated.as_dict())
            return authorization

    def rollback_dependency(
        self,
        *,
        session_id: str,
        caller: str,
        target: str,
        timestamp: str,
    ) -> bool:
        """Удалить только последнюю provenance после неудачного config update."""

        path, lock_path = self._scoped_paths()
        caller = _safe_selector(caller, field="provenance.required_by")
        target = _safe_selector(target, field="provenance.task")
        timestamp = _safe_timestamp(timestamp, field="provenance.timestamp")
        with _policy_thread_lock, _exclusive_policy_lock(lock_path):
            policy = self.read()
            if (
                policy is None
                or policy.session_id != session_id
                or policy.state != TASK_POLICY_ACTIVE
                or not policy.dependencies
            ):
                return False
            dependency = policy.dependencies[-1]
            if (
                dependency.required_by != caller
                or dependency.task != target
                or dependency.timestamp != timestamp
            ):
                return False
            updated = replace(
                policy,
                dependencies=policy.dependencies[:-1],
                updated_at=timestamp,
            )
            _atomic_json_write(path, updated.as_dict())
            return True


def _read_session(environment: DevEnvironment) -> DevSession | None:
    state_path = _ensure_scoped_path(
        environment.state_file,
        environment.repository_root,
        label="DevSession state path",
    )
    payload = _read_json(state_path, max_bytes=64 * 1024, missing_ok=True)
    if payload is None:
        return None
    try:
        session = DevSession.from_dict(payload)
    except ValueError as exc:
        raise TaskSandboxError("DEV_TASK_SESSION_CORRUPT", "DevSession marker некорректен") from exc
    if not _same_path(session.repository_root, environment.repository_root):
        raise TaskSandboxError("DEV_TASK_SESSION_FOREIGN_REPOSITORY", "DevSession marker принадлежит другой рабочей копии")
    return session


def _active_policy_context(config_name: object) -> TaskPolicyContext:
    if config_name != DEV_PROFILE:
        return TaskPolicyContext(False, None, "DEV_TASK_POLICY_INACTIVE_PROFILE")
    env_values = (
        os.environ.get(TASK_POLICY_SESSION_ENV),
        os.environ.get(TASK_POLICY_ROOT_ENV),
        os.environ.get(TASK_POLICY_FILE_ENV),
    )
    if not any(value is not None for value in env_values):
        return TaskPolicyContext(False, None, "DEV_TASK_POLICY_NO_CONTEXT")
    session_id, root_value, policy_value = env_values
    if not all(isinstance(value, str) and value for value in env_values):
        return TaskPolicyContext(True, None, "DEV_TASK_POLICY_CONTEXT_INCOMPLETE")
    try:
        environment = DevEnvironment.current()
        _safe_session_id(session_id)
        if not _same_path(root_value, environment.repository_root):
            return TaskPolicyContext(True, None, "DEV_TASK_POLICY_FOREIGN_REPOSITORY")
        if not _same_path(policy_value, environment.task_policy_file):
            return TaskPolicyContext(True, None, "DEV_TASK_POLICY_FOREIGN_PATH")
        session = _read_session(environment)
        if session is None or session.session_id != session_id:
            return TaskPolicyContext(True, None, "DEV_TASK_SESSION_NOT_ACTIVE")
        if session.state.value not in {"starting", "running", "stopping"}:
            return TaskPolicyContext(True, None, "DEV_TASK_SESSION_NOT_ACTIVE")
        policy = TaskPolicyStore(environment).read()
    except TaskSandboxError as exc:
        return TaskPolicyContext(True, None, exc.code)
    if policy is None:
        return TaskPolicyContext(True, None, "DEV_TASK_POLICY_MISSING")
    if policy.session_id != session_id or policy.profile != DEV_PROFILE:
        return TaskPolicyContext(True, None, "DEV_TASK_POLICY_CONTEXT_MISMATCH")
    if policy.state != TASK_POLICY_ACTIVE:
        return TaskPolicyContext(True, policy, "DEV_TASK_POLICY_NOT_ACTIVE")
    return TaskPolicyContext(True, policy, "DEV_TASK_POLICY_ACTIVE")


def active_task_policy(config_name: object) -> TaskPolicy | None:
    """Вернуть проверенную active policy; invalid worker context не даёт privilege."""

    context = _active_policy_context(config_name)
    if context.policy is None or not context.enforced:
        return None
    return context.policy if context.policy.state == TASK_POLICY_ACTIVE else None


def task_policy_context(config_name: object) -> TaskPolicyContext:
    """Проверить inherited context, не считая env variable самостоятельным доказательством."""

    return _active_policy_context(config_name)


def authorize_task_call(config_name: object, caller: object, target: object) -> TaskAuthorization | None:
    """Разрешить только canonical task_call под active inherited policy."""

    context = _active_policy_context(config_name)
    if not context.enforced:
        return None
    if context.policy is None:
        return TaskAuthorization(False, False, context.code)
    return context.policy.authorize(caller, target)


def register_task_dependency(
    config_name: object,
    *,
    caller: str,
    target: str,
    timestamp: str,
) -> TaskAuthorization | None:
    context = _active_policy_context(config_name)
    if not context.enforced:
        return None
    if context.policy is None:
        return TaskAuthorization(False, False, context.code)
    try:
        return TaskPolicyStore(DevEnvironment.current()).register_dependency(
            session_id=context.policy.session_id,
            caller=caller,
            target=target,
            timestamp=timestamp,
        )
    except TaskSandboxError as exc:
        return TaskAuthorization(False, False, exc.code)


def rollback_task_dependency(
    config_name: object,
    *,
    caller: str,
    target: str,
    timestamp: str,
) -> bool | None:
    """Отменить последнюю provenance после неудачного сохранения профиля."""

    context = _active_policy_context(config_name)
    if not context.enforced:
        return None
    if context.policy is None:
        return False
    store = TaskPolicyStore(DevEnvironment.current())
    try:
        rolled_back = store.rollback_dependency(
            session_id=context.policy.session_id,
            caller=caller,
            target=target,
            timestamp=timestamp,
        )
        if rolled_back:
            return True
        store.mark_cleanup_pending(timestamp=timestamp)
    except (OSError, RuntimeError, ValueError):
        try:
            store.mark_cleanup_pending(timestamp=timestamp)
        except (OSError, RuntimeError, ValueError):
            pass
    return False


def scheduler_time_text(value: datetime) -> str:
    """Сформатировать scheduler timestamp в persisted config style проекта."""

    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def reset_scheduler_state(payload: object, catalog: TaskCatalog) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TaskSandboxError("DEV_TASK_PROFILE_INVALID", "config/ap.json должен быть JSON-объектом")
    result = copy.deepcopy(dict(payload))
    for descriptor in catalog.tasks:
        section = result.get(descriptor.section)
        if not isinstance(section, dict) or not isinstance(section.get("Scheduler"), dict):
            raise TaskSandboxError(
                "DEV_TASK_SCHEDULER_MALFORMED",
                f"Секция {descriptor.section} изменилась во время операции",
                task=descriptor.command,
            )
        scheduler = section["Scheduler"]
        scheduler["Enable"] = False
        scheduler["NextRun"] = SCHEDULER_RESET_TIME
    return result


def apply_task_plan(
    payload: object,
    catalog: TaskCatalog,
    plan: TaskPlan,
    *,
    next_run: str,
) -> dict[str, object]:
    result = reset_scheduler_state(payload, catalog)
    for task in plan.root_tasks:
        section = result.get(task)
        if not isinstance(section, dict) or not isinstance(section.get("Scheduler"), dict):
            raise TaskSandboxError(
                "DEV_TASK_SCHEDULER_MALFORMED",
                f"Root task {task} исчезла из config/ap.json",
                task=task,
            )
        scheduler = section["Scheduler"]
        scheduler["Enable"] = True
        scheduler["NextRun"] = next_run
    return result


def scheduler_state(payload: object, catalog: TaskCatalog) -> dict[str, dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise TaskSandboxError("DEV_TASK_PROFILE_INVALID", "config/ap.json должен быть JSON-объектом")
    state: dict[str, dict[str, object]] = {}
    for descriptor in catalog.tasks:
        section = payload.get(descriptor.section)
        if not isinstance(section, Mapping) or not isinstance(section.get("Scheduler"), Mapping):
            raise TaskSandboxError("DEV_TASK_SCHEDULER_MALFORMED", "Scheduler изменился во время проверки")
        scheduler = section["Scheduler"]
        enabled = scheduler.get("Enable")
        next_run = scheduler.get("NextRun")
        if not isinstance(enabled, bool) or not isinstance(next_run, str):
            raise TaskSandboxError("DEV_TASK_SCHEDULER_MALFORMED", "Scheduler содержит некорректные runtime fields")
        state[descriptor.command] = {"enabled": enabled, "next_run": next_run}
    return state


__all__ = [
    "SCHEDULER_RESET_TIME",
    "TASK_POLICY_FILE_ENV",
    "TASK_POLICY_ROOT_ENV",
    "TASK_POLICY_SESSION_ENV",
    "TaskAuthorization",
    "TaskCatalog",
    "TaskDescriptor",
    "TaskPlan",
    "TaskPolicy",
    "TaskPolicyContext",
    "TaskPolicyStore",
    "TaskProvenance",
    "TaskSandboxError",
    "active_task_policy",
    "apply_task_plan",
    "authorize_task_call",
    "read_profile_payload",
    "register_task_dependency",
    "rollback_task_dependency",
    "reset_scheduler_state",
    "scheduler_state",
    "scheduler_time_text",
    "task_policy_context",
    "write_profile_payload",
]
