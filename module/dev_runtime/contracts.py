"""Контракты и фиксированные границы AzurPilot Dev Runtime."""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from module.dev_runtime.target import (
    DevTarget,
    DevTargetError,
    DevTargetRegistry,
)
from module.dev_runtime.target import target_identity as calculate_target_identity

DEV_HOST = "127.0.0.1"
DEV_PORT = 25549
STATE_SCHEMA_VERSION = 1
DEFAULT_READY_TIMEOUT = 120.0
DEFAULT_STOP_TIMEOUT = 20.0
_IS_WINDOWS = os.name == "nt"


def _absolute_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _paths_equivalent(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        try:
            return _absolute_path(left) == _absolute_path(right)
        except (OSError, RuntimeError, ValueError):
            return False


def _allowed_command_python_paths(expected_python: Path) -> tuple[Path, ...]:
    """Вернуть допустимые argv[0] для project venv и его Windows base runtime."""

    allowed = [expected_python]
    if not _IS_WINDOWS:
        return tuple(allowed)

    try:
        current_python = Path(os.path.abspath(sys.executable))
        if not _paths_equivalent(current_python, expected_python):
            return tuple(allowed)
        base_executable = getattr(sys, "_base_executable", None)
        if not isinstance(base_executable, str) or not base_executable:
            return tuple(allowed)
        base_python = Path(os.path.abspath(base_executable))
    except (OSError, RuntimeError, TypeError, ValueError):
        return tuple(allowed)

    if not any(_paths_equivalent(base_python, candidate) for candidate in allowed):
        allowed.append(base_python)
    return tuple(allowed)


class DevSessionState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    STALE = "stale"


class DevRuntimeMode(StrEnum):
    STANDALONE_PROCESS = "standalone_process"
    SHARED_WEBUI = "shared_webui"


class DevTaskMode(StrEnum):
    NONE = "none"
    TASK_AWARE = "task_aware"


class DevTaskPhase(StrEnum):
    NONE = "none"
    PREPARING = "preparing"
    PREPARED = "prepared"
    RUNNING = "running"
    PRESERVED = "preserved"
    CLEANUP_PENDING = "cleanup_pending"
    CLEAN = "clean"


class DevStatusKind(StrEnum):
    NO_SESSION = "no_session"
    STARTING = "starting"
    RUNNING_OWNED = "running_owned"
    STOPPING = "stopping"
    STALE = "stale"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    FAILED = "failed"
    STOPPED = "stopped"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class DevResult:
    ok: bool
    code: str
    message: str
    state: str
    session_id: str | None = None
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    created_at: float
    executable: str
    command_line: tuple[str, ...]
    cwd: str

    def as_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "created_at": self.created_at,
            "executable": self.executable,
            "command_line": list(self.command_line),
            "cwd": self.cwd,
        }

    def command_session_id(self) -> str | None:
        if self.command_line.count("--dev-session-id") != 1:
            return None
        index = self.command_line.index("--dev-session-id")
        if index + 1 >= len(self.command_line):
            return None
        session_id = self.command_line[index + 1]
        return session_id if session_id else None

    def command_profile_name(self) -> str | None:
        """Вернуть профиль из exact CLI signature, не читая текущий target marker."""

        if self.command_line.count("--run") != 1:
            return None
        index = self.command_line.index("--run")
        if index + 1 >= len(self.command_line):
            return None
        profile_name = self.command_line[index + 1]
        try:
            DevTarget(profile_name)
        except ValueError:
            return None
        return profile_name

    def matches_dev_contract(
        self,
        repository_root: Path,
        session_id: str,
        python_executable: Path | None = None,
        profile_name: str | None = None,
    ) -> bool:
        """Проверить полную сигнатуру процесса одной DevSession."""

        if self.pid <= 0 or not math.isfinite(self.created_at) or self.created_at <= 0:
            return False
        if not session_id or "\x00" in session_id:
            return False

        try:
            root = Path(os.path.abspath(repository_root))
            expected_python = (
                Path(os.path.abspath(python_executable))
                if python_executable is not None
                else root
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            if profile_name is None:
                profile_name = DevTargetRegistry.load(root).profile_name
            allowed_python_paths = _allowed_command_python_paths(expected_python)
            expected_gui = root / "gui.py"
            expected = (
                str(expected_python),
                str(expected_gui),
                "--dev-session-id",
                session_id,
                "--host",
                DEV_HOST,
                "--port",
                str(DEV_PORT),
                "--run",
                profile_name,
            )
        except (OSError, RuntimeError, ValueError):
            return False

        if len(self.command_line) != len(expected):
            return False
        if not any(
            _paths_equivalent(self.command_line[0], candidate)
            for candidate in allowed_python_paths
        ):
            return False
        if not _paths_equivalent(self.command_line[1], expected[1]):
            return False
        if self.command_line[2:] != expected[2:]:
            return False
        if not _paths_equivalent(self.cwd, root):
            return False
        # Windows venv redirector запускает base runtime как дочерний процесс:
        # у runtime-child argv[0] уже может быть sys._base_executable. Разрешаем
        # этот путь только когда текущий CLI сам запущен из ожидаемого project
        # .venv. Остальные argv/cwd/token остаются exact. Фактический executable
        # фиксируется в ProcessIdentity и затем сравнивается при PID-reuse.
        return True

    @classmethod
    def from_dict(cls, payload: object) -> ProcessIdentity:
        if not isinstance(payload, dict):
            raise ValueError("process должен быть объектом")
        command_line = payload.get("command_line")
        if not isinstance(command_line, list) or not all(
            isinstance(item, str) for item in command_line
        ):
            raise ValueError("command_line должен быть массивом строк")
        try:
            pid = int(payload["pid"])
            created_at = float(payload["created_at"])
            executable = payload["executable"]
            cwd = payload["cwd"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("process содержит некорректные обязательные поля") from exc
        if pid <= 0:
            raise ValueError("pid должен быть положительным")
        if not math.isfinite(created_at) or created_at <= 0:
            raise ValueError("created_at должен быть положительным конечным числом")
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValueError("executable должен быть непустой безопасной строкой")
        if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
            raise ValueError("cwd должен быть непустой безопасной строкой")
        if any("\x00" in item for item in command_line):
            raise ValueError("command_line содержит недопустимый нулевой байт")
        return cls(
            pid=pid,
            created_at=created_at,
            executable=executable,
            command_line=tuple(command_line),
            cwd=cwd,
        )


@dataclass(slots=True)
class DevSession:
    session_id: str
    state: DevSessionState
    repository_root: str
    created_at: str
    updated_at: str
    process: ProcessIdentity | None = None
    last_code: str | None = None
    last_message: str | None = None
    task_mode: DevTaskMode = DevTaskMode.NONE
    task_phase: DevTaskPhase = DevTaskPhase.NONE
    task_cleanup_required: bool = False
    task_policy_expected: bool = False
    profile_name: str | None = None
    target_identity: str | None = None
    runtime_mode: DevRuntimeMode = DevRuntimeMode.SHARED_WEBUI

    def __post_init__(self) -> None:
        if self.profile_name is None:
            if self.target_identity is not None:
                raise ValueError("target_identity нельзя сохранить без profile_name")
            return
        try:
            target = DevTarget(self.profile_name)
        except ValueError:
            # Оставляем прежнюю возможность создать искусственно повреждённый marker:
            # manager должен классифицировать его как DEV_TARGET_INVALID при
            # чтении, а не скрывать диагностику исключением конструктора.
            if self.target_identity is not None:
                raise ValueError("target_identity нельзя проверить для invalid profile_name") from None
            return
        expected = calculate_target_identity(target)
        if self.target_identity is None:
            # Маркеры предыдущей схемы могли содержать profile_name без identity.
            return
        if not isinstance(self.target_identity, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.target_identity
        ):
            raise ValueError("target_identity имеет некорректный формат")
        if self.target_identity != expected:
            raise ValueError("target_identity не соответствует profile_name")

    @property
    def is_task_aware(self) -> bool:
        return self.task_mode == DevTaskMode.TASK_AWARE

    @property
    def task_cleanup_needed(self) -> bool:
        return self.is_task_aware and self.task_cleanup_required

    def task_lifecycle_as_dict(self) -> dict[str, object]:
        return {
            "mode": self.task_mode.value,
            "phase": self.task_phase.value,
            "cleanup_required": self.task_cleanup_required,
            "policy_expected": self.task_policy_expected,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "state": self.state.value,
            "repository_root": self.repository_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "process": self.process.as_dict() if self.process is not None else None,
            "profile_name": self.profile_name,
            "target_identity": self.target_identity,
            "runtime_mode": self.runtime_mode.value,
            "last_code": self.last_code,
            "last_message": self.last_message,
            "task_mode": self.task_mode.value,
            "task_phase": self.task_phase.value,
            "task_cleanup_required": self.task_cleanup_required,
            "task_policy_expected": self.task_policy_expected,
        }

    @classmethod
    def from_dict(cls, payload: object) -> DevSession:
        if not isinstance(payload, dict):
            raise ValueError("маркер должен быть объектом")
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("неподдерживаемая версия маркера")
        session_id = payload.get("session_id")
        repository_root = payload.get("repository_root")
        created_at = payload.get("created_at")
        updated_at = payload.get("updated_at")
        if not all(
            isinstance(value, str) and value
            for value in (session_id, repository_root, created_at, updated_at)
        ):
            raise ValueError("маркер содержит неполные обязательные поля")
        process_payload = payload.get("process")
        process = (
            None if process_payload is None else ProcessIdentity.from_dict(process_payload)
        )
        if process is not None:
            process_session_id = process.command_session_id()
            if process_session_id is not None and process_session_id != session_id:
                raise ValueError("process принадлежит другой DevSession")
        profile_name = payload.get("profile_name")
        if profile_name is not None:
            if not isinstance(profile_name, str):
                raise ValueError("profile_name должен быть строкой или null")
            try:
                profile_name = DevTarget(profile_name).profile_name
            except ValueError as exc:
                raise ValueError("profile_name имеет недопустимый формат") from exc
        process_profile_name = process.command_profile_name() if process is not None else None
        if (
            profile_name is not None
            and process_profile_name is not None
            and profile_name != process_profile_name
        ):
            raise ValueError("process принадлежит другому development target")
        if profile_name is None:
            profile_name = process_profile_name
        target_identity = payload.get("target_identity")
        if target_identity is not None and not isinstance(target_identity, str):
            raise ValueError("target_identity должен быть строкой или null")
        try:
            runtime_mode = DevRuntimeMode(
                str(payload.get("runtime_mode", DevRuntimeMode.STANDALONE_PROCESS.value))
            )
        except ValueError as exc:
            raise ValueError("маркер содержит некорректный runtime mode") from exc
        try:
            state = DevSessionState(str(payload["state"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("маркер содержит некорректное состояние") from exc
        last_code = payload.get("last_code")
        last_message = payload.get("last_message")
        if last_code is not None and not isinstance(last_code, str):
            raise ValueError("last_code должен быть строкой или null")
        if last_message is not None and not isinstance(last_message, str):
            raise ValueError("last_message должен быть строкой или null")
        try:
            task_mode = DevTaskMode(str(payload.get("task_mode", DevTaskMode.NONE.value)))
            task_phase = DevTaskPhase(
                str(payload.get("task_phase", DevTaskPhase.NONE.value))
            )
        except ValueError as exc:
            raise ValueError("маркер содержит некорректный task lifecycle") from exc
        task_cleanup_required = payload.get("task_cleanup_required", False)
        task_policy_expected = payload.get("task_policy_expected", False)
        if not isinstance(task_cleanup_required, bool) or not isinstance(
            task_policy_expected, bool
        ):
            raise ValueError("task lifecycle flags должны быть boolean")
        if task_mode == DevTaskMode.NONE:
            if (
                task_phase != DevTaskPhase.NONE
                or task_cleanup_required
                or task_policy_expected
            ):
                raise ValueError("обычная DevSession не может содержать task lifecycle")
        elif (
            task_phase == DevTaskPhase.NONE
            or task_cleanup_required is not True
            or task_policy_expected is not True
        ) and task_phase != DevTaskPhase.CLEAN:
            raise ValueError("незавершённый task lifecycle требует cleanup и policy")
        if task_mode == DevTaskMode.TASK_AWARE and task_phase == DevTaskPhase.CLEAN:
            if task_cleanup_required or task_policy_expected:
                raise ValueError("clean task lifecycle не должен требовать cleanup")
        return cls(
            session_id=session_id,
            state=state,
            repository_root=repository_root,
            created_at=created_at,
            updated_at=updated_at,
            process=process,
            last_code=last_code,
            last_message=last_message,
            task_mode=task_mode,
            task_phase=task_phase,
            task_cleanup_required=task_cleanup_required,
            task_policy_expected=task_policy_expected,
            profile_name=profile_name,
            target_identity=target_identity,
            runtime_mode=runtime_mode,
        )


@dataclass(frozen=True, slots=True)
class DevEnvironment:
    repository_root: Path
    python_executable: Path
    dev_target: DevTarget | None = None
    host: str = DEV_HOST
    port: int = DEV_PORT

    def __post_init__(self) -> None:
        if self.host != DEV_HOST or self.port != DEV_PORT:
            raise ValueError("Dev Runtime разрешает только фиксированный локальный адрес и порт")
        root = Path(self.repository_root).resolve()
        object.__setattr__(self, "repository_root", root)
        if self.dev_target is None:
            object.__setattr__(self, "dev_target", DevTargetRegistry.load(root))

    @property
    def state_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-session.json"

    @property
    def lock_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-session.lock"

    @property
    def pre_execution_lock_file(self) -> Path:
        """Блокировка запуска до фиксации владения целевым процессом."""

        return self.repository_root / "config" / "state" / "dev-runtime-pre-execution.lock"

    @property
    def log_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-gui.log"

    @property
    def profile_file(self) -> Path:
        target = self.dev_target
        if target is None:  # pragma: no cover - защищено __post_init__
            raise DevTargetError("DEV_TARGET_NOT_CONFIGURED", "Development target не назначен")
        return target.profile_file(self.repository_root)

    @property
    def profile_name(self) -> str:
        target = self.dev_target
        if target is None:  # pragma: no cover - защищено __post_init__
            raise DevTargetError("DEV_TARGET_NOT_CONFIGURED", "Development target не назначен")
        return target.profile_name

    @property
    def target_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-target.json"

    @property
    def coordination_lock_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-coordination.lock"

    @property
    def control_root(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-control"

    @property
    def task_policy_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-task-policy.json"

    @property
    def task_policy_lock_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-task-policy.lock"

    @property
    def evidence_root(self) -> Path:
        """Изолированный ignored root для диагностических артефактов сессий."""

        return self.repository_root / "config" / "state" / "dev-runtime-runs"

    @property
    def evidence_lock_file(self) -> Path:
        """Межпроцессная блокировка хранилища диагностики текущей рабочей копии."""

        return self.repository_root / "config" / "state" / "dev-runtime-evidence.lock"

    @classmethod
    def current(cls, repository_root: Path | None = None) -> DevEnvironment:
        root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else Path(__file__).resolve().parents[2]
        )
        return cls(
            repository_root=root,
            # Не resolve(): POSIX venv обычно использует symlink на базовый Python.
            python_executable=Path(os.path.abspath(sys.executable)),
            dev_target=DevTargetRegistry.load(root),
        )
