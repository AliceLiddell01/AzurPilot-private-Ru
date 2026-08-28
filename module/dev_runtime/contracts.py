"""Контракты и фиксированные границы AzurPilot Dev Runtime."""

from __future__ import annotations

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
        if not isinstance(executable, str) or not executable:
            raise ValueError("executable должен быть непустой строкой")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd должен быть непустой строкой")
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
            python_executable=Path(sys.executable).resolve(),
        )
