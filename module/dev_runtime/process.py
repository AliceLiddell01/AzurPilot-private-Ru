"""Запуск и точная проверка владения процессами DevSession."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

from module.dev_runtime.contracts import DEV_PROFILE, DevEnvironment, ProcessIdentity


class ProcessBackend:
    """Системная граница запуска и точной проверки процесса DevSession."""

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        command = self.expected_command(environment, session_id)
        environment.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = environment.log_file.open("a", encoding="utf-8", buffering=1)
        kwargs: dict[str, object] = {
            "cwd": str(environment.repository_root),
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        finally:
            log_handle.close()
        return int(process.pid)

    @staticmethod
    def expected_command(environment: DevEnvironment, session_id: str) -> list[str]:
        return [
            str(environment.python_executable),
            str(environment.repository_root / "gui.py"),
            "--dev-session-id",
            session_id,
            "--host",
            environment.host,
            "--port",
            str(environment.port),
            "--run",
            DEV_PROFILE,
        ]

    def capture(self, pid: int) -> ProcessIdentity | None:
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            return ProcessIdentity(
                pid=pid,
                created_at=float(process.create_time()),
                executable=str(Path(process.exe()).resolve()),
                command_line=tuple(process.cmdline()),
                cwd=str(Path(process.cwd()).resolve()),
            )
        except psutil.NoSuchProcess:
            return None
        except Exception as exc:
            raise RuntimeError(
                f"Не удалось получить идентичность процесса DevSession PID {pid}: {exc}"
            ) from exc

    def matches(self, identity: ProcessIdentity) -> bool | None:
        current = self.capture(identity.pid)
        if current is None:
            return None
        return (
            abs(current.created_at - identity.created_at) < 0.01
            and _same_path(current.executable, identity.executable)
            and _same_path(current.cwd, identity.cwd)
            and _normalized_command_line(current.command_line)
            == _normalized_command_line(identity.command_line)
        )

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        expected_gui = str((environment.repository_root / "gui.py").resolve())
        expected_python = str(environment.python_executable.resolve())
        found: list[ProcessIdentity] = []
        try:
            for process in psutil.process_iter(
                attrs=["pid", "create_time", "exe", "cmdline", "cwd"]
            ):
                try:
                    info = process.info
                    cmdline = tuple(info.get("cmdline") or ())
                    if "--dev-session-id" not in cmdline:
                        continue
                    index = cmdline.index("--dev-session-id")
                    if index + 1 >= len(cmdline) or cmdline[index + 1] != session_id:
                        continue
                    raw_executable = info.get("exe")
                    raw_cwd = info.get("cwd")
                    if not raw_executable or not raw_cwd:
                        raise RuntimeError(
                            "Найден процесс с идентификатором DevSession, но его executable/cwd нельзя подтвердить"
                        )
                    try:
                        pid = int(info["pid"])
                        created_at = float(info["create_time"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "Найден процесс с идентификатором DevSession, но его PID/create time нельзя подтвердить"
                        ) from exc
                    executable = str(Path(str(raw_executable)).resolve())
                    cwd = str(Path(str(raw_cwd)).resolve())
                    command_python = cmdline[0] if cmdline else ""
                    if not (
                        _same_path(executable, expected_python)
                        or _same_path(command_python, expected_python)
                    ):
                        continue
                    if not _same_path(cwd, str(environment.repository_root)):
                        continue
                    if not any(_same_path(item, expected_gui) for item in cmdline):
                        continue
                    found.append(
                        ProcessIdentity(
                            pid=pid,
                            created_at=created_at,
                            executable=executable,
                            command_line=cmdline,
                            cwd=cwd,
                        )
                    )
                except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError, ValueError):
                    continue
        except Exception as exc:
            raise RuntimeError(
                f"Не удалось найти процесс DevSession по идентификатору сессии: {exc}"
            ) from exc
        return tuple(found)

    def is_descendant(self, child_pid: int, parent: ProcessIdentity) -> bool:
        if child_pid == parent.pid:
            return self.matches(parent) is True
        try:
            process = psutil.Process(child_pid)
            for ancestor in process.parents():
                if ancestor.pid != parent.pid:
                    continue
                try:
                    return abs(ancestor.create_time() - parent.created_at) < 0.01
                except psutil.Error:
                    return False
            return False
        except psutil.Error:
            return False

    def listens_on(self, pid: int, host: str, port: int) -> bool:
        """Подтвердить, что конкретный владелец WebUI слушает локальный порт."""
        try:
            process = psutil.Process(pid)
            for connection in process.net_connections(kind="inet"):
                if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                    continue
                local_host = str(connection.laddr.ip)
                local_port = int(connection.laddr.port)
                if local_port == port and local_host == host:
                    return True
            return False
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            return False

    def request_stop(self, identity: ProcessIdentity) -> bool:
        if self.matches(identity) is not True:
            return False
        try:
            if os.name == "nt":
                os.kill(identity.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.kill(identity.pid, signal.SIGINT)
            return True
        except (OSError, ValueError):
            return False

    def wait_exit(self, identity: ProcessIdentity, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            matches = self.matches(identity)
            if matches is None:
                return True
            if matches is False:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))

    def force_stop(self, identity: ProcessIdentity) -> bool:
        if self.matches(identity) is not True:
            return False
        try:
            root = psutil.Process(identity.pid)
            children = root.children(recursive=True)
            if self.matches(identity) is not True:
                return False
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                root.kill()
            except psutil.NoSuchProcess:
                return True
            _, alive = psutil.wait_procs([root, *children], timeout=5)
            return not alive or self.matches(identity) is None
        except Exception:
            return False


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def _normalized_command_line(command_line: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in command_line:
        if item.casefold().endswith(
            ("python.exe", "pythonw.exe", "python")
        ) or item.casefold().endswith("gui.py"):
            try:
                normalized.append(os.path.normcase(str(Path(item).resolve())))
                continue
            except (OSError, RuntimeError):
                pass
        normalized.append(item)
    return tuple(normalized)
