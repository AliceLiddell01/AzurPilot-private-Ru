"""Контракты и фиксированные границы AzurPilot Dev Runtime."""

from __future__ import annotations

import math
import os
import sys
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

DEV_PROFILE = "ap"
DEV_HOST = "127.0.0.1"
DEV_PORT = 25549
STATE_SCHEMA_VERSION = 1
DEFAULT_READY_TIMEOUT = 120.0
DEFAULT_STOP_TIMEOUT = 20.0


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


class DevSessionState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    STALE = "stale"


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

    def matches_dev_contract(
        self,
        repository_root: Path,
        session_id: str,
        python_executable: Path | None = None,
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
                DEV_PROFILE,
            )
        except (OSError, RuntimeError, ValueError):
            return False

        if len(self.command_line) != len(expected):
            return False
        if not _paths_equivalent(self.command_line[0], expected[0]):
            return False
        if not _paths_equivalent(self.command_line[1], expected[1]):
            return False
        if self.command_line[2:] != expected[2:]:
            return False
        if not _paths_equivalent(self.cwd, root):
            return False
        # На Windows venv может запускаться через redirector: argv[0] остаётся
        # project Python, а image executable, который возвращает Process API,
        # может указывать на базовый интерпретатор. Фактический executable всё
        # равно фиксируется в ProcessIdentity и затем сравнивается при PID-reuse
        # проверках; принадлежность DevSession доказывает точный argv/cwd/token.
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

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "state": self.state.value,
            "repository_root": self.repository_root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "process": self.process.as_dict() if self.process is not None else None,
            "last_code": self.last_code,
            "last_message": self.last_message,
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
        return cls(
            session_id=session_id,
            state=state,
            repository_root=repository_root,
            created_at=created_at,
            updated_at=updated_at,
            process=process,
            last_code=last_code,
            last_message=last_message,
        )


@dataclass(frozen=True, slots=True)
class DevEnvironment:
    repository_root: Path
    python_executable: Path
    host: str = DEV_HOST
    port: int = DEV_PORT

    def __post_init__(self) -> None:
        if self.host != DEV_HOST or self.port != DEV_PORT:
            raise ValueError("Dev Runtime разрешает только фиксированный локальный адрес и порт")

    @property
    def state_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-session.json"

    @property
    def lock_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-session.lock"

    @property
    def log_file(self) -> Path:
        return self.repository_root / "config" / "state" / "dev-runtime-gui.log"

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
        )
