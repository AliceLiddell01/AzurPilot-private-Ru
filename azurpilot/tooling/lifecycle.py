"""Сервис lifecycle для WebUI проекта с точным владением процессом и портом."""

from __future__ import annotations

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
    CapabilityStatus,
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
from .infrastructure import InfrastructureService
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
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "no_http_response"


def _process_is_descendant(pid: int, ancestor: ProcessIdentity) -> bool:
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    for _ in range(16):
        if process.pid == ancestor.pid:
            return ancestor.matches(process)
        try:
            process = process.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if process is None:
            return False
    return False


class LifecycleService:
    """Безопасные Start/Stop/inspect без завершения процесса только по имени."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        *,
        require_infrastructure: bool = True,
        infrastructure: InfrastructureService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()
        self.require_infrastructure = require_infrastructure
        self.infrastructure = infrastructure or InfrastructureService(self.runner)

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
        if observation.inspection_failed or observation.listener_present is None:
            return _PortState(observation, "unknown", False), identity
        if not observation.listener_present and not observation.pids:
            return _PortState(observation, "free", False), identity
        if observation.pid_unknown:
            if not observation.pids and identity is not None and identity.matches():
                return _PortState(observation, "azurpilot", True), identity
            return _PortState(observation, "foreign", False), identity
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
                "Не удалось проверить владение портом WebUI.",
            )
        if port_state.owner == "foreign":
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=False,
                code=ResultCode.TOOLING_PORT_CONFLICT,
                state=OperationState.CONFLICT,
                message="Порт WebUI занят процессом без подтверждённой идентичности AzurPilot.",
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
                message="WebUI отвечает, владение подтверждено."
                if ready
                else "Процесс WebUI запущен, готовность ещё не подтверждена.",
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
                "Старое состояние lifecycle не подтверждается текущим PID; очистка не выполнена.",
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
                "Checkout не подготовлен для Start; выполните Build.",
            )

    def _ensure_infrastructure(
        self, root: Path, settings: DeploySettings, timeout_seconds: float
    ) -> tuple[ToolingWarning, ...]:
        if not self.require_infrastructure:
            return ()
        outcome = self.infrastructure.ensure_started(
            root, settings, timeout_seconds=max(1.0, timeout_seconds)
        )
        warnings: list[ToolingWarning] = []
        if outcome.caddy is CapabilityStatus.NOT_CONFIGURED:
            warnings.append(
                ToolingWarning(
                    code=WarningCode.TOOLING_CADDY_NOT_CONFIGURED,
                    message="Caddy не настроен; локальный WebUI продолжен без удалённого входа.",
                )
            )
        return tuple(warnings)

    @staticmethod
    def _wait_cleanup(
        running: RunningProcess,
        settings: DeploySettings,
        timeout_seconds: float = 2.0,
    ) -> tuple[bool, PortObservation]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        observation = observe_tcp_port(settings.webui_port)
        while True:
            confirmed = (
                not observation.inspection_failed
                and observation.listener_present is False
                and not observation.pids
                and running.poll() is not None
            )
            if confirmed or time.monotonic() >= deadline:
                return confirmed, observation
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            observation = observe_tcp_port(settings.webui_port)

    @staticmethod
    def _wait_stop_cleanup(
        identity: ProcessIdentity,
        settings: DeploySettings,
        timeout_seconds: float,
    ) -> tuple[bool, PortObservation]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        observation = observe_tcp_port(settings.webui_port)
        while True:
            confirmed = (
                not observation.inspection_failed
                and observation.listener_present is False
                and not observation.pids
                and not identity.matches()
            )
            if confirmed or time.monotonic() >= deadline:
                return confirmed, observation
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            observation = observe_tcp_port(settings.webui_port)

    @staticmethod
    def _lifecycle_details(
        settings: DeploySettings,
        *,
        status: OperationState,
        readiness: str,
        pid: int | None,
        cleanup_confirmed: bool,
        exit_status: int | None = None,
    ) -> LifecycleDetails:
        return LifecycleDetails(
            status=status,
            pid=pid,
            port=settings.webui_port,
            readiness=readiness,
            cleanup_confirmed=cleanup_confirmed,
            exit_status=exit_status,
        )

    def _clear_confirmed_cleanup(
        self,
        coordinator: RepositoryCoordinator,
        settings: DeploySettings,
        operation_id: str,
        *,
        pid: int | None,
        readiness: str,
        exit_status: int | None = None,
    ) -> None:
        try:
            coordinator.clear_lifecycle()
            coordinator.clear_stop_request()
        except Exception as error:
            raise ToolingError(
                ResultCode.TOOLING_CLEANUP_UNKNOWN,
                "Процесс остановлен, но состояние lifecycle нельзя безопасно очистить; требуется восстановление.",
                state=OperationState.IN_FLIGHT,
                operation_id=operation_id,
                details=self._lifecycle_details(
                    settings,
                    status=OperationState.IN_FLIGHT,
                    readiness=readiness,
                    pid=pid,
                    cleanup_confirmed=True,
                    exit_status=exit_status,
                ),
            ) from error

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
            if observation.inspection_failed or observation.listener_present is None:
                return False, "port_identity_unavailable"
            if observation.pid_unknown:
                if observation.pids or not running.identity.matches():
                    return False, "foreign_listener"
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
        if not 0 < timeout_seconds <= 24 * 60 * 60:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок должен быть положительным и ограниченным числом.",
            )
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
        cleanup_attempted = False
        try:
            self._check_start_prerequisites(root, settings)
            start_deadline = time.monotonic() + timeout_seconds
            infrastructure_warnings = self._ensure_infrastructure(
                root,
                settings,
                min(timeout_seconds, max(1.0, start_deadline - time.monotonic())),
            )
            remaining = start_deadline - time.monotonic()
            if remaining <= 0:
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    "Подготовка инфраструктуры исчерпала срок Start.",
                    operation_id=operation_id,
                )
            record = coordinator.read_lifecycle()
            port_state, identity = self._port_state(settings, coordinator, record)
            if port_state.owner == "unknown":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Владение портом WebUI не подтверждено.",
                    operation_id=operation_id,
                )
            if port_state.owner == "foreign":
                raise ToolingError(
                    ResultCode.TOOLING_PORT_CONFLICT,
                    "Порт WebUI занят чужим процессом.",
                    operation_id=operation_id,
                )
            if identity is not None and identity.matches():
                ready, readiness = _probe_ready(settings)
                if ready:
                    return ToolingResult[LifecycleDetails, LifecycleEvidence](
                        ok=True,
                        code=ResultCode.OK,
                        state=OperationState.READY,
                        message="WebUI уже запущен; новый процесс не создавался.",
                        operation_id=operation_id,
                        details=LifecycleDetails(
                            status=OperationState.READY,
                            pid=identity.pid,
                            port=settings.webui_port,
                            readiness=readiness,
                        ),
                        warnings=infrastructure_warnings,
                        evidence=self._evidence(resolved, identity, "azurpilot"),
                    )
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Процесс WebUI уже запущен без подтверждённой готовности.",
                    operation_id=operation_id,
                )
            if record is not None and port_state.owner == "free":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Старое состояние lifecycle не подтверждается текущим PID; запуск запрещён.",
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
            try:
                coordinator.write_lifecycle(
                    coordinator.record_from_identity(
                        running.identity, root, settings.webui_port
                    )
                )
            except Exception as error:
                cleanup_attempted = True
                was_running = running.poll() is None
                terminate_succeeded = ProcessController.terminate(
                    running.identity,
                    timeout_seconds=min(15.0, max(0.1, timeout_seconds)),
                )
                cleanup_confirmed, _ = self._wait_cleanup(
                    running, settings, timeout_seconds=2.0
                )
                if not cleanup_confirmed or (was_running and not terminate_succeeded):
                    raise ToolingError(
                        ResultCode.TOOLING_CLEANUP_UNKNOWN,
                        "Не удалось сохранить состояние lifecycle; очистка WebUI не подтверждена, состояние сохранено для восстановления.",
                        state=OperationState.IN_FLIGHT,
                        operation_id=operation_id,
                        details=self._lifecycle_details(
                            settings,
                            status=OperationState.IN_FLIGHT,
                            readiness="lifecycle_write_failed",
                            pid=running.pid,
                            cleanup_confirmed=False,
                            exit_status=running.poll(),
                        ),
                    ) from error
                cleanup_attempted = True
                self._clear_confirmed_cleanup(
                    coordinator,
                    settings,
                    operation_id,
                    pid=running.pid,
                    readiness="lifecycle_write_failed",
                    exit_status=running.poll(),
                )
                if isinstance(error, ToolingError):
                    raise ToolingError(
                        error.code,
                        error.message,
                        state=error.state,
                        details=error.details,
                        evidence=error.evidence,
                        operation_id=operation_id,
                    ) from error
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Не удалось сохранить состояние lifecycle; WebUI остановлен, повторите команду.",
                    operation_id=operation_id,
                ) from error
            ready, readiness = self._wait_readiness(
                running,
                settings,
                start_deadline - time.monotonic(),
            )
            if not ready:
                cleanup_attempted = True
                was_running = running.poll() is None
                terminate_succeeded = ProcessController.terminate(
                    running.identity, timeout_seconds=min(15.0, timeout_seconds)
                )
                cleanup_confirmed, _ = self._wait_cleanup(
                    running, settings, timeout_seconds=2.0
                )
                if not cleanup_confirmed or (was_running and not terminate_succeeded):
                    raise ToolingError(
                        ResultCode.TOOLING_CLEANUP_UNKNOWN,
                        "Готовность WebUI не подтверждена, а остановку и освобождение порта нельзя доказать; состояние lifecycle сохранено.",
                        state=OperationState.IN_FLIGHT,
                        operation_id=operation_id,
                        details=self._lifecycle_details(
                            settings,
                            status=OperationState.IN_FLIGHT,
                            readiness=readiness,
                            pid=running.pid,
                            cleanup_confirmed=False,
                            exit_status=running.poll(),
                        ),
                    )
                cleanup_attempted = True
                self._clear_confirmed_cleanup(
                    coordinator,
                    settings,
                    operation_id,
                    pid=running.pid,
                    readiness=readiness,
                    exit_status=running.poll(),
                )
                code = (
                    ResultCode.TOOLING_PORT_CONFLICT
                    if readiness == "foreign_listener"
                    else ResultCode.TOOLING_PROCESS_EXITED
                    if readiness == "process_exited"
                    else ResultCode.TOOLING_TIMEOUT
                )
                termination_text = (
                    "Процесс AzurPilot завершился самостоятельно."
                    if not terminate_succeeded
                    else "Процесс AzurPilot остановлен после неподтверждённой готовности."
                )
                raise ToolingError(
                    code,
                    f"WebUI не подтвердил готовность в установленный срок; {termination_text}",
                    operation_id=operation_id,
                    details=self._lifecycle_details(
                        settings,
                        status=OperationState.STOPPED,
                        readiness=readiness,
                        pid=running.pid,
                        cleanup_confirmed=True,
                        exit_status=running.poll(),
                    ),
                )
            warnings: list[ToolingWarning] = list(infrastructure_warnings)
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
                    cleanup_attempted = True
                    was_running = running.poll() is None
                    terminated = ProcessController.terminate(
                        running.identity,
                        timeout_seconds=min(15.0, max(0.1, timeout_seconds)),
                    )
                    cleanup_confirmed, _ = self._wait_cleanup(
                        running, settings, timeout_seconds=2.0
                    )
                    if cleanup_confirmed and (terminated or not was_running):
                        cleanup_attempted = True
                        self._clear_confirmed_cleanup(
                            coordinator,
                            settings,
                            operation_id,
                            pid=running.pid,
                            readiness="cancelled",
                            exit_status=running.poll(),
                        )
                        termination_text = (
                            "дерево процессов AzurPilot остановлено"
                            if terminated
                            else "процесс уже завершился"
                        )
                        raise ToolingError(
                            ResultCode.TOOLING_CANCELLED,
                            f"WebUI в переднем плане прерван по запросу пользователя; {termination_text}, очистка подтверждена.",
                            state=OperationState.STOPPED,
                            operation_id=operation_id,
                            details=self._lifecycle_details(
                                settings,
                                status=OperationState.STOPPED,
                                readiness="cancelled",
                                pid=running.pid,
                                cleanup_confirmed=True,
                                exit_status=running.poll(),
                            ),
                        )
                    raise ToolingError(
                        ResultCode.TOOLING_CLEANUP_UNKNOWN,
                        "WebUI в переднем плане прерван, но очистка не подтверждена; состояние lifecycle сохранено.",
                        state=OperationState.IN_FLIGHT,
                        operation_id=operation_id,
                        details=self._lifecycle_details(
                            settings,
                            status=OperationState.IN_FLIGHT,
                            readiness="cancelled",
                            pid=running.pid,
                            cleanup_confirmed=False,
                            exit_status=running.poll(),
                        ),
                    )
                exit_status = running.poll()
                cleanup_confirmed, _ = self._wait_cleanup(
                    running, settings, timeout_seconds=0.5
                )
                if exit_status is not None:
                    cleanup_attempted = True
                    if cleanup_confirmed:
                        cleanup_attempted = True
                        self._clear_confirmed_cleanup(
                            coordinator,
                            settings,
                            operation_id,
                            pid=running.pid,
                            readiness="process_exited",
                            exit_status=exit_status,
                        )
                        raise ToolingError(
                            ResultCode.TOOLING_PROCESS_EXITED,
                            "WebUI в переднем плане завершился после готовности; команда не считается успешной.",
                            state=OperationState.STOPPED,
                            operation_id=operation_id,
                            details=self._lifecycle_details(
                                settings,
                                status=OperationState.STOPPED,
                                readiness="process_exited",
                                pid=running.pid,
                                cleanup_confirmed=True,
                                exit_status=exit_status,
                            ),
                        )
                    raise ToolingError(
                        ResultCode.TOOLING_CLEANUP_UNKNOWN,
                        "WebUI в переднем плане завершился, но listener или дерево процессов не освобождены; состояние lifecycle сохранено.",
                        state=OperationState.IN_FLIGHT,
                        operation_id=operation_id,
                        details=self._lifecycle_details(
                            settings,
                            status=OperationState.IN_FLIGHT,
                            readiness="process_exited",
                            pid=running.pid,
                            cleanup_confirmed=False,
                            exit_status=exit_status,
                        ),
                    )
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="WebUI запущен, владение процессом и готовность подтверждены.",
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
        except Exception as error:
            if running is None or cleanup_attempted:
                raise
            was_running = running.poll() is None
            terminate_succeeded = ProcessController.terminate(
                running.identity,
                timeout_seconds=min(15.0, max(0.1, timeout_seconds)),
            )
            cleanup_confirmed, _ = self._wait_cleanup(
                running, settings, timeout_seconds=2.0
            )
            cleanup_attempted = True
            if not cleanup_confirmed or (was_running and not terminate_succeeded):
                raise ToolingError(
                    ResultCode.TOOLING_CLEANUP_UNKNOWN,
                    "Ошибка Start не позволила подтвердить остановку WebUI; состояние lifecycle сохранено.",
                    state=OperationState.IN_FLIGHT,
                    operation_id=operation_id,
                    details=self._lifecycle_details(
                        settings,
                        status=OperationState.IN_FLIGHT,
                        readiness="start_failure_cleanup_unknown",
                        pid=running.pid,
                        cleanup_confirmed=False,
                        exit_status=running.poll(),
                    ),
                ) from error
            self._clear_confirmed_cleanup(
                coordinator,
                settings,
                operation_id,
                pid=running.pid,
                readiness="start_failure",
                exit_status=running.poll(),
            )
            if isinstance(error, ToolingError):
                raise ToolingError(
                    error.code,
                    error.message,
                    state=error.state,
                    details=error.details,
                    evidence=error.evidence,
                    operation_id=operation_id,
                ) from error
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Start завершился ошибкой; WebUI остановлен, повторите команду.",
                operation_id=operation_id,
            ) from error
        finally:
            lock.release()

    def stop(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> ToolingResult[LifecycleDetails, LifecycleEvidence]:
        if not 0 < timeout_seconds <= 24 * 60 * 60:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок должен быть положительным и ограниченным числом.",
            )
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
                "Владение портом WebUI не подтверждено.",
                    operation_id=operation_id,
                )
            if port_state.owner == "foreign":
                raise ToolingError(
                    ResultCode.TOOLING_PORT_CONFLICT,
                "Чужой процесс на порту WebUI не будет остановлен.",
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
                    "Состояние WebUI отсутствует, но владение портом неясно.",
                    operation_id=operation_id,
                )
            if not identity.matches():
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Состояние PID переиспользовано или сведения о владении устарели.",
                    operation_id=operation_id,
                )
            coordinator.request_stop()
            deadline = time.monotonic() + timeout_seconds
            terminated = ProcessController.terminate(
                identity, timeout_seconds=min(15.0, max(0.1, timeout_seconds))
            )
            cleanup_confirmed, _observation = self._wait_stop_cleanup(
                identity,
                settings,
                max(0.0, deadline - time.monotonic()),
            )
            identity_still_matches = identity.matches()
            if not cleanup_confirmed or identity_still_matches:
                code = (
                    ResultCode.TOOLING_TIMEOUT
                    if not terminated or identity_still_matches
                    else ResultCode.TOOLING_CLEANUP_UNKNOWN
                )
                raise ToolingError(
                    code,
                    "После Stop не подтверждены завершение принадлежащего дерева процессов и свободный порт WebUI; состояние lifecycle сохранено.",
                    operation_id=operation_id,
                    state=OperationState.IN_FLIGHT,
                    details=self._lifecycle_details(
                        settings,
                        status=OperationState.IN_FLIGHT,
                        readiness="cleanup_unknown",
                        pid=identity.pid,
                        cleanup_confirmed=False,
                    ),
                )
            self._clear_confirmed_cleanup(
                coordinator,
                settings,
                operation_id,
                pid=identity.pid,
                readiness="stopped",
            )
            return ToolingResult[LifecycleDetails, LifecycleEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.STOPPED,
                message=(
                    "WebUI остановлен и освобождение порта подтверждено."
                    if terminated
                    else "WebUI уже завершился; освобождение порта подтверждено."
                ),
                operation_id=operation_id,
                details=LifecycleDetails(
                    status=OperationState.STOPPED,
                    pid=identity.pid,
                    port=settings.webui_port,
                    readiness="stopped",
                    cleanup_confirmed=True,
                ),
                evidence=self._evidence(resolved, identity, "free"),
            )
        finally:
            lock.release()


__all__ = ["LifecycleService"]
