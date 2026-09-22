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
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import psutil

from azurpilot.tooling.mcp_coordination import FileLock
from azurpilot.tooling.mcp_errors import ToolingError
from azurpilot.tooling.process_core import (
    MCP_LOCAL_TEST_ENVIRONMENT_PREFIX,
    ProcessController,
    ProcessIdentity,
    ProcessSpec,
    RunningProcess,
    StructuredProcessRunner,
)
from module.mcp_shared.versioning import SOURCE_REVISION_ENV

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL_SECONDS = 0.25
STOP_TIMEOUT_SECONDS = 8.0
_STATE_DIRECTORY = Path("config") / "state" / "local-mcp-http"
_LOCK_NAME = "supervisor.lock"
_MARKER_NAME = "supervisor.json"
_SOURCE_REVISION_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SOURCE_SET_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
LOCAL_HTTP_SOURCE_SET_DIGEST_ENV_VARS = {
    "azurpilot-dev": "AZURPILOT_DEV_MCP_SOURCE_SET_DIGEST",
    "azurpilot-game": "AZURPILOT_GAME_MCP_SOURCE_SET_DIGEST",
}


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


class LocalHttpSupervisorStopOutcome(StrEnum):
    """Закрытые результаты bounded stop/recovery операции supervisor."""

    EXACT_LIVE_OWNER_STOPPED = "exact_live_owner_stopped"
    ALREADY_STOPPED = "already_stopped"
    STALE_RECORDED_OWNER_RECOVERED = "stale_recorded_owner_recovered"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    INVALID_MARKER = "invalid_marker"
    UNKNOWN_RECOVERY = "unknown_recovery"
    TERMINATION_FAILED = "termination_failed"
    MARKER_CHANGED = "marker_changed"
    POSTCONDITION_FAILED = "postcondition_failed"
    PORT_CONFLICT = "port_conflict"


@dataclass(frozen=True, slots=True)
class LocalHttpSupervisorStopResult:
    """Доказательный результат остановки без неоднозначного bool contract."""

    outcome: LocalHttpSupervisorStopOutcome
    marker_present: bool
    marker_removed: bool = False
    ownership_confirmed: bool = False
    postcondition_confirmed: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in {
            LocalHttpSupervisorStopOutcome.EXACT_LIVE_OWNER_STOPPED,
            LocalHttpSupervisorStopOutcome.ALREADY_STOPPED,
            LocalHttpSupervisorStopOutcome.STALE_RECORDED_OWNER_RECOVERED,
        }


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )
    except (OSError, TypeError, ValueError):
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


def _bounded_token_present(name: str) -> bool:
    value = os.environ.get(name, "")
    return bool(
        value
        and len(value.encode("utf-8")) <= 16 * 1024
        and not any(character.isspace() for character in value)
    )


def _identity_to_marker(identity: ProcessIdentity) -> dict[str, object]:
    """Сериализовать canonical ProcessIdentity в совместимый marker DTO."""

    marker: dict[str, object] = {
        "pid": identity.pid,
        "created_at": identity.start_time,
        "executable": str(identity.executable),
        "command": list(identity.argv),
        "cwd": str(identity.cwd),
    }
    if identity.process_group is not None:
        marker["process_group"] = identity.process_group
    return marker


def _identity_from_marker(value: object) -> ProcessIdentity | None:
    """Прочитать marker DTO без повторения identity/ownership алгоритма."""

    if not isinstance(value, dict):
        return None
    try:
        pid = value["pid"]
        created_at = value["created_at"]
        executable = value["executable"]
        command = value["command"]
        cwd = value["cwd"]
        process_group = value.get("process_group")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not isinstance(executable, str)
            or not isinstance(command, list)
            or any(not isinstance(item, str) for item in command)
            or not isinstance(cwd, str)
            or (
                process_group is not None
                and (isinstance(process_group, bool) or not isinstance(process_group, int))
            )
        ):
            return None
        return ProcessIdentity(
            pid=pid,
            start_time=float(created_at),
            executable=Path(executable),
            argv=tuple(command),
            cwd=Path(cwd),
            process_group=process_group,
        )
    except (TypeError, ValueError, KeyError, OSError):
        return None


