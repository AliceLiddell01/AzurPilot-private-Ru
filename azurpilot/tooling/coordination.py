"""Cross-platform locks, lifecycle state и ownership observations."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import psutil

from .contracts import LifecycleRecord, ResultCode
from .errors import ToolingError
from .filesystem import (
    ScopedPath,
    StateLayout,
    bounded_read_text,
    is_unsafe_path,
    path_has_link,
    path_identity,
)
from .process import ProcessIdentity


class FileLock:
    """Неблокирующий advisory lock с monotonic timeout."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream = None
        self._locked = False

    def acquire(self, timeout_seconds: float = 0.0) -> bool:
        if timeout_seconds < 0 or timeout_seconds > 24 * 60 * 60:
            raise ValueError("timeout_seconds должен быть bounded")
        if self._locked:
            return True
        if path_has_link(self.path):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Lock path содержит symlink или reparse point.",
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0)
        if self.path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._stream = stream
                self._locked = True
                return True
            except BlockingIOError, OSError:
                if time.monotonic() >= deadline:
                    stream.close()
                    return False
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._locked = False

    def __enter__(self) -> Self:
        if not self.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT, "Операция уже выполняется."
            )
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True)
class PortObservation:
    """Результат read-only проверки TCP listener."""

    port: int
    pids: tuple[int, ...]
    inspection_failed: bool = False


def observe_tcp_port(port: int) -> PortObservation:
    if not 1 <= port <= 65535:
        raise ValueError("port вне диапазона")
    pids: set[int] = set()
    failed = False
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied, OSError:
        return PortObservation(port, (), True)
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN:
            continue
        try:
            local_port = connection.laddr.port
        except AttributeError:
            continue
        if local_port == port:
            if connection.pid is None:
                failed = True
            else:
                pids.add(int(connection.pid))
    return PortObservation(port, tuple(sorted(pids)), failed)


@dataclass(frozen=True)
class RepositoryCoordinator:
    """State/locks, расположенные вне checkout и привязанные к его identity."""

    root: Path
    layout: StateLayout

    @classmethod
    def for_root(cls, root: Path) -> RepositoryCoordinator:
        return cls(root, StateLayout.for_repository(root))

    def lock(self, operation: str) -> FileLock:
        return FileLock(self.layout.path(f"{operation}.lock"))

    @property
    def lifecycle_state_path(self) -> Path:
        return self.layout.path("lifecycle.json")

    @property
    def stop_request_path(self) -> Path:
        return self.layout.path("stop.request")

    def read_lifecycle(self) -> LifecycleRecord | None:
        path = self.lifecycle_state_path
        if is_unsafe_path(path):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Lifecycle state оказался symlinkом или reparse point.",
            )
        if not path.exists():
            return None
        try:
            return LifecycleRecord.model_validate_json(
                bounded_read_text(path, max_bytes=64 * 1024)
            )
        except Exception as exc:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Lifecycle state повреждён или имеет неизвестную схему.",
            ) from exc

    def write_lifecycle(self, record: LifecycleRecord) -> None:
        if record.root_identity != path_identity(self.root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Lifecycle state относится к другому root.",
            )
        self.layout.ensure()
        ScopedPath(self.layout.repository_directory).atomic_write_text(
            "lifecycle.json", record.model_dump_json(indent=2)
        )

    def request_stop(self) -> None:
        self.layout.ensure()
        ScopedPath(self.layout.repository_directory).atomic_write_text(
            "stop.request",
            json.dumps(
                {
                    "schema_version": 1,
                    "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
        )

    def clear_stop_request(self) -> None:
        path = self.stop_request_path
        if is_unsafe_path(path):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Stop request оказался symlinkом или reparse point.",
            )
        if path.exists():
            path.unlink()

    def clear_lifecycle(self) -> None:
        path = self.lifecycle_state_path
        if is_unsafe_path(path):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Lifecycle state оказался symlinkом или reparse point.",
            )
        if path.exists():
            path.unlink()

    def identity_from_record(self, record: LifecycleRecord) -> ProcessIdentity:
        if record.root_identity != path_identity(self.root):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "PID state принадлежит другому root.",
            )
        return ProcessIdentity(
            pid=record.pid,
            start_time=record.started_at,
            executable=Path(record.executable),
            argv=record.argv,
            cwd=Path(record.working_directory),
            process_group=record.process_group,
        )

    @staticmethod
    def record_from_identity(
        identity: ProcessIdentity, root: Path, port: int
    ) -> LifecycleRecord:
        return LifecycleRecord(
            root_identity=path_identity(root),
            pid=identity.pid,
            started_at=identity.start_time,
            executable=str(identity.executable),
            argv=identity.argv,
            working_directory=str(identity.cwd),
            process_group=identity.process_group,
            port=port,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def iter_processes_for_root(root: Path) -> Iterator[ProcessIdentity]:
    """Дать только процессы, для которых проверка exact identity завершилась."""

    root = root.resolve(strict=False)
    try:
        processes = psutil.process_iter(["pid", "create_time", "exe", "cmdline", "cwd"])
    except OSError:
        return
    for process in processes:
        try:
            cwd = process.info.get("cwd")
            executable = process.info.get("exe")
            cmdline = tuple(str(item) for item in process.info.get("cmdline") or ())
            if not cwd or not executable or not cmdline:
                continue
            if Path(cwd).resolve(strict=False) != root:
                continue
            yield ProcessIdentity(
                pid=int(process.info["pid"]),
                start_time=float(process.info["create_time"]),
                executable=Path(executable).resolve(strict=False),
                argv=cmdline,
                cwd=Path(cwd).resolve(strict=False),
            )
        except psutil.Error, OSError, TypeError, ValueError:
            continue


__all__ = [
    "FileLock",
    "PortObservation",
    "RepositoryCoordinator",
    "iter_processes_for_root",
    "observe_tcp_port",
]
