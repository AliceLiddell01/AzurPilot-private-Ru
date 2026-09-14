"""Владелец локальных Game/Dev MCP Streamable HTTP процессов.

Supervisor запускает ровно по одному loopback-процессу для каждого MCP,
проверяет readiness и завершает принадлежащие ему процессы при остановке.
Состояние и lock находятся в игнорируемом ``config/state`` и не содержат
bearer-токены.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.25
STOP_TIMEOUT_SECONDS = 8.0
_STATE_DIRECTORY = Path("config") / "state" / "local-mcp-http"
_LOCK_NAME = "supervisor.lock"
_MARKER_NAME = "supervisor.json"


@dataclass(frozen=True, slots=True)
class LocalHttpService:
    """Описание одного принадлежащего supervisor MCP процесса."""

    name: str
    module: str
    port: int
    token_env_var: str


LOCAL_HTTP_SERVICES = (
    LocalHttpService(
        name="azurpilot-dev",
        module="module.dev_mcp.local_http",
        port=8775,
        token_env_var="AZURPILOT_DEV_LOCAL_MCP_TOKEN",
    ),
    LocalHttpService(
        name="azurpilot-game",
        module="module.game_mcp.local_http",
        port=8776,
        token_env_var="AZURPILOT_GAME_LOCAL_MCP_TOKEN",
    ),
)


class LocalHttpSupervisorError(RuntimeError):
    """Supervisor не может безопасно подтвердить ownership/readiness."""


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )
    except OSError, TypeError, ValueError:
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError as exc:
        raise LocalHttpSupervisorError(
            "Нельзя проверить путь local MCP supervisor"
        ) from exc


def _ensure_state_directory(repository_root: Path) -> Path:
    state_root = repository_root / "config" / "state"
    for candidate in (repository_root, repository_root / "config", state_root):
        if os.path.lexists(candidate) and _is_reparse_point(candidate):
            raise LocalHttpSupervisorError(
                "Путь local MCP supervisor не должен проходить через ссылку или junction"
            )
    state_directory = repository_root / _STATE_DIRECTORY
    state_directory.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(state_directory):
        raise LocalHttpSupervisorError(
            "Каталог local MCP supervisor не должен быть ссылкой или junction"
        )
    return state_directory


def _try_lock(path: Path):
    """Захватить process-held lock без ожидания и вернуть открытый handle."""

    try:
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError, OSError:
        try:
            handle.close()
        except UnboundLocalError:
            pass
        return None
    return handle


def _release_lock(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError, ValueError:
        pass
    finally:
        handle.close()


def _bounded_token_present(name: str) -> bool:
    value = os.environ.get(name, "")
    return bool(
        value
        and len(value.encode("utf-8")) <= 16 * 1024
        and not any(character.isspace() for character in value)
    )


def _process_identity(pid: int) -> dict[str, object] | None:
    try:
        process = psutil.Process(pid)
        return {
            "pid": int(pid),
            "created_at": float(process.create_time()),
            "executable": str(Path(process.exe()).absolute()),
            "command": list(process.cmdline()),
            "cwd": str(Path(process.cwd()).absolute()),
        }
    except psutil.Error, OSError, TypeError, ValueError:
        return None


def _identity_matches(actual: psutil.Process, expected: dict[str, object]) -> bool:
    try:
        expected_pid = int(expected["pid"])
        expected_created_at = float(expected["created_at"])
        expected_executable = str(expected["executable"])
        expected_command = tuple(str(item) for item in expected["command"])
        expected_cwd = str(expected["cwd"])
        return (
            actual.pid == expected_pid
            and abs(actual.create_time() - expected_created_at) < 0.01
            and _same_path(actual.exe(), expected_executable)
            and tuple(actual.cmdline()) == expected_command
            and _same_path(actual.cwd(), expected_cwd)
        )
    except psutil.Error, OSError, TypeError, ValueError, KeyError:
        return False


class LocalHttpSupervisor:
    """Запустить и удерживать exact-owned Game/Dev local HTTP processes."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        python_executable: Path | str | None = None,
        services: Iterable[LocalHttpService] = LOCAL_HTTP_SERVICES,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.repository_root = Path(repository_root).absolute()
        self.services = tuple(services)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.python_executable = (
            Path(python_executable).absolute()
            if python_executable
            else self._default_python()
        )
        self.state_directory = _ensure_state_directory(self.repository_root)
        self.lock_path = self.state_directory / _LOCK_NAME
        self.marker_path = self.state_directory / _MARKER_NAME
        self._lock_handle: Any | None = None
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._runtime_processes: dict[str, psutil.Process] = {}

    def _default_python(self) -> Path:
        relative = (
            Path(".venv") / "Scripts" / "python.exe"
            if os.name == "nt"
            else Path(".venv") / "bin" / "python"
        )
        candidate = self.repository_root / relative
        return candidate if candidate.is_file() else Path(sys.executable).absolute()

    def _validate(self) -> None:
        if not (self.repository_root / "pyproject.toml").is_file():
            raise LocalHttpSupervisorError(
                "Корень local MCP supervisor не содержит pyproject.toml"
            )
        if not self.python_executable.is_file():
            raise LocalHttpSupervisorError(
                "Project Python для local MCP supervisor не найден"
            )
        if not 1 <= len(self.services) <= 8:
            raise LocalHttpSupervisorError(
                "Каталог local MCP services имеет неверный размер"
            )
        seen_ports: set[int] = set()
        seen_names: set[str] = set()
        for service in self.services:
            if service.name in seen_names or service.port in seen_ports:
                raise LocalHttpSupervisorError(
                    "Каталог local MCP services содержит дубликат"
                )
            if not service.name or not service.module or not service.token_env_var:
                raise LocalHttpSupervisorError("Каталог local MCP service неполон")
            if not 1 <= service.port <= 65535:
                raise LocalHttpSupervisorError("Порт local MCP service вне диапазона")
            if not _bounded_token_present(service.token_env_var):
                raise LocalHttpSupervisorError(
                    f"Переменная {service.token_env_var} для local MCP не задана"
                )
            seen_names.add(service.name)
            seen_ports.add(service.port)
        if not 1 <= self.startup_timeout_seconds <= 120:
            raise LocalHttpSupervisorError(
                "Timeout запуска local MCP supervisor вне диапазона"
            )

    def _command(self, service: LocalHttpService) -> list[str]:
        return [
            str(self.python_executable),
            "-u",
            "-m",
            service.module,
        ]

    def _log_path(self, service: LocalHttpService) -> Path:
        return self.state_directory / f"{service.name}.stderr.log"

    def _spawn(self, service: LocalHttpService) -> subprocess.Popen[bytes]:
        command = self._command(service)
        try:
            stderr = self._log_path(service).open("ab")
            kwargs: dict[str, Any] = {
                "cwd": str(self.repository_root),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": stderr,
                "env": os.environ.copy(),
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
        except (OSError, subprocess.SubprocessError) as exc:
            try:
                stderr.close()
            except UnboundLocalError:
                pass
            raise LocalHttpSupervisorError(
                f"Не удалось запустить owned local MCP service {service.name}"
            ) from exc
        finally:
            try:
                stderr.close()
            except UnboundLocalError:
                pass
        return process

    def _effective_process(
        self, service: LocalHttpService, launcher: subprocess.Popen[bytes]
    ) -> psutil.Process:
        """Найти фактический runtime-child Windows venv redirector."""

        if os.name != "nt":
            try:
                return psutil.Process(launcher.pid)
            except psutil.Error as exc:
                raise LocalHttpSupervisorError(
                    f"Нельзя получить identity local MCP service {service.name}"
                ) from exc

        deadline = time.monotonic() + min(self.startup_timeout_seconds, 10.0)
        while True:
            try:
                parent = psutil.Process(launcher.pid)
                candidates = parent.children(recursive=True)
            except psutil.Error as exc:
                raise LocalHttpSupervisorError(
                    f"Нельзя перечислить runtime-child local MCP service {service.name}"
                ) from exc
            for candidate in sorted(
                candidates, key=lambda item: item.create_time(), reverse=True
            ):
                try:
                    command = tuple(str(item).strip() for item in candidate.cmdline())
                    if (
                        len(command) >= 4
                        and command[-3:] == ("-u", "-m", service.module)
                        and _same_path(candidate.cwd(), self.repository_root)
                    ):
                        return candidate
                except (psutil.Error, OSError, TypeError):
                    continue
            if launcher.poll() is not None:
                raise LocalHttpSupervisorError(
                    f"Local MCP service {service.name} завершился до runtime-child"
                )
            if time.monotonic() >= deadline:
                raise LocalHttpSupervisorError(
                    f"Runtime-child local MCP service {service.name} не найден"
                )
            time.sleep(0.05)

    def _runtime_is_alive(self, service: LocalHttpService) -> bool:
        """Проверить жизнь exact-owned runtime без zombie false-positive."""

        if os.name != "nt":
            launcher = self._children.get(service.name)
            if launcher is not None:
                # На POSIX завершившийся child может оставаться zombie, пока
                # родитель не вызвал wait(). Popen.poll() видит это состояние
                # точно для процесса, запущенного этим supervisor.
                return launcher.poll() is None
        try:
            return self._runtime_processes[service.name].is_running()
        except (KeyError, psutil.Error):
            return False

    @staticmethod
    def _ready(service: LocalHttpService) -> bool:
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", service.port, timeout=1.0
            )
            connection.request(
                "GET",
                "/ready",
                headers={"Host": f"127.0.0.1:{service.port}"},
            )
            response = connection.getresponse()
            body = response.read(16 * 1024)
            payload = json.loads(body.decode("utf-8"))
            return (
                response.status == 200
                and payload.get("ok") is True
                and payload.get("code") == "LOCAL_MCP_READY"
                and payload.get("server_name") == service.name
                and payload.get("transport") == "local_http"
            )
        except OSError, ValueError, TypeError, json.JSONDecodeError:
            return False
        finally:
            if connection is not None:
                connection.close()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        pending = {service.name: service for service in self.services}
        while pending:
            for name, service in tuple(pending.items()):
                if not self._runtime_is_alive(service):
                    raise LocalHttpSupervisorError(
                        f"Local MCP service {name} завершился до readiness"
                    )
                if self._ready(service):
                    del pending[name]
            if pending and time.monotonic() >= deadline:
                names = ", ".join(sorted(pending))
                raise LocalHttpSupervisorError(
                    f"Local MCP services не достигли readiness: {names}"
                )
            if pending:
                time.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _port_is_in_use(service: LocalHttpService) -> bool:
        connection: http.client.HTTPConnection | None = None
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", service.port, timeout=0.25
            )
            connection.connect()
            return True
        except OSError:
            return False
        finally:
            if connection is not None:
                connection.close()

    def _supervisor_identity(self) -> dict[str, object]:
        identity = _process_identity(os.getpid())
        if identity is None:
            raise LocalHttpSupervisorError(
                "Нельзя подтвердить identity local MCP supervisor"
            )
        if os.name == "nt":
            try:
                parent = psutil.Process(os.getpid()).parent()
                parent_command = tuple(
                    str(item).strip() for item in parent.cmdline()
                )
                if (
                    len(parent_command) >= 5
                    and parent_command[-4:]
                    == (
                        "-u",
                        "-m",
                        "module.mcp_shared.local_http_supervisor",
                        "serve",
                    )
                    and _same_path(parent.exe(), self.python_executable)
                ):
                    launcher = _process_identity(parent.pid)
                    if launcher is not None:
                        identity["launcher_process"] = launcher
            except (psutil.Error, OSError, TypeError):
                pass
        return identity

    def _write_marker(self) -> None:
        service_processes = []
        for service in self.services:
            runtime_process = self._runtime_processes[service.name]
            process = _process_identity(runtime_process.pid)
            if process is None:
                raise LocalHttpSupervisorError(
                    f"Нельзя подтвердить identity local MCP service {service.name}"
                )
            launcher = _process_identity(self._children[service.name].pid)
            if launcher is None:
                raise LocalHttpSupervisorError(
                    f"Нельзя подтвердить launcher local MCP service {service.name}"
                )
            service_processes.append((service, process, launcher))
        payload = {
            "schema_version": 1,
            "repository_root": str(self.repository_root),
            "python_executable": str(self.python_executable),
            "supervisor": self._supervisor_identity(),
            "services": [
                {
                    "name": service.name,
                    "port": service.port,
                    "token_env_var": service.token_env_var,
                    "process": process,
                    "launcher_process": launcher,
                }
                for service, process, launcher in service_processes
            ],
        }
        temporary = self.marker_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.marker_path)

    def _remove_owned_marker(self) -> None:
        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
            supervisor = payload.get("supervisor", {})
            if int(supervisor.get("pid", -1)) != os.getpid():
                return
        except OSError, ValueError, TypeError, AttributeError:
            return
        try:
            self.marker_path.unlink()
        except OSError:
            pass

    def _remove_recorded_marker(self, payload: object) -> None:
        """Удалить marker только если он всё ещё описывает exact owner."""

        if not isinstance(payload, dict) or not isinstance(
            payload.get("supervisor"), dict
        ):
            return
        try:
            current = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(current, dict) or current.get("supervisor") != payload[
            "supervisor"
        ]:
            return
        try:
            self.marker_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _terminate_recorded_identity(expected: dict[str, object]) -> None:
        pid = expected.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return
        try:
            process = psutil.Process(pid)
            if not _identity_matches(process, expected) or not process.is_running():
                return
            process.terminate()
            try:
                process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except psutil.TimeoutExpired:
                if _identity_matches(process, expected) and process.is_running():
                    process.kill()
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except psutil.NoSuchProcess:
            return
        except (psutil.Error, OSError, subprocess.SubprocessError):
            logger.error("Не удалось завершить owned local MCP process")

    @classmethod
    def _identity_tree(cls, expected: dict[str, object]) -> list[dict[str, object]]:
        """Собрать exact identity корня и всех его текущих descendants."""

        pid = expected.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            return []
        try:
            root = psutil.Process(pid)
            if not _identity_matches(root, expected):
                return []
            descendants = root.children(recursive=True)
        except (psutil.Error, OSError, TypeError, ValueError):
            return []

        identities: list[dict[str, object]] = []
        seen: set[tuple[int, float]] = set()

        def add(identity: dict[str, object] | None) -> None:
            if identity is None:
                return
            try:
                key = (int(identity["pid"]), float(identity["created_at"]))
            except (KeyError, TypeError, ValueError):
                return
            if key not in seen:
                seen.add(key)
                identities.append(identity)

        for descendant in reversed(descendants):
            add(_process_identity(descendant.pid))
        add(expected)
        return identities

    @classmethod
    def _terminate_process(cls, process: psutil.Process) -> None:
        identity = _process_identity(process.pid)
        if identity is None or not _identity_matches(process, identity):
            return
        for descendant in cls._identity_tree(identity):
            cls._terminate_recorded_identity(descendant)

    def _stop_children(self) -> None:
        stopped_pids: set[int] = set()
        for process in tuple(self._runtime_processes.values()):
            if process.pid not in stopped_pids:
                self._terminate_process(process)
                stopped_pids.add(process.pid)
        for launcher in tuple(self._children.values()):
            if launcher.pid in stopped_pids or launcher.poll() is not None:
                continue
            try:
                process = psutil.Process(launcher.pid)
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
            self._terminate_process(process)
            stopped_pids.add(launcher.pid)
        self._children.clear()
        self._runtime_processes.clear()

    def serve(self) -> bool:
        """Запустить services и удерживать lock до штатной остановки.

        Возвращает ``False``, если другой exact-scoped supervisor уже владеет
        lock; это штатный результат повторного запуска из Desktop shortcut.
        """

        _ensure_state_directory(self.repository_root)
        lock_handle = _try_lock(self.lock_path)
        if lock_handle is None:
            return False
        self._lock_handle = lock_handle
        stop_requested = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        previous_handlers: dict[int, Any] = {}
        for signum in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM)):
            try:
                previous_handlers[signum] = signal.signal(signum, request_stop)
            except ValueError, OSError:
                pass
        try:
            self._validate()
            for service in self.services:
                if self._port_is_in_use(service):
                    raise LocalHttpSupervisorError(
                        f"Порт local MCP service {service.name} уже занят"
                    )
                launcher = self._spawn(service)
                self._children[service.name] = launcher
                self._runtime_processes[service.name] = self._effective_process(
                    service, launcher
                )
            self._wait_ready()
            self._write_marker()
            while not stop_requested:
                for service in self.services:
                    if not self._runtime_is_alive(service):
                        raise LocalHttpSupervisorError(
                            f"Local MCP service {service.name} неожиданно завершился"
                        )
                time.sleep(POLL_INTERVAL_SECONDS)
            return True
        finally:
            self._stop_children()
            self._remove_owned_marker()
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except ValueError, OSError:
                    pass
            _release_lock(self._lock_handle)
            self._lock_handle = None

    def status(self) -> dict[str, object]:
        """Вернуть bounded состояние marker/process/readiness без token данных."""

        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except OSError, ValueError, TypeError:
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_STOPPED",
                "repository_root": str(self.repository_root),
            }
        if payload.get("repository_root") != str(self.repository_root):
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH",
                "repository_root": str(self.repository_root),
            }
        supervisor = payload.get("supervisor")
        if not isinstance(supervisor, dict):
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                "repository_root": str(self.repository_root),
            }
        supervisor_pid = supervisor.get("pid")
        if isinstance(supervisor_pid, bool) or not isinstance(supervisor_pid, int):
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                "repository_root": str(self.repository_root),
            }
        try:
            supervisor_process = psutil.Process(supervisor_pid)
        except (psutil.Error, OSError, TypeError, ValueError):
            supervisor_process = None
        if supervisor_process is None or not _identity_matches(
            supervisor_process, supervisor
        ):
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_STALE",
                "repository_root": str(self.repository_root),
            }
        marker_services = payload.get("services")
        if not isinstance(marker_services, list) or len(marker_services) != len(
            self.services
        ):
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                "repository_root": str(self.repository_root),
            }
        services: list[dict[str, object]] = []
        for expected_service in self.services:
            item = next(
                (
                    candidate
                    for candidate in marker_services
                    if isinstance(candidate, dict)
                    and candidate.get("name") == expected_service.name
                ),
                None,
            )
            if item is None or item.get("port") != expected_service.port:
                return {
                    "ok": False,
                    "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                    "repository_root": str(self.repository_root),
                }
            child = item.get("process")
            child_pid = child.get("pid") if isinstance(child, dict) else None
            if (
                isinstance(child_pid, bool)
                or not isinstance(child_pid, int)
                or not isinstance(child, dict)
            ):
                return {
                    "ok": False,
                    "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                    "repository_root": str(self.repository_root),
                }
            child_identity = _process_identity(child_pid)
            alive = False
            if child_identity is not None:
                try:
                    alive = _identity_matches(psutil.Process(child_pid), child)
                except psutil.Error, OSError, TypeError, ValueError, KeyError:
                    alive = False
            services.append(
                {
                    "server_name": expected_service.name,
                    "port": expected_service.port,
                    "alive": alive,
                    "ready": bool(alive and self._ready(expected_service)),
                }
            )
        ready = bool(services) and all(
            item["alive"] and item["ready"] for item in services
        )
        return {
            "ok": ready,
            "code": "LOCAL_MCP_SUPERVISOR_READY"
            if ready
            else "LOCAL_MCP_SUPERVISOR_DEGRADED",
            "repository_root": str(self.repository_root),
            "supervisor_pid": supervisor_pid,
            "services": services,
        }

    @staticmethod
    def _terminate_recorded_processes(payload: object) -> None:
        """Завершить только recorded owner/service trees из marker."""

        if not isinstance(payload, dict):
            return
        records: list[object] = []
        supervisor = payload.get("supervisor")
        if isinstance(supervisor, dict):
            records.append(supervisor)
        services = payload.get("services")
        if isinstance(services, list):
            records.extend(services)
        for item in records:
            if not isinstance(item, dict):
                continue
            if item is supervisor:
                expected_records = [item, item.get("launcher_process")]
            else:
                expected_records = [item.get("process"), item.get("launcher_process")]
            for expected in expected_records:
                if not isinstance(expected, dict):
                    continue
                for identity in LocalHttpSupervisor._identity_tree(expected):
                    LocalHttpSupervisor._terminate_recorded_identity(identity)

    def stop(self) -> bool:
        """Остановить только supervisor с exact recorded identity."""

        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except OSError, ValueError, TypeError:
            return False
        if payload.get("repository_root") != str(self.repository_root):
            return False
        supervisor = payload.get("supervisor")
        if not isinstance(supervisor, dict):
            return False
        try:
            process = psutil.Process(int(supervisor["pid"]))
        except psutil.Error, KeyError, TypeError, ValueError:
            self._terminate_recorded_processes(payload)
            self._remove_recorded_marker(payload)
            return False
        if not _identity_matches(process, supervisor):
            self._terminate_recorded_processes(payload)
            self._remove_recorded_marker(payload)
            return False
        try:
            # Не использовать console/group control events: на Windows такой
            # сигнал может затронуть текущий Codex control plane. Exact
            # identity уже подтверждена, поэтому завершаем только этот PID.
            process.terminate()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
            return not process.is_running()
        except psutil.TimeoutExpired:
            # Exact identity всё ещё подтверждена; fallback не ищет процессы по
            # имени и не трогает чужие PID.
            try:
                if not _identity_matches(process, supervisor):
                    return False
                process.terminate()
                try:
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
                except psutil.TimeoutExpired:
                    if _identity_matches(process, supervisor):
                        process.kill()
                        process.wait(timeout=STOP_TIMEOUT_SECONDS)
                return not process.is_running()
            except (OSError, psutil.Error):
                return False
        except psutil.NoSuchProcess:
            return True
        finally:
            self._terminate_recorded_processes(payload)
            self._remove_recorded_marker(payload)


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="AzurPilot local MCP HTTP supervisor")
    parser.add_argument(
        "command", choices=("serve", "status", "stop"), nargs="?", default="serve"
    )
    parser.add_argument("--root", type=Path, default=_default_repository_root())
    args = parser.parse_args()
    supervisor = LocalHttpSupervisor(args.root)
    try:
        if args.command == "status":
            print(
                json.dumps(
                    supervisor.status(), ensure_ascii=False, separators=(",", ":")
                )
            )
            return
        if args.command == "stop":
            print(
                json.dumps(
                    {"ok": supervisor.stop()}, ensure_ascii=False, separators=(",", ":")
                )
            )
            return
        if not supervisor.serve():
            logger.info("Local MCP supervisor уже запущен другим владельцем")
    except LocalHttpSupervisorError as exc:
        logger.error("Local MCP supervisor остановлен: %s", exc)
        raise SystemExit(2) from None


__all__ = (
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "LOCAL_HTTP_SERVICES",
    "LocalHttpService",
    "LocalHttpSupervisor",
    "LocalHttpSupervisorError",
    "main",
)


if __name__ == "__main__":  # pragma: no cover
    main()