def _valid_recorded_identity(identity: ProcessIdentity) -> bool:
    """Проверить bounded поля identity до любой операции с процессом."""

    return bool(
        identity.pid > 0
        and math.isfinite(identity.start_time)
        and str(identity.executable)
        and identity.argv
        and str(identity.cwd)
    )


class LocalHttpSupervisor:
    """Запустить и удерживать exact-owned Game/Dev local HTTP processes."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        python_executable: Path | str | None = None,
        services: Iterable[LocalHttpService] = LOCAL_HTTP_SERVICES,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        runner: StructuredProcessRunner | None = None,
        state_namespace: str | None = None,
        allow_test_environment: bool = False,
    ) -> None:
        self.repository_root = Path(repository_root).absolute()
        self.services = tuple(services)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.python_executable = (
            Path(python_executable).absolute()
            if python_executable
            else self._default_python()
        )
        state_directory = _ensure_state_directory(self.repository_root)
        if state_namespace is not None:
            if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", state_namespace) is None:
                raise LocalHttpSupervisorError(
                    "Namespace local MCP supervisor имеет неверный формат"
                )
            state_directory = state_directory / state_namespace
            state_directory.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(state_directory):
                raise LocalHttpSupervisorError(
                    "Namespace local MCP supervisor не должен быть ссылкой или junction"
                )
        self.state_directory = state_directory
        self.lock_path = self.state_directory / _LOCK_NAME
        self.marker_path = self.state_directory / _MARKER_NAME
        self._lock_handle: Any | None = None
        self.runner = runner or StructuredProcessRunner()
        self.allow_test_environment = allow_test_environment
        self._children: dict[str, RunningProcess] = {}
        self._runtime_processes: dict[str, ProcessIdentity] = {}

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

    def _spawn(self, service: LocalHttpService) -> RunningProcess:
        command = self._command(service)
        try:
            token = os.environ.get(service.token_env_var, "")
            explicit_env = {
                service.token_env_var: token,
                "PYTHONUNBUFFERED": "1",
            }
            revision = os.environ.get(SOURCE_REVISION_ENV, "").strip().lower()
            if _SOURCE_REVISION_RE.fullmatch(revision):
                explicit_env[SOURCE_REVISION_ENV] = revision
            digest_env = LOCAL_HTTP_SOURCE_SET_DIGEST_ENV_VARS.get(service.name)
            digest = os.environ.get(digest_env or "", "").strip().lower()
            if digest_env and _SOURCE_SET_DIGEST_RE.fullmatch(digest):
                explicit_env[digest_env] = digest
            environment = dict(explicit_env)
            if self.allow_test_environment:
                environment.update(
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key.startswith(MCP_LOCAL_TEST_ENVIRONMENT_PREFIX)
                    }
                )
            running = self.runner.start(
                ProcessSpec(
                    executable=self.python_executable,
                    argv=tuple(command[1:]),
                    cwd=self.repository_root,
                    timeout_seconds=max(30.0, self.startup_timeout_seconds + 10.0),
                    env=environment,
                    allow_test_environment=self.allow_test_environment,
                )
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise LocalHttpSupervisorError(
                f"Не удалось запустить owned local MCP service {service.name}"
            ) from exc
        return running

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
            return self._runtime_processes[service.name].matches()
        except (KeyError, psutil.Error):
            return False

    @staticmethod
    def _ready_payload(service: LocalHttpService) -> dict[str, object] | None:
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
            if not (
                response.status == 200
                and payload.get("ok") is True
                and payload.get("code") == "LOCAL_MCP_READY"
                and payload.get("server_name") == service.name
                and payload.get("transport") == "local_http"
            ):
                return None
            return {
                key: payload[key]
                for key in (
                    "server_name",
                    "server_version",
                    "source_revision",
                    "source_set_digest",
                    "tool_catalog_sha256",
                    "capability_catalog_sha256",
                    "contract_revision",
                )
                if key in payload
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        finally:
            if connection is not None:
                connection.close()

    @classmethod
    def _ready(cls, service: LocalHttpService) -> bool:
        return cls._ready_payload(service) is not None

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

    def port_conflicts(self) -> tuple[str, ...]:
        """Вернуть first-party services, чьи loopback-порты уже заняты."""

        return tuple(
            service.name
            for service in self.services
            if self._port_is_in_use(service)
        )

    def _supervisor_identity(self) -> dict[str, object]:
        try:
            identity = ProcessIdentity.capture(os.getpid())
        except (OSError, psutil.Error) as exc:
            raise LocalHttpSupervisorError(
                "Нельзя подтвердить identity local MCP supervisor"
            ) from exc
        marker = _identity_to_marker(identity)
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
                    launcher = ProcessIdentity.capture(parent.pid)
                    marker["launcher_process"] = _identity_to_marker(launcher)
            except (psutil.Error, OSError, TypeError):
                pass
        return marker

    def _write_marker(self) -> None:
        service_processes = []
        for service in self.services:
            try:
                process = self._runtime_processes[service.name]
                launcher = self._children[service.name].identity
            except KeyError as exc:
                raise LocalHttpSupervisorError(
                    f"Нельзя подтвердить identity local MCP service {service.name}"
                ) from exc
            service_processes.append(
                (service, _identity_to_marker(process), _identity_to_marker(launcher))
            )
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
            supervisor = _identity_from_marker(payload.get("supervisor"))
            if supervisor is None or supervisor.pid != os.getpid():
                return
        except (OSError, ValueError, TypeError, AttributeError):
            return
        try:
            self.marker_path.unlink()
        except OSError:
            pass

    def _read_marker_payload(
        self,
    ) -> tuple[
        dict[str, object] | None,
        LocalHttpSupervisorStopOutcome | None,
    ]:
        """Прочитать marker с различением absent, invalid и unknown."""

        try:
            payload = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, UnicodeError):
            return None, LocalHttpSupervisorStopOutcome.UNKNOWN_RECOVERY
        except (TypeError, ValueError):
            return None, LocalHttpSupervisorStopOutcome.INVALID_MARKER
        if not isinstance(payload, dict):
            return None, LocalHttpSupervisorStopOutcome.INVALID_MARKER
        return payload, None

    def _validated_marker_identities(
        self, payload: dict[str, object]
    ) -> tuple[ProcessIdentity, tuple[ProcessIdentity, ...]] | LocalHttpSupervisorStopOutcome:
        """Проверить marker schema и вернуть supervisor/recorded identities."""

        if payload.get("schema_version") != 1:
            return LocalHttpSupervisorStopOutcome.INVALID_MARKER
        if payload.get("repository_root") != str(self.repository_root):
            return LocalHttpSupervisorStopOutcome.OWNERSHIP_MISMATCH
        if payload.get("python_executable") != str(self.python_executable):
            return LocalHttpSupervisorStopOutcome.INVALID_MARKER
        supervisor_value = payload.get("supervisor")
        supervisor = _identity_from_marker(supervisor_value)
        if supervisor is None or not _valid_recorded_identity(supervisor):
            return LocalHttpSupervisorStopOutcome.INVALID_MARKER
        marker_services = payload.get("services")
        if not isinstance(marker_services, list) or len(marker_services) != len(
            self.services
        ):
            return LocalHttpSupervisorStopOutcome.INVALID_MARKER

        service_records: dict[str, dict[str, object]] = {}
        for item in marker_services:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                return LocalHttpSupervisorStopOutcome.INVALID_MARKER
            name = item["name"]
            if name in service_records:
                return LocalHttpSupervisorStopOutcome.INVALID_MARKER
            service_records[name] = item

        identities: list[ProcessIdentity] = [supervisor]
        for service in self.services:
            item = service_records.get(service.name)
            if item is None or item.get("port") != service.port:
                return LocalHttpSupervisorStopOutcome.INVALID_MARKER
            if item.get("token_env_var") != service.token_env_var:
                return LocalHttpSupervisorStopOutcome.INVALID_MARKER
            process = _identity_from_marker(item.get("process"))
            launcher = _identity_from_marker(item.get("launcher_process"))
            if (
                process is None
                or launcher is None
                or not _valid_recorded_identity(process)
                or not _valid_recorded_identity(launcher)
            ):
                return LocalHttpSupervisorStopOutcome.INVALID_MARKER
            # Сначала останавливать родительские launchers; ProcessController
            # повторно проверяет exact identity и descendants перед каждой
            # остановкой.
            identities.extend((launcher, process))

        unique: list[ProcessIdentity] = []
        seen: set[tuple[object, ...]] = set()
        for identity in identities:
            key = (
                identity.pid,
                identity.start_time,
                str(identity.executable),
                identity.argv,
                str(identity.cwd),
                identity.process_group,
            )
            if key not in seen:
                seen.add(key)
                unique.append(identity)
        return supervisor, tuple(unique)

    def _try_recovery_lock(self) -> FileLock | None:
        """Захватить coordination lock или вернуть unknown без ожидания."""

        lock = FileLock(self.lock_path)
        try:
            if lock.acquire(timeout_seconds=0):
                return lock
        except (OSError, ToolingError):
            return None
        return None

    def _remove_recorded_marker(self, payload: object) -> bool:
        """Удалить marker только при полном unchanged payload."""

        if not isinstance(payload, dict):
            return False
        try:
            current = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            return False
        if not isinstance(current, dict) or current != payload:
            return False
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _safe_identity_matches(identity: ProcessIdentity) -> bool:
        """Не превращать ошибку liveness в разрешение на recovery."""

        try:
            return identity.matches()
        except (OSError, ValueError, psutil.Error):
            return False

    @staticmethod
    def _safe_inspect_state(identity: ProcessIdentity) -> str:
        """Вернуть unknown при любой ошибке проверки процесса."""

        try:
            return ProcessController.inspect_state(identity)
        except (OSError, ValueError, psutil.Error, ToolingError):
            return "unknown"

    @staticmethod
    def _terminate_exact_identity(identity: ProcessIdentity) -> bool:
        """Остановить identity только после typed liveness проверки."""

        state = LocalHttpSupervisor._safe_inspect_state(identity)
        if state == "unknown":
            return False
        if state == "absent":
            return True
        try:
            ProcessController.terminate(identity, timeout_seconds=STOP_TIMEOUT_SECONDS)
        except (OSError, ValueError, psutil.Error, ToolingError):
            return False
        return LocalHttpSupervisor._safe_inspect_state(identity) == "absent"

    def _stopped_postcondition(self) -> bool:
        """Подтвердить marker absent/stopped и отсутствие port conflict."""

        return (
            self.status().get("code") == "LOCAL_MCP_SUPERVISOR_STOPPED"
            and not self.port_conflicts()
        )

    def stop_result(self) -> LocalHttpSupervisorStopResult:
        """Остановить owner или bounded-recover stale marker с typed evidence."""

        payload, read_outcome = self._read_marker_payload()
        if read_outcome is not None:
            return LocalHttpSupervisorStopResult(
                outcome=read_outcome,
                marker_present=True,
                detail="Marker нельзя безопасно прочитать или классифицировать.",
            )
        if payload is None:
            if self.port_conflicts():
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.PORT_CONFLICT,
                    marker_present=False,
                    detail="Marker отсутствует, но owned port занят.",
                )
            return LocalHttpSupervisorStopResult(
                outcome=LocalHttpSupervisorStopOutcome.ALREADY_STOPPED,
                marker_present=False,
                marker_removed=True,
                postcondition_confirmed=True,
                detail="Marker отсутствует; runtime уже остановлен.",
            )

        validated = self._validated_marker_identities(payload)
        if isinstance(validated, LocalHttpSupervisorStopOutcome):
            return LocalHttpSupervisorStopResult(
                outcome=validated,
                marker_present=True,
                detail="Marker ownership/schema не подтверждены.",
            )
        supervisor_identity, identities = validated
        exact_live_owner = self._safe_identity_matches(supervisor_identity)
        if not exact_live_owner and self._safe_inspect_state(supervisor_identity) == "unknown":
            return LocalHttpSupervisorStopResult(
                outcome=LocalHttpSupervisorStopOutcome.UNKNOWN_RECOVERY,
                marker_present=True,
                detail="Liveness recorded supervisor нельзя безопасно подтвердить.",
            )

        lock: FileLock | None = None
        if not exact_live_owner:
            # Stale marker можно очищать только пока новый supervisor не может
            # заменить его параллельно. Второе чтение закрывает race между
            # status() и cleanup.
            lock = self._try_recovery_lock()
            if lock is None:
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.UNKNOWN_RECOVERY,
                    marker_present=True,
                    detail="Coordination lock занят или недоступен.",
                )
            current, current_outcome = self._read_marker_payload()
            if current_outcome is not None:
                lock.release()
                return LocalHttpSupervisorStopResult(
                    outcome=current_outcome,
                    marker_present=True,
                    detail="Marker изменился или стал нечитаемым до cleanup.",
                )
            if current is None or current != payload:
                lock.release()
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.MARKER_CHANGED,
                    marker_present=True,
                    detail="Marker изменился между read и cleanup.",
                )
            current_validated = self._validated_marker_identities(current)
            if isinstance(current_validated, LocalHttpSupervisorStopOutcome):
                lock.release()
                return LocalHttpSupervisorStopResult(
                    outcome=current_validated,
                    marker_present=True,
                    detail="Marker стал invalid/foreign до cleanup.",
                )
            supervisor_identity, identities = current_validated
            if self._safe_identity_matches(supervisor_identity):
                lock.release()
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.UNKNOWN_RECOVERY,
                    marker_present=True,
                    detail="Recorded supervisor стал exact live owner до cleanup.",
                )

        try:
            for identity in identities:
                if identity == supervisor_identity and not exact_live_owner:
                    # Первичная проверка stale классифицировала identity как
                    # отсутствующую или несовпадающую; нельзя останавливать её
                    # только по предположению на основе PID.
                    continue
                if not self._terminate_exact_identity(identity):
                    return LocalHttpSupervisorStopResult(
                        outcome=LocalHttpSupervisorStopOutcome.TERMINATION_FAILED,
                        marker_present=True,
                        ownership_confirmed=exact_live_owner,
                        detail="Остановка recorded exact process или postcondition не подтверждена.",
                    )
            # При foreign listener marker нужно сохранить и заблокировать
            # recovery; нельзя удалять evidence ownership при port conflict.
            if self.port_conflicts():
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.PORT_CONFLICT,
                    marker_present=True,
                    ownership_confirmed=exact_live_owner,
                    detail="После exact cleanup остался port conflict.",
                )
            if not self._remove_recorded_marker(payload):
                return LocalHttpSupervisorStopResult(
                    outcome=LocalHttpSupervisorStopOutcome.MARKER_CHANGED,
                    marker_present=True,
                    ownership_confirmed=exact_live_owner,
                    detail="Marker изменился или не может быть безопасно удалён.",
                )
            if not self._stopped_postcondition():
                outcome = (
                    LocalHttpSupervisorStopOutcome.PORT_CONFLICT
                    if self.port_conflicts()
                    else LocalHttpSupervisorStopOutcome.POSTCONDITION_FAILED
                )
                return LocalHttpSupervisorStopResult(
                    outcome=outcome,
                    marker_present=True,
                    marker_removed=True,
                    ownership_confirmed=exact_live_owner,
                    detail="STOPPED/no-conflict postcondition не подтверждён.",
                )
            return LocalHttpSupervisorStopResult(
                outcome=(
                    LocalHttpSupervisorStopOutcome.EXACT_LIVE_OWNER_STOPPED
                    if exact_live_owner
                    else LocalHttpSupervisorStopOutcome.STALE_RECORDED_OWNER_RECOVERED
                ),
                marker_present=True,
                marker_removed=True,
                ownership_confirmed=True,
                postcondition_confirmed=True,
                detail=(
                    "Exact live owner остановлен."
                    if exact_live_owner
                    else "Stale recorded owner безопасно очищен."
                ),
            )
        finally:
            if lock is not None:
                lock.release()

    def _stop_children(self) -> None:
        stopped_pids: set[int] = set()
        for launcher in tuple(self._children.values()):
            if launcher.pid in stopped_pids or launcher.poll() is not None:
                continue
            if ProcessController.terminate(
                launcher.identity, timeout_seconds=STOP_TIMEOUT_SECONDS
            ):
                stopped_pids.add(launcher.pid)
        # Runtime identity сохраняется для marker и fallback, если процесс
        # завершился между двумя наблюдениями.
        for identity in tuple(self._runtime_processes.values()):
            if identity.pid in stopped_pids:
                continue
            ProcessController.terminate(
                identity, timeout_seconds=STOP_TIMEOUT_SECONDS
            )
            stopped_pids.add(identity.pid)
        self._children.clear()
        self._runtime_processes.clear()

    def serve(self) -> bool:
        """Запустить services и удерживать lock до штатной остановки.

        Возвращает ``False``, если другой exact-scoped supervisor уже владеет
        lock; это штатный результат повторного запуска из Desktop shortcut.
        """

        _ensure_state_directory(self.repository_root)
        self._lock_handle = None
        coordination_lock = FileLock(self.lock_path)
        if not coordination_lock.acquire(timeout_seconds=0):
            return False
        self._lock_handle = coordination_lock
        stop_requested = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        previous_handlers: dict[int, Any] = {}
        for signum in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM)):
            try:
                previous_handlers[signum] = signal.signal(signum, request_stop)
            except (ValueError, OSError):
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
                self._runtime_processes[service.name] = launcher.identity
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
                except (ValueError, OSError):
                    pass
            if self._lock_handle is not None:
                self._lock_handle.release()
            self._lock_handle = None

    def status(self) -> dict[str, object]:
        """Вернуть bounded состояние marker/process/readiness без token данных."""

        payload, read_outcome = self._read_marker_payload()
        if read_outcome is not None:
            code = (
                "LOCAL_MCP_SUPERVISOR_MARKER_INVALID"
                if read_outcome is LocalHttpSupervisorStopOutcome.INVALID_MARKER
                else "LOCAL_MCP_SUPERVISOR_UNKNOWN"
            )
            return {
                "ok": False,
                "code": code,
                "repository_root": str(self.repository_root),
            }
        if payload is None:
            return {
                "ok": False,
                "code": "LOCAL_MCP_SUPERVISOR_STOPPED",
                "repository_root": str(self.repository_root),
            }

        validated = self._validated_marker_identities(payload)
        if isinstance(validated, LocalHttpSupervisorStopOutcome):
            code = (
                "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH"
                if validated is LocalHttpSupervisorStopOutcome.OWNERSHIP_MISMATCH
                else "LOCAL_MCP_SUPERVISOR_MARKER_INVALID"
            )
            return {
                "ok": False,
                "code": code,
                "repository_root": str(self.repository_root),
            }
        supervisor_identity, _identities = validated
        if not self._safe_identity_matches(supervisor_identity):
            if self._safe_inspect_state(supervisor_identity) == "unknown":
                code = "LOCAL_MCP_SUPERVISOR_UNKNOWN"
            else:
                code = "LOCAL_MCP_SUPERVISOR_STALE"
            return {
                "ok": False,
                "code": code,
                "repository_root": str(self.repository_root),
            }
        marker_services = payload.get("services")
        assert isinstance(marker_services, list)
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
            child_identity = _identity_from_marker(child)
            if child_identity is None:
                return {
                    "ok": False,
                    "code": "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
                    "repository_root": str(self.repository_root),
                }
            alive = self._safe_identity_matches(child_identity)
            ready_payload = self._ready_payload(expected_service) if alive else None
            services.append(
                {
                    "server_name": expected_service.name,
                    "port": expected_service.port,
                    "alive": alive,
                    "ready": ready_payload is not None,
                    **(ready_payload or {}),
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
            "supervisor_pid": supervisor_identity.pid,
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
                identity = _identity_from_marker(expected)
                if identity is None:
                    continue
                ProcessController.terminate(
                    identity, timeout_seconds=STOP_TIMEOUT_SECONDS
                )

    def stop(self) -> bool:
        """Остановить только supervisor с exact recorded identity."""
        return self.stop_result().ok


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="AzurPilot local MCP HTTP supervisor")
    parser.add_argument(
        "command", choices=("serve", "status", "stop"), nargs="?", default="serve"
    )
    parser.add_argument("--root", type=Path, default=_default_repository_root())
    parser.add_argument(
        "--service",
        choices=tuple(service.name for service in LOCAL_HTTP_SERVICES),
        action="append",
    )
    args = parser.parse_args()
    selected_names = tuple(dict.fromkeys(args.service or ()))
    selected_services = tuple(
        service for service in LOCAL_HTTP_SERVICES if service.name in selected_names
    )
    supervisor = LocalHttpSupervisor(
        args.root,
        services=selected_services or LOCAL_HTTP_SERVICES,
        state_namespace=(selected_names[0] if len(selected_names) == 1 else None),
    )
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
    "LOCAL_HTTP_SOURCE_SET_DIGEST_ENV_VARS",
    "LocalHttpService",
    "LocalHttpSupervisor",
    "LocalHttpSupervisorError",
    "LocalHttpSupervisorStopOutcome",
    "LocalHttpSupervisorStopResult",
    "main",
)


if __name__ == "__main__":  # pragma: no cover
    main()
