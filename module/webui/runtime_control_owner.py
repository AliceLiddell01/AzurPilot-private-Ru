"""Выполнение общего WebUI runtime control catalog внутри owner-процесса."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from module.application.runtime_control import (
    RuntimeControlOperation,
    RuntimeControlResult,
    RuntimeOwnerIdentity,
    WebUIControlServer,
)
from module.application.runtime_handover import (
    HandoverHooks,
    HandoverPolicy,
    ProfileHandoverCoordinator,
)
from module.application.runtime_state import (
    RuntimePhase,
    RuntimeStateSnapshot,
    RuntimeStateStore,
)


class WebUIRuntimeControlOwner:
    """Единственный executor, имеющий доступ к WebUI-owned ProcessManager."""

    def __init__(
        self,
        repository_root: Path | str | None = None,
        *,
        manager_factory: Callable[[str], object] | None = None,
        application: object | None = None,
        notifier: Callable[[str, str, str], bool] | None = None,
        profile_provider: Callable[[], Sequence[str]] | None = None,
        worker_record_provider: Callable[[str], dict[str, object] | None] | None = None,
        function_factory: Callable[[str], object] | None = None,
        deploy_config_provider: Callable[[], object] | None = None,
        development_profile_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[2]).resolve()
        self._manager_factory = manager_factory
        self._application = application
        self._notifier = notifier
        self._profile_provider = profile_provider
        self._worker_record_provider = worker_record_provider
        self._function_factory = function_factory
        self._deploy_config_provider = deploy_config_provider
        self._development_profile_provider = development_profile_provider
        self.state = RuntimeStateStore(self.repository_root)

    def start_server(self) -> WebUIControlServer:
        server = WebUIControlServer(
            self.repository_root,
            owner_reader=self.owner_identity,
            owner_matches=self.owner_matches,
            executor=self.execute,
        )
        server.start()
        return server

    @staticmethod
    def owner_identity() -> RuntimeOwnerIdentity | None:
        from module.webui.worker_registry import get_owner_record

        record = get_owner_record()
        return None if record is None else RuntimeOwnerIdentity.from_value(record)

    @staticmethod
    def owner_matches(owner: RuntimeOwnerIdentity) -> bool:
        from module.webui.worker_registry import process_matches

        try:
            return process_matches(owner.as_dict()) is True
        except RuntimeError:
            return False

    def execute(
        self,
        operation: RuntimeControlOperation,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
    ) -> RuntimeControlResult:
        owner = self.owner_identity()
        try:
            if owner is None or self.owner_matches(owner) is not True:
                return self._failure(
                    operation,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_OWNER_STALE",
                    "Идентичность WebUI owner не подтверждена во время выполнения operation",
                    owner=owner,
                )
            if profile not in self._profiles():
                return self._failure(
                    operation,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_PROFILE_INVALID",
                    "Профиль не входит в canonical registry рабочей копии",
                    owner=owner,
                )
            if operation is RuntimeControlOperation.START_PROFILE:
                return self._start(
                    profile,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    session_id=session_id,
                    owner=owner,
                )
            if operation is RuntimeControlOperation.STOP_PROFILE:
                return self._stop(
                    profile,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    session_id=session_id,
                    owner=owner,
                )
            return self._failure(
                operation,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_OPERATION_INVALID",
                "Операция отсутствует в фиксированном типизированном каталоге",
                owner=owner,
            )
        except Exception:  # noqa: BLE001 - owner boundary fails closed.
            return self._failure(
                operation,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_EXECUTION_FAILED",
                "Операция владельца runtime завершилась без подтверждённого результата",
                owner=owner,
            )

    def _start(
        self,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        owner: RuntimeOwnerIdentity,
    ) -> RuntimeControlResult:
        manager = self._manager(profile)
        development_profile = self._development_profile()
        if self._read_alive(manager):
            snapshot = self.state.read(profile)
            if snapshot is None:
                return self._failure(
                    RuntimeControlOperation.START_PROFILE,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_STATE_UNKNOWN",
                    "Работающий профиль не имеет подтверждённого runtime state; start отклонён",
                    owner=owner,
                )
            if (
                profile == development_profile
                and session_id is not None
                and snapshot.session_id != session_id
            ):
                return self._failure(
                    RuntimeControlOperation.START_PROFILE,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_OWNERSHIP_MISMATCH",
                    "Уже работающий development worker принадлежит другой DevSession",
                    owner=owner,
                    details={"active_session_id": snapshot.session_id},
                )
            if profile == development_profile:
                record = self._worker_record(profile)
                if record is None:
                    return self._failure(
                        RuntimeControlOperation.START_PROFILE,
                        profile,
                        request_id,
                        idempotency_key,
                        "RUNTIME_START_UNCONFIRMED",
                        "Профиль считается запущенным только при подтверждённой записи worker",
                        owner=owner,
                    )
                snapshot = self.state.mark_ap_ready(
                    profile,
                    worker_pid=int(record["pid"]),
                    worker_created_at=float(record["created_at"]),
                    operation_id=request_id,
                    session_id=session_id or snapshot.session_id,
                )
            return self._success(
                RuntimeControlOperation.START_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_ALREADY_RUNNING",
                "Профиль уже запущен в общем WebUI",
                snapshot,
                owner,
                {"idempotent": True},
            )

        running = self._live_profiles()
        handover_details: dict[str, object] | None = None
        if running:
            if (
                profile != development_profile
                or len(running) != 1
                or running[0] == development_profile
            ):
                return self._failure(
                    RuntimeControlOperation.START_PROFILE,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_RESOURCE_BUSY",
                    "Другой runtime-профиль уже использует общий игровой ресурс",
                    owner=owner,
                    details={"running_profiles": running},
                )
            source = running[0]
            grace_seconds = self._grace_seconds()
            handover = ProfileHandoverCoordinator(
                HandoverPolicy(
                    grace_period_seconds=grace_seconds,
                    quiesce_timeout_seconds=grace_seconds,
                )
            ).run(
                source,
                operation_id=request_id,
                session_id=session_id,
                hooks=_OwnerHandoverHooks(self, source),
            )
            if not handover.ok:
                return self._failure(
                    RuntimeControlOperation.START_PROFILE,
                    profile,
                    request_id,
                    idempotency_key,
                    handover.code,
                    handover.message,
                    owner=owner,
                    details={"handover": handover.as_dict()},
                )
            if self._live_profiles():
                return self._failure(
                    RuntimeControlOperation.START_PROFILE,
                    profile,
                    request_id,
                    idempotency_key,
                    "RUNTIME_HANDOVER_TIMEOUT",
                    "После handover worker пользовательского профиля всё ещё работает; development profile не запускается",
                    owner=owner,
                    details={"handover": handover.as_dict()},
                )
            handover_details = handover.as_dict()

        if profile == development_profile:
            self.state.mark_ap_acquiring(profile, operation_id=request_id, session_id=session_id)
        self._start_manager(manager, profile, request_id, session_id)
        if self._read_alive(manager) is not True:
            self.state.mark_failed(
                profile,
                operation_id=request_id,
                session_id=session_id,
                terminal_state="start_unconfirmed",
            )
            return self._failure(
                RuntimeControlOperation.START_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_START_UNCONFIRMED",
                "WebUI-owned ProcessManager не подтвердил запуск worker",
                owner=owner,
            )
        record = self._worker_record(profile)
        if record is None:
            cleanup_confirmed = self._cooperative_stop_unconfirmed(
                manager,
                request_id=request_id,
                session_id=session_id,
            )
            self.state.mark_failed(
                profile,
                operation_id=request_id,
                session_id=session_id,
                terminal_state="worker_record_unconfirmed",
            )
            return self._failure(
                RuntimeControlOperation.START_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_START_UNCONFIRMED",
                "Worker запущен без authoritative registry readback",
                owner=owner,
                details={"cleanup_confirmed": cleanup_confirmed},
            )
        if profile == development_profile:
            snapshot = self.state.mark_ap_ready(
                profile,
                worker_pid=int(record["pid"]),
                worker_created_at=float(record["created_at"]),
                operation_id=request_id,
                session_id=session_id,
            )
        else:
            snapshot = self.state.mark_worker_started(
                profile,
                worker_pid=int(record["pid"]),
                worker_created_at=float(record["created_at"]),
                operation_id=request_id,
                session_id=session_id,
            )
        return self._success(
            RuntimeControlOperation.START_PROFILE,
            profile,
            request_id,
            idempotency_key,
            "RUNTIME_STARTED",
            "Профиль запущен в общем WebUI",
            snapshot,
            owner,
            {"handover": handover_details} if handover_details is not None else {},
        )

    def _stop(
        self,
        profile: str,
        *,
        request_id: str,
        idempotency_key: str,
        session_id: str | None,
        owner: RuntimeOwnerIdentity,
    ) -> RuntimeControlResult:
        snapshot = self.state.read(profile)
        if session_id is not None and (
            snapshot is None or snapshot.session_id != session_id or snapshot.profile != profile
        ):
            return self._failure(
                RuntimeControlOperation.STOP_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_OWNERSHIP_MISMATCH",
                "DevSession не владеет worker указанного профиля",
                owner=owner,
            )
        manager = self._manager(profile)
        if self._read_alive(manager) is not True:
            snapshot = self.state.mark_worker_stopped(
                profile,
                operation_id=request_id,
                session_id=session_id or (snapshot.session_id if snapshot else None),
            )
            return self._success(
                RuntimeControlOperation.STOP_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_ALREADY_STOPPED",
                "Профиль уже остановлен",
                snapshot,
                owner,
                {"idempotent": True},
            )
        stopped = manager.stop()
        if type(stopped) is not bool or stopped is not True or self._read_alive(manager):
            self.state.mark_failed(
                profile,
                operation_id=request_id,
                session_id=session_id,
                terminal_state="stop_unconfirmed",
            )
            return self._failure(
                RuntimeControlOperation.STOP_PROFILE,
                profile,
                request_id,
                idempotency_key,
                "RUNTIME_STOP_UNCONFIRMED",
                "WebUI-owned ProcessManager не подтвердил остановку worker",
                owner=owner,
            )
        snapshot = self.state.mark_worker_stopped(
            profile,
            operation_id=request_id,
            session_id=session_id,
        )
        return self._success(
            RuntimeControlOperation.STOP_PROFILE,
            profile,
            request_id,
            idempotency_key,
            "RUNTIME_STOPPED",
            "Профиль остановлен",
            snapshot,
            owner,
            {},
        )

    def _profiles(self) -> tuple[str, ...]:
        if self._profile_provider is not None:
            return tuple(self._profile_provider())
        from module.config.profile import discover_profile_configs

        profiles = discover_profile_configs(self.repository_root / "config", strict=True)
        return tuple(profile.name for profile in profiles if profile.mod_name == "alas")

    def _development_profile(self) -> str | None:
        if self._development_profile_provider is not None:
            value = self._development_profile_provider()
            return value if isinstance(value, str) and value else None
        try:
            from module.dev_runtime.target import DevTargetRegistry

            return DevTargetRegistry.load(self.repository_root).profile_name
        except Exception:  # noqa: BLE001 - target discovery fails closed.
            return None

    def _live_profiles(self) -> list[str]:
        live: list[str] = []
        for profile in self._profiles():
            if self._read_alive(self._manager(profile)):
                # Живой manager без authoritative registry тоже блокирует
                # ресурс: неизвестное состояние нельзя трактовать как idle.
                live.append(profile)
        return live

    def _manager(self, profile: str) -> object:
        if self._manager_factory is not None:
            return self._manager_factory(profile)
        from module.webui.process_manager import ProcessManager

        return ProcessManager.get_manager(profile)

    @staticmethod
    def _read_alive(manager: object) -> bool:
        value = getattr(manager, "alive", False)
        if type(value) is not bool:
            raise RuntimeError("ProcessManager.alive должен быть bool")
        return value

    def _start_manager(self, manager: object, profile: str, operation_id: str, session_id: str | None) -> None:
        function = self._function_factory(profile) if self._function_factory is not None else None
        if function is None:
            from module.submodule.utils import get_config_mod

            function = get_config_mod(profile)

        start = getattr(manager, "start", None)
        if not callable(start):
            raise TypeError("WebUI-owned ProcessManager не предоставляет start")
        start(
            func=function,
            operation_id=operation_id,
            session_id=session_id,
        )

    def _cooperative_stop_unconfirmed(
        self,
        manager: object,
        *,
        request_id: str,
        session_id: str | None,
    ) -> bool:
        request = getattr(manager, "request_cooperative_stop", None)
        wait = getattr(manager, "wait_for_exit", None)
        if not callable(request) or not callable(wait):
            return False
        try:
            if request(operation_id=request_id, session_id=session_id) is not True:
                return False
            return wait(self._grace_seconds()) is True
        except Exception:  # noqa: BLE001 - cooperative stop fails closed.
            return False

    def _worker_record(self, profile: str) -> dict[str, object] | None:
        if self._worker_record_provider is not None:
            return self._worker_record_provider(profile)
        from module.webui.worker_registry import get_workers

        record = get_workers(os.getpid()).get(profile)
        return record if isinstance(record, dict) else None

    def _grace_seconds(self) -> float:
        try:
            value = float(self._deploy_config().RuntimeHandoverGraceSeconds)
        except (AttributeError, TypeError, ValueError):
            value = 30.0
        if not 0 <= value <= 300:
            raise RuntimeError("RuntimeHandoverGraceSeconds выходит за допустимые границы")
        return value

    def _deploy_config(self) -> object:
        if self._deploy_config_provider is not None:
            return self._deploy_config_provider()
        from module.webui.setting import State

        return State.deploy_config

    def _application_adapter(self) -> object:
        if self._application is None:
            from module.application.legacy_game_adapters import (
                LegacyGameApplicationAdapter,
            )

            self._application = LegacyGameApplicationAdapter()
        return self._application

    def _notify(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        target = self._development_profile() or "development profile"
        content = (
            f"Текущий профиль занят; после короткого периода ожидания "
            f"управление перейдёт к {target}."
        )
        if self._notifier is not None:
            return self._notifier(
                profile,
                "Требуется подтверждение передачи игрового ресурса",
                content,
            )
        from module.notify.notify import notify_webui

        result = notify_webui(
            profile,
            title="Требуется подтверждение передачи игрового ресурса",
            content=content,
        )
        return type(result) is bool and result

    @staticmethod
    def _success(
        operation: RuntimeControlOperation,
        profile: str,
        request_id: str,
        key: str,
        code: str,
        message: str,
        snapshot: RuntimeStateSnapshot,
        owner: RuntimeOwnerIdentity,
        details: dict[str, object],
    ) -> RuntimeControlResult:
        return RuntimeControlResult(
            ok=True,
            code=code,
            message=message,
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=key,
            state=snapshot.as_dict(),
            details=details,
            owner=owner,
        )

    @staticmethod
    def _failure(
        operation: RuntimeControlOperation,
        profile: str,
        request_id: str,
        key: str,
        code: str,
        message: str,
        *,
        owner: RuntimeOwnerIdentity | None,
        details: dict[str, object] | None = None,
    ) -> RuntimeControlResult:
        return RuntimeControlResult(
            ok=False,
            code=code,
            message=message,
            operation=operation,
            profile=profile,
            request_id=request_id,
            idempotency_key=key,
            details=details or {},
            owner=owner,
        )


class _OwnerHandoverHooks(HandoverHooks):
    def __init__(self, owner: WebUIRuntimeControlOwner, profile: str) -> None:
        self.owner = owner
        self.profile = profile

    def read_state(self, profile: str) -> RuntimeStateSnapshot | None:
        return self.owner.state.read(profile)

    def mark_phase(self, profile: str, phase: RuntimePhase, operation_id: str, session_id: str | None) -> None:
        methods = {
            RuntimePhase.HANDOVER_REQUESTED: self.owner.state.request_handover,
            RuntimePhase.PREEMPTION_NOTICE: self.owner.state.mark_preemption_notice,
            RuntimePhase.GRACE_PERIOD: self.owner.state.mark_grace_period,
            RuntimePhase.QUIESCE_REQUESTED: self.owner.state.request_quiesce,
            RuntimePhase.CURRENT_TASK_DRAINING: self.owner.state.mark_draining,
            RuntimePhase.CURRENT_TASK_STOPPED: self.owner.state.mark_current_task_stopped,
            RuntimePhase.RETURNING_TO_MAIN: self.owner.state.mark_returning_to_main,
            RuntimePhase.MAIN_CONFIRMED: self.owner.state.mark_main_confirmed,
        }
        if phase is RuntimePhase.FAILED:
            self.owner.state.mark_failed(
                profile,
                operation_id=operation_id,
                session_id=session_id,
                terminal_state="handover_failed",
            )
            return
        method = methods.get(phase)
        if method is None:
            raise RuntimeError(f"Неизвестная handover phase: {phase}")
        method(profile, operation_id=operation_id, session_id=session_id)

    def notify_preemption(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        return self.owner._notify(profile, operation_id, session_id)

    def request_cooperative_quiesce(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        manager = self.owner._manager(profile)
        request = getattr(manager, "request_cooperative_stop", None)
        return callable(request) and request(operation_id=operation_id, session_id=session_id) is True

    def wait_worker_stopped(self, profile: str, timeout_seconds: float) -> bool:
        manager = self.owner._manager(profile)
        wait = getattr(manager, "wait_for_exit", None)
        return callable(wait) and wait(timeout_seconds) is True

    def return_to_main(self, profile: str, operation_id: str, session_id: str | None) -> bool:
        method = getattr(self.owner._application_adapter(), "return_to_main", None)
        return callable(method) and method(profile) is True

    def is_main_confirmed(self, profile: str) -> bool:
        method = getattr(self.owner._application_adapter(), "is_in_main", None)
        return callable(method) and method(profile) is True


__all__ = ["WebUIRuntimeControlOwner"]
