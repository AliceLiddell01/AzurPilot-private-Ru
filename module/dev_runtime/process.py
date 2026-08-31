"""Запуск и точная проверка владения процессами DevSession."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

from module.dev_runtime.contracts import DevEnvironment, ProcessIdentity
from module.dev_runtime.target import DevTarget
from module.dev_runtime.task_sandbox import (
    TASK_POLICY_FILE_ENV,
    TASK_POLICY_ROOT_ENV,
    TASK_POLICY_SESSION_ENV,
)

_WINDOWS_REDIRECTOR_SETTLE_TIMEOUT = 1.0
_WINDOWS_REDIRECTOR_POLL_INTERVAL = 0.02
_IS_WINDOWS = os.name == "nt"
_WINDOWS_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", None)


class ProcessBackend:
    """Системная граница запуска и точной проверки процесса DevSession."""

    def __init__(self) -> None:
        self._launch_expectations: dict[int, tuple[DevEnvironment, str]] = {}
        self._launch_handles: dict[int, subprocess.Popen] = {}

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
        child_environment = os.environ.copy()
        for variable in (
            TASK_POLICY_SESSION_ENV,
            TASK_POLICY_ROOT_ENV,
            TASK_POLICY_FILE_ENV,
        ):
            child_environment.pop(variable, None)
        try:
            policy_path = environment.task_policy_file
            policy_present = policy_path.is_file() and not policy_path.is_symlink()
            if policy_present and hasattr(policy_path, "is_junction"):
                policy_present = not policy_path.is_junction()
        except OSError:
            policy_present = False
        if policy_present:
            child_environment[TASK_POLICY_SESSION_ENV] = session_id
            child_environment[TASK_POLICY_ROOT_ENV] = str(environment.repository_root)
            child_environment[TASK_POLICY_FILE_ENV] = str(environment.task_policy_file)
        kwargs["env"] = child_environment
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        finally:
            log_handle.close()
        pid = int(process.pid)
        self._launch_expectations[pid] = (environment, session_id)
        self._launch_handles[pid] = process
        return pid

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
            environment.profile_name,
        ]

    @staticmethod
    def identity_belongs_to_session(
        environment: DevEnvironment,
        session_id: str,
        identity: ProcessIdentity,
        profile_name: str | None = None,
    ) -> bool:
        expected_profile = (
            profile_name if profile_name is not None else environment.profile_name
        )
        return identity.matches_dev_contract(
            environment.repository_root,
            session_id,
            environment.python_executable,
            expected_profile,
        )

    def _identity_is_destructively_trusted(self, identity: ProcessIdentity) -> bool:
        expectation = self._launch_expectations.get(identity.pid)
        if expectation is not None:
            environment, session_id = expectation
            return self.identity_belongs_to_session(environment, session_id, identity)
        session_id = identity.command_session_id()
        if session_id is None:
            return False
        profile_name = identity.command_profile_name()
        if profile_name is None:
            return False
        return identity.matches_dev_contract(
            Path(identity.cwd), session_id, profile_name=profile_name
        )

    def _abort_unverified_launch(self, pid: int) -> bool:
        """Остановить только что созданный процесс через принадлежащий нам Popen handle."""
        process = self._launch_handles.pop(pid, None)
        if process is None:
            return False
        try:
            if process.poll() is not None:
                return True
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            return process.poll() is not None
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _identity_from_process(
        process: psutil.Process, *, pid: int | None = None
    ) -> ProcessIdentity:
        process_pid = int(process.pid if pid is None else pid)
        return ProcessIdentity(
            pid=process_pid,
            created_at=float(process.create_time()),
            executable=str(Path(process.exe()).absolute()),
            command_line=tuple(process.cmdline()),
            cwd=str(Path(process.cwd()).absolute()),
        )

    def _capture_windows_redirected_child(
        self,
        launcher_pid: int,
        environment: DevEnvironment,
        session_id: str,
    ) -> ProcessIdentity | None:
        """Усыновить runtime-child стандартного Windows venv redirector."""
        deadline = time.monotonic() + _WINDOWS_REDIRECTOR_SETTLE_TIMEOUT
        while True:
            try:
                launcher = psutil.Process(launcher_pid)
                children = launcher.children(recursive=True)
            except psutil.NoSuchProcess:
                return self._find_windows_redirected_child(
                    launcher_pid, environment, session_id
                )
            except psutil.AccessDenied as exc:
                raise RuntimeError(
                    "Нельзя безопасно перечислить дочерние процессы Windows venv launcher"
                ) from exc

            candidates: list[ProcessIdentity] = []
            for child in children:
                try:
                    if child.status() == psutil.STATUS_ZOMBIE:
                        continue
                    identity = self._identity_from_process(child)
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, TypeError, ValueError):
                    continue
                if self.identity_belongs_to_session(environment, session_id, identity):
                    candidates.append(identity)

            if len(candidates) > 1:
                raise RuntimeError(
                    "Windows venv launcher создал несколько процессов с полной сигнатурой DevSession"
                )
            if len(candidates) == 1:
                return candidates[0]

            handle = self._launch_handles.get(launcher_pid)
            if handle is None or handle.poll() is not None:
                return self._find_windows_redirected_child(
                    launcher_pid, environment, session_id
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._find_windows_redirected_child(
                    launcher_pid, environment, session_id
                )
            time.sleep(min(_WINDOWS_REDIRECTOR_POLL_INTERVAL, remaining))

    def _find_windows_redirected_child(
        self,
        launcher_pid: int,
        environment: DevEnvironment,
        session_id: str,
    ) -> ProcessIdentity | None:
        """Найти child с exact signature после выхода короткоживущего launcher."""

        candidates: list[ProcessIdentity] = []
        try:
            processes = psutil.process_iter(
                ["pid", "ppid", "create_time", "exe", "cmdline", "cwd"]
            )
            for process in processes:
                try:
                    if process.info.get("ppid") != launcher_pid:
                        continue
                    identity = self._identity_from_process(process)
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, TypeError, ValueError):
                    continue
                if self.identity_belongs_to_session(environment, session_id, identity):
                    candidates.append(identity)
        except (psutil.AccessDenied, OSError) as exc:
            raise RuntimeError(
                "Нельзя безопасно перечислить процессы после выхода Windows venv launcher"
            ) from exc
        if len(candidates) > 1:
            raise RuntimeError(
                "Windows venv launcher оставил несколько процессов с полной сигнатурой DevSession"
            )
        return candidates[0] if candidates else None

    def capture(self, pid: int) -> ProcessIdentity | None:
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                self._launch_handles.pop(pid, None)
                return None
            identity = self._identity_from_process(process, pid=pid)
            expectation = self._launch_expectations.get(pid)
            if expectation is not None:
                environment, session_id = expectation
                exact_identity = self.identity_belongs_to_session(
                    environment, session_id, identity
                )
                redirector_launcher = (
                    _IS_WINDOWS
                    and self._is_redirector_launcher_signature(
                        environment, session_id, identity
                    )
                )
                if not exact_identity and not redirector_launcher:
                    self._abort_unverified_launch(pid)
                    return None
                if (
                    _IS_WINDOWS
                    and pid in self._launch_handles
                    and _same_path(identity.executable, str(environment.python_executable))
                ):
                    redirected = self._capture_windows_redirected_child(
                        pid,
                        environment,
                        session_id,
                    )
                    if redirected is not None:
                        self._launch_expectations.pop(pid, None)
                        self._launch_expectations[redirected.pid] = (
                            environment,
                            session_id,
                        )
                        self._launch_handles.pop(pid, None)
                        return redirected
                    if redirector_launcher and not exact_identity:
                        self._abort_unverified_launch(pid)
                        return None
            self._launch_handles.pop(pid, None)
            return identity
        except psutil.NoSuchProcess:
            self._launch_handles.pop(pid, None)
            return None
        except Exception as exc:
            if pid in self._launch_handles:
                self._abort_unverified_launch(pid)
            raise RuntimeError(
                f"Не удалось получить идентичность процесса DevSession PID {pid}: {exc}"
            ) from exc

    @staticmethod
    def _is_redirector_launcher_signature(
        environment: DevEnvironment,
        session_id: str,
        identity: ProcessIdentity,
    ) -> bool:
        """Проверить argv Windows venv launcher до adoption exact runtime-child."""

        expected = ProcessBackend.expected_command(environment, session_id)
        if identity.pid <= 0 or len(identity.command_line) != len(expected):
            return False
        if not _same_path(identity.executable, expected[0]):
            return False
        if not _same_path(identity.command_line[0], expected[0]):
            return False
        if not _same_path(identity.command_line[1], expected[1]):
            return False
        return identity.command_line[2:] == tuple(expected[2:])

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if not self._identity_is_destructively_trusted(identity):
            return False
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
                            "Найден процесс с идентификатором DevSession, но его "
                            "executable/cwd нельзя подтвердить"
                        )
                    try:
                        process_pid = int(info["pid"])
                        created_at = float(info["create_time"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeError(
                            "Найден процесс с идентификатором DevSession, но его "
                            "PID/create time нельзя подтвердить"
                        ) from exc

                    identity = ProcessIdentity(
                        pid=process_pid,
                        created_at=created_at,
                        executable=str(Path(str(raw_executable)).absolute()),
                        command_line=cmdline,
                        cwd=str(Path(str(raw_cwd)).absolute()),
                    )
                    if not self.identity_belongs_to_session(
                        environment,
                        session_id,
                        identity,
                        profile_name=environment.profile_name,
                    ):
                        raise RuntimeError(
                            "Найден процесс с идентификатором DevSession, но его "
                            "полная process identity не соответствует ожидаемой сигнатуре"
                        )
                    found.append(identity)
                except (
                    psutil.AccessDenied,
                    psutil.NoSuchProcess,
                ):
                    continue
                except (OSError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Найден процесс с идентификатором DevSession, но его "
                        "process identity нельзя безопасно проверить"
                    ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Не удалось найти процесс DevSession по идентификатору сессии: {exc}"
            ) from exc
        return tuple(found)

    def is_descendant(self, child_pid: int, parent: ProcessIdentity) -> bool:
        if child_pid == parent.pid:
            try:
                return self.matches(parent) is True
            except RuntimeError:
                return False
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

    def _windows_process_group_id(self, identity: ProcessIdentity) -> int | None:
        """Найти отдельную process group, созданную project venv launcher."""
        session_id = identity.command_session_id()
        if session_id is None:
            return None
        root = Path(identity.cwd)
        expected_python = root / ".venv" / "Scripts" / "python.exe"
        try:
            run_index = identity.command_line.index("--run")
            profile_name = identity.command_line[run_index + 1]
            target = DevTarget(profile_name)
        except (IndexError, ValueError):
            return None
        if not identity.matches_dev_contract(
            root,
            session_id,
            expected_python,
            profile_name=profile_name,
        ):
            return None

        if (
            _same_path(identity.executable, str(expected_python))
            and _same_path(identity.command_line[0], str(expected_python))
        ):
            return identity.pid

        try:
            process = psutil.Process(identity.pid)
            parent = process.parent()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return None
        if parent is None:
            return None

        try:
            launcher_identity = self._identity_from_process(parent)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            return None
        if not _same_path(launcher_identity.executable, str(expected_python)):
            return None
        # Windows venv redirector может сообщить собственный transient cwd,
        # отличающийся от cwd репозитория. Child уже подтверждает contract
        # repository/cwd/session, поэтому для group owner проверяем exact
        # executable и argv launcher.
        if not self._is_redirector_launcher_signature(
            DevEnvironment(
                repository_root=root,
                python_executable=expected_python,
                dev_target=target,
            ),
            session_id,
            launcher_identity,
        ):
            return None
        return launcher_identity.pid

    def request_stop(self, identity: ProcessIdentity) -> bool:
        if not self._identity_is_destructively_trusted(identity):
            return False
        try:
            if self.matches(identity) is not True:
                return False
            if _IS_WINDOWS:
                if _WINDOWS_CTRL_BREAK_EVENT is None:
                    return False
                process_group_id = self._windows_process_group_id(identity)
                if process_group_id is None:
                    return False
                os.kill(process_group_id, _WINDOWS_CTRL_BREAK_EVENT)
            else:
                os.kill(identity.pid, signal.SIGINT)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def wait_exit(self, identity: ProcessIdentity, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                matches = self.matches(identity)
            except RuntimeError:
                return False
            if matches is None:
                return True
            if matches is False:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))

    def force_stop(self, identity: ProcessIdentity) -> bool:
        if not self._identity_is_destructively_trusted(identity):
            return False
        try:
            if self.matches(identity) is not True:
                return False
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
                pass
            _, alive = psutil.wait_procs([root, *children], timeout=5)
            if alive:
                return False
            return self.matches(identity) is None
        except Exception:
            return False


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        try:
            return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
                os.path.abspath(right)
            )
        except (OSError, RuntimeError, ValueError):
            return False


def _normalized_command_line(command_line: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in command_line:
        if item.casefold().endswith(
            ("python.exe", "pythonw.exe", "python")
        ) or item.casefold().endswith("gui.py"):
            try:
                normalized.append(os.path.normcase(os.path.abspath(item)))
                continue
            except (OSError, RuntimeError, ValueError):
                pass
        normalized.append(item)
    return tuple(normalized)
