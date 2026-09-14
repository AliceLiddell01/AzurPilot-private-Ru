"""Lifecycle service для project WebUI с exact process/port ownership."""

from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

import psutil

from .config import (
    DeploySettings,
    load_deploy_settings,
    local_webui_url,
    project_python,
)
from .contracts import (
    LifecycleDetails,
    LifecycleEvidence,
    OperationState,
    ResultCode,
    ToolingResult,
    ToolingWarning,
    WarningCode,
)
from .coordination import PortObservation, RepositoryCoordinator, observe_tcp_port
from .errors import ToolingError
from .process import (
    ProcessController,
    ProcessIdentity,
    ProcessSpec,
    RunningProcess,
    StructuredProcessRunner,
)
from .repository import RepositoryResolver


@dataclass(frozen=True)
class _PortState:
    observation: PortObservation
    owner: str
    owned_by_record: bool


def _operation_id(prefix: str) -> str:
    return f"{prefix}-{token_hex(8)}"


def _probe_ready(
    settings: DeploySettings, timeout_seconds: float = 1.0
) -> tuple[bool, str]:
    host = (
        "127.0.0.1"
        if settings.webui_host in {"0.0.0.0", "::", "localhost"}
        else settings.webui_host
    )
    request = urllib.request.Request(
        f"http://{host}:{settings.webui_port}/", method="GET"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            response.read(1)
            return (200 <= status < 400), f"http_{status}"
    except urllib.error.HTTPError as exc:
        return (200 <= exc.code < 400) or exc.code in {401, 403}, f"http_{exc.code}"
    except urllib.error.URLError, TimeoutError, OSError:
        return False, "no_http_response"


def _process_is_descendant(pid: int, ancestor: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess, psutil.AccessDenied:
        return False
    for _ in range(16):
        if process.pid == ancestor.pid:
            return ancestor.matches(process)
        try:
            process = process.parent()
        except psutil.NoSuchProcess, psutil.AccessDenied:
            return False
        if process is None:
            return False
    return False


class LifecycleService:
    """Start/stop/inspect без generic process-name termination."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        *,
        require_infrastructure: bool = True,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()
        self.require_infrastructure = require_infrastructure

    def _resolve(
        self, repository_root: str | Path | None
    ) -> tuple[Path, DeploySettings, RepositoryCoordinator]:
        resolved = self.resolver.resolve(repository_root)
        settings = load_deploy_settings(resolved.path)
        return resolved.path, settings, RepositoryCoordinator.for_root(resolved.path)

    @staticmethod
    def _port_state(
        settings: DeploySettings,
        coordinator: RepositoryCoordinator,
        record: object,
    ) -> tuple[_PortState, ProcessIdentity | None]:
        observation = observe_tcp_port(settings.webui_port)
        identity: ProcessIdentity | None = None
        if record is not None:
            identity = coordinator.identity_from_record(record)  # type: ignore[arg-type]
        if observation.inspection_failed:
            return _PortState(observation, "unknown", False), identity
        if not observation.pids:
            return _PortState(observation, "free", False), identity
        if identity is not None and identity.matches():
            ownership = all(
                _process_is_descendant(pid, identity) for pid in observation.pids
            )
            if ownership:
                return _PortState(observation, "azurpilot", True), identity
        return _PortState(observation, "foreign", False), identity

    @staticmethod
    def _evidence(
        resolved: object, process: ProcessIdentity | None, owner: str
    ) -> LifecycleEvidence:
        return LifecycleEvidence(
            repository=resolved.evidence,  # type: ignore[attr-defined]
            process=process.public_evidence()
            if process is not None and process.matches()
            else None,
            port_owner=owner,
        )

    def inspect(
        self, repository_root: str | Path | None = None
    ) -> ToolingResult[LifecycleDetails, LifecycleEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        settings = load_deploy_settings(root)
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        port_state, identity = self._port_state(settings, coordinator, record)
        if port_state.owner == "unknown":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Владение WebUI port не удалось проверить.",
            )
        if port_state.owner == "foreign":
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=False,
                code=ResultCode.TOOLING_PORT_CONFLICT,
                state=OperationState.CONFLICT,
                message="WebUI port занят процессом без подтверждённой AzurPilot identity.",
                details=LifecycleDetails(
                    status=OperationState.CONFLICT,
                    pid=None,
                    port=settings.webui_port,
                    readiness="foreign_listener",
                ),
                evidence=self._evidence(resolved, identity, port_state.owner),
            )
        if identity is not None and identity.matches():
            ready, readiness = _probe_ready(settings)
            state = OperationState.READY if ready else OperationState.RUNNING
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=True,
                code=ResultCode.OK,
                state=state,
                message="WebUI отвечает и ownership подтверждён."
                if ready
                else "WebUI process запущен, readiness ещё не подтверждена.",
                details=LifecycleDetails(
                    status=state,
                    pid=identity.pid,
                    port=settings.webui_port,
                    readiness=readiness,
                ),
                evidence=self._evidence(resolved, identity, port_state.owner),
            )
        if record is not None and port_state.owner == "free":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Старый lifecycle state не подтверждается текущим PID; очистка не выполнена.",
            )
        return ToolingResult[LifecycleDetails, LifecycleEvidence](
            ok=True,
            code=ResultCode.OK,
            state=OperationState.STOPPED,
            message="WebUI не запущен.",
            details=LifecycleDetails(
                status=OperationState.STOPPED,
                port=settings.webui_port,
                readiness="not_running",
            ),
            evidence=self._evidence(resolved, None, port_state.owner),
        )

    def _check_start_prerequisites(self, root: Path, settings: DeploySettings) -> None:
        required = (
            root / "gui.py",
            root / "config" / "deploy.yaml",
            project_python(root, settings),
        )
        if any(not item.is_file() for item in required):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Checkout не подготовлен для start; выполните build.",
            )
        if self.require_infrastructure:
            compose = root / "infrastructure" / "observability" / "compose.yaml"
            env_file = root / ".env"
            if (
                not compose.is_file()
                or not env_file.is_file()
                or not shutil.which("docker")
            ):
                raise ToolingError(
                    ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                    "Docker Compose/PostgreSQL preflight не подтверждён; запуск остановлен.",
                )

    def _wait_readiness(
        self,
        running: RunningProcess,
        settings: DeploySettings,
        timeout_seconds: float,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_seconds
        last = "not_checked"
        while time.monotonic() < deadline:
            if running.poll() is not None:
                return False, "process_exited"
            observation = observe_tcp_port(settings.webui_port)
            if observation.inspection_failed:
                return False, "port_identity_unavailable"
            if observation.pids and not all(
                _process_is_descendant(pid, running.identity)
                for pid in observation.pids
            ):
                return False, "foreign_listener"
            ready, last = _probe_ready(
                settings,
                timeout_seconds=min(1.0, max(0.1, deadline - time.monotonic())),
            )
            if ready:
                return True, last
            time.sleep(0.1)
        return False, last

    def start(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 60.0,
        open_browser: bool = False,
        foreground: bool = False,
    ) -> ToolingResult[LifecycleDetails, LifecycleEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        settings = load_deploy_settings(root)
        coordinator = RepositoryCoordinator.for_root(root)
        operation_id = _operation_id("start")
        lock = coordinator.lock("lifecycle")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Start/Stop уже выполняется.",
                operation_id=operation_id,
            )
        running: RunningProcess | None = None
        try:
            self._check_start_prerequisites(root, settings)
            record = coordinator.read_lifecycle()
            port_state, identity = self._port_state(settings, coordinator, record)
            if port_state.owner == "unknown":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Владение WebUI port не подтверждено.",
                    operation_id=operation_id,
                )
            if port_state.owner == "foreign":
                raise ToolingError(
                    ResultCode.TOOLING_PORT_CONFLICT,
                    "WebUI port занят чужим процессом.",
                    operation_id=operation_id,
                )
            if identity is not None and identity.matches():
                ready, readiness = _probe_ready(settings)
                if ready:
                    return ToolingResult[LifecycleDetails, LifecycleEvidence](
                        ok=True,
                        code=ResultCode.OK,
                        state=OperationState.READY,
                        message="WebUI уже запущен; новый process не создавался.",
                        operation_id=operation_id,
                        details=LifecycleDetails(
                            status=OperationState.READY,
                            pid=identity.pid,
                            port=settings.webui_port,
                            readiness=readiness,
                        ),
                        evidence=self._evidence(resolved, identity, "azurpilot"),
                    )
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "WebUI process уже запущен без readiness.",
                    operation_id=operation_id,
                )
            if record is not None and port_state.owner == "free":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Старый lifecycle state не подтверждается текущим PID; запуск запрещён.",
                    operation_id=operation_id,
                )

            python_path = project_python(root, settings)
            running = self.runner.start(
                ProcessSpec(
                    executable=python_path,
                    argv=("gui.py",),
                    cwd=root,
                    timeout_seconds=max(1.0, timeout_seconds),
                    env={"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                )
            )
            ready, readiness = self._wait_readiness(running, settings, timeout_seconds)
            if not ready:
                ProcessController.terminate(
                    running.identity, timeout_seconds=min(15.0, timeout_seconds)
                )
                code = (
                    ResultCode.TOOLING_PORT_CONFLICT
                    if readiness == "foreign_listener"
                    else ResultCode.TOOLING_TIMEOUT
                )
                raise ToolingError(
                    code,
                    "WebUI не подтвердил readiness в пределах deadline.",
                    operation_id=operation_id,
                )
            coordinator.write_lifecycle(
                coordinator.record_from_identity(
                    running.identity, root, settings.webui_port
                )
            )
            warnings: list[ToolingWarning] = []
            if open_browser and not webbrowser.open(local_webui_url(settings)):
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_BROWSER_NOT_OPENED,
                        message="Браузер не подтвердил открытие WebUI.",
                    )
                )
            if foreground:
                try:
                    while running.poll() is None:
                        time.sleep(0.25)
                except KeyboardInterrupt:
                    ProcessController.terminate(
                        running.identity, timeout_seconds=timeout_seconds
                    )
                    coordinator.clear_lifecycle()
                    coordinator.clear_stop_request()
                    raise
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="WebUI запущен, process identity и readiness подтверждены.",
                operation_id=operation_id,
                details=LifecycleDetails(
                    status=OperationState.READY,
                    pid=running.pid,
                    port=settings.webui_port,
                    readiness=readiness,
                ),
                warnings=tuple(warnings),
                evidence=self._evidence(resolved, running.identity, "azurpilot"),
            )
        finally:
            if (
                running is not None
                and running.poll() is not None
                and coordinator.lifecycle_state_path.exists()
            ):
                try:
                    coordinator.clear_lifecycle()
                except ToolingError:
                    pass
            lock.release()

    def stop(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> ToolingResult[LifecycleDetails, LifecycleEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        settings = load_deploy_settings(root)
        coordinator = RepositoryCoordinator.for_root(root)
        operation_id = _operation_id("stop")
        lock = coordinator.lock("lifecycle")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Start/Stop уже выполняется.",
                operation_id=operation_id,
            )
        try:
            record = coordinator.read_lifecycle()
            port_state, identity = self._port_state(settings, coordinator, record)
            if port_state.owner == "unknown":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Владение WebUI port не подтверждено.",
                    operation_id=operation_id,
                )
            if port_state.owner == "foreign":
                raise ToolingError(
                    ResultCode.TOOLING_PORT_CONFLICT,
                    "Чужой process на WebUI port не будет остановлен.",
                    operation_id=operation_id,
                )
            if identity is None:
                if port_state.owner == "free":
                    return ToolingResult[LifecycleDetails, LifecycleEvidence](
                        ok=True,
                        code=ResultCode.OK,
                        state=OperationState.STOPPED,
                        message="WebUI уже остановлен.",
                        operation_id=operation_id,
                        details=LifecycleDetails(
                            status=OperationState.STOPPED,
                            port=settings.webui_port,
                            readiness="not_running",
                        ),
                        evidence=self._evidence(resolved, None, "free"),
                    )
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "WebUI state отсутствует, но port ownership неясен.",
                    operation_id=operation_id,
                )
            if not identity.matches():
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "PID state переиспользован или ownership устарел.",
                    operation_id=operation_id,
                )
            coordinator.request_stop()
            if not ProcessController.terminate(
                identity, timeout_seconds=timeout_seconds
            ):
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    "Owned WebUI process не завершился в пределах deadline.",
                    operation_id=operation_id,
                )
            observation = observe_tcp_port(settings.webui_port)
            if observation.inspection_failed or observation.pids:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "После Stop не подтверждён свободный WebUI port.",
                    operation_id=operation_id,
                )
            coordinator.clear_lifecycle()
            coordinator.clear_stop_request()
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.STOPPED,
                message="Owned WebUI process и его дочернее дерево остановлены.",
                operation_id=operation_id,
                details=LifecycleDetails(
                    status=OperationState.STOPPED,
                    pid=identity.pid,
                    port=settings.webui_port,
                    readiness="stopped",
                ),
                evidence=self._evidence(resolved, identity, "free"),
            )
        finally:
            lock.release()


__all__ = ["LifecycleService"]
