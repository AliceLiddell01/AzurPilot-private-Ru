"""Менеджер жизненного цикла фиксированной локальной DevSession профиля ap."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.dev_runtime.contracts import (
    DEFAULT_READY_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    DEV_PROFILE,
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionState,
    DevStatusKind,
    ProcessIdentity,
)
from module.dev_runtime.diagnostics import (
    DevDiagnosticsMixin,
    _default_storage_probe,
    _port_is_listening,
)
from module.dev_runtime.process import ProcessBackend, _same_path
from module.dev_runtime.task_sandbox import (
    SCHEDULER_RESET_TIME,
    TASK_POLICY_ACTIVE,
    TaskCatalog,
    TaskPlan,
    TaskPolicyStore,
    TaskSandboxError,
    apply_task_plan,
    read_profile_payload,
    reset_scheduler_state,
    scheduler_state,
    scheduler_time_text,
    write_profile_payload,
)

_LOCK_TIMEOUT = 10.0
_LOCK_RETRY_INTERVAL = 0.05
_state_thread_lock = threading.RLock()


class DevSessionManager(DevDiagnosticsMixin):
    def __init__(
        self,
        environment: DevEnvironment | None = None,
        *,
        process_backend: ProcessBackend | None = None,
        storage_probe: Callable[[DevEnvironment], tuple[bool, str]] | None = None,
        port_probe: Callable[[str, int], bool] | None = None,
        readiness_probe: Callable[[DevEnvironment, ProcessIdentity], tuple[bool, str]]
        | None = None,
        now: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    ):
        self.environment = environment or DevEnvironment.current()
        self.process_backend = process_backend or ProcessBackend()
        self.storage_probe = storage_probe or _default_storage_probe
        self.port_probe = port_probe or _port_is_listening
        self.readiness_probe = readiness_probe or self._default_readiness_probe
        self.now = now or (lambda: datetime.now(UTC))
        self.session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout

    def list_tasks(self) -> DevResult:
        """Вернуть каталог из raw profile без изменения состояния."""

        try:
            catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        return DevResult(
            ok=True,
            code="DEV_TASK_CATALOG_READY",
            message="Каталог schedulable tasks профиля ap прочитан",
            state=DevStatusKind.NO_SESSION.value,
            details=catalog.as_dict(),
        )

    def status(self) -> DevResult:
        """Вернуть Stage 1 status и безопасный read-only снимок task policy."""

        result = super().status()
        try:
            policy_present = self.environment.task_policy_file.exists() or self.environment.task_policy_file.is_symlink()
        except OSError:
            policy_present = True
        if not policy_present:
            return result
        details = dict(result.details)
        task_policy = TaskPolicyStore(self.environment).inspect()
        details["task_policy"] = task_policy
        if task_policy.get("valid") is False:
            code = str(task_policy.get("code", "DEV_TASK_POLICY_INVALID"))
            return DevResult(
                ok=False,
                code=code,
                message="Task policy невозможно безопасно подтвердить",
                state=result.state,
                session_id=result.session_id,
                details=details,
            )
        return DevResult(
            ok=result.ok,
            code=result.code,
            message=result.message,
            state=result.state,
            session_id=result.session_id,
            details=details,
        )

    def plan(
        self,
        *,
        root_tasks: Iterable[str] | str | None,
        excluded_tasks: Iterable[str] | str | None = None,
    ) -> DevResult:
        """Сформировать read-only task plan для будущей task-aware session."""

        _plan, result = self._build_task_plan(root_tasks, excluded_tasks)
        return result

    def task_smoke(
        self,
        *,
        root_tasks: Iterable[str] | str,
        excluded_tasks: Iterable[str] | str | None = None,
        preserve_task_state: bool = False,
    ) -> DevResult:
        """Запустить Stage 2 lifecycle smoke через штатный Dev Runtime path."""

        steps: list[dict[str, object]] = []
        planned = self.plan(root_tasks=root_tasks, excluded_tasks=excluded_tasks)
        steps.append(planned.as_dict())
        if not planned.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_PLAN_FAILED",
                message="Task-aware smoke остановлен на проверке плана",
                state=planned.state,
                details={"steps": steps},
            )
        preflight = self.preflight()
        steps.append(preflight.as_dict())
        if not preflight.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_PREFLIGHT_FAILED",
                message="Task-aware smoke остановлен на предварительной проверке",
                state=preflight.state,
                details={"steps": steps},
            )
        started = self.start(root_tasks=root_tasks, excluded_tasks=excluded_tasks)
        steps.append(started.as_dict())
        if not started.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_START_FAILED",
                message="Task-aware smoke не смог запустить DevSession",
                state=started.state,
                session_id=started.session_id,
                details={"steps": steps},
            )
        observed = self.status()
        steps.append(observed.as_dict())
        if not observed.ok or observed.state != DevStatusKind.RUNNING_OWNED.value:
            stopped = self.stop(preserve_task_state=preserve_task_state)
            steps.append(stopped.as_dict())
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_STATUS_FAILED",
                message="Task-aware smoke не подтвердил рабочее состояние и владение",
                state=observed.state,
                session_id=started.session_id,
                details={"steps": steps},
            )
        stopped = self.stop(preserve_task_state=preserve_task_state)
        steps.append(stopped.as_dict())
        final_status = self.status()
        steps.append(final_status.as_dict())
        ok = stopped.ok and (
            final_status.state == DevStatusKind.STOPPED.value
            or preserve_task_state
        )
        return DevResult(
            ok=ok,
            code="DEV_TASK_SMOKE_PASS" if ok else "DEV_TASK_SMOKE_STOP_FAILED",
            message=(
                "Проверка task sandbox и lifecycle пройдена"
                if ok
                else "Task-aware smoke не подтвердил безопасное завершение"
            ),
            state=final_status.state,
            session_id=started.session_id,
            details={"steps": steps, "preserve_task_state": preserve_task_state},
        )

    def cleanup(self) -> DevResult:
        """Явно завершить scheduler cleanup, не останавливая live process."""

        with self._locked_state():
            try:
                session = self._read_session()
            except ValueError as exc:
                return DevResult(
                    ok=False,
                    code="DEV_STATE_CORRUPT",
                    message=f"Маркер повреждён; cleanup запрещен: {exc}",
                    state=DevStatusKind.CORRUPT.value,
                )
            if session is not None and session.process is not None:
                try:
                    matches = self.process_backend.matches(session.process)
                except RuntimeError as exc:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_OWNERSHIP_UNKNOWN",
                        message=f"Владение невозможно подтвердить; cleanup запрещен: {exc}",
                        state=DevStatusKind.OWNERSHIP_MISMATCH,
                    )
                if matches is True:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_SESSION_ACTIVE",
                        message="Сначала безопасно остановите активную DevSession",
                        state=DevStatusKind.RUNNING_OWNED,
                    )
                if matches is False:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_OWNERSHIP_MISMATCH",
                        message="PID из маркера не принадлежит DevSession; cleanup запрещен",
                        state=DevStatusKind.OWNERSHIP_MISMATCH,
                    )
                session.process = None
            elif session is not None and session.state is not DevSessionState.STOPPED:
                try:
                    candidates = self.process_backend.find_by_session(
                        self.environment, session.session_id
                    )
                except RuntimeError as exc:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_OWNERSHIP_UNKNOWN",
                        message=f"Владение невозможно подтвердить; cleanup запрещен: {exc}",
                        state=DevStatusKind.OWNERSHIP_MISMATCH,
                    )
                if len(candidates) > 1:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_RECOVERY_AMBIGUOUS",
                        message="Найдено несколько процессов DevSession; cleanup запрещен из соображений безопасности",
                        state=DevStatusKind.OWNERSHIP_MISMATCH,
                    )
                if len(candidates) == 1:
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_SESSION_ACTIVE",
                        message="Найден принадлежащий DevSession процесс; сначала безопасно остановите его",
                        state=DevStatusKind.RUNNING_OWNED,
                    )
            cleanup = self._cleanup_task_state_locked(
                expected_session_id=session.session_id if session is not None else None
            )
            if not cleanup.ok:
                if session is not None:
                    session.state = DevSessionState.FAILED
                    session.updated_at = self._timestamp()
                    session.last_code = "DEV_CLEANUP_FAILED"
                    session.last_message = cleanup.message
                    self._write_session(session)
                    return self._session_result(
                        session,
                        ok=False,
                        code="DEV_CLEANUP_FAILED",
                        message=cleanup.message,
                        state=DevStatusKind.FAILED,
                        details=cleanup.details,
                    )
                return cleanup
            if session is None:
                return cleanup
            session.state = DevSessionState.STOPPED
            session.process = None
            session.updated_at = self._timestamp()
            session.last_code = "DEV_TASK_CLEANUP_COMPLETED"
            session.last_message = "Scheduler-state профиля ap очищен"
            self._write_session(session)
            return self._session_result(
                session,
                ok=True,
                code="DEV_TASK_CLEANUP_COMPLETED",
                message=session.last_message,
                state=DevStatusKind.STOPPED,
                details=cleanup.details,
            )

    def _build_task_plan(
        self,
        root_tasks: Iterable[str] | str | None,
        excluded_tasks: Iterable[str] | str | None,
    ) -> tuple[TaskPlan | None, DevResult]:
        try:
            catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            plan = TaskPlan.from_catalog(catalog, root_tasks, excluded_tasks)
        except TaskSandboxError as exc:
            return None, self._task_error(exc)
        return plan, DevResult(
            ok=True,
            code="DEV_TASK_PLAN_READY",
            message="Task-aware план профиля ap сформирован",
            state=DevStatusKind.NO_SESSION.value,
            details={"plan": plan.as_dict()},
        )

    @staticmethod
    def _task_error(exc: TaskSandboxError) -> DevResult:
        return DevResult(
            ok=False,
            code=exc.code,
            message=str(exc),
            state=DevStatusKind.FAILED.value,
            details={"error": exc.as_dict()},
        )

    def _cleanup_leftover_task_state_locked(self) -> DevResult:
        store = TaskPolicyStore(self.environment)
        try:
            policy = store.read()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        if policy is None:
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_NOT_NEEDED",
                message="Безопасный leftover task policy отсутствует",
                state=DevStatusKind.NO_SESSION.value,
                details={"cleanup_confirmed": True},
            )
        try:
            session = self._read_session()
        except ValueError as exc:
            return self._task_error(
                TaskSandboxError("DEV_STATE_CORRUPT", f"Нельзя проверить leftover policy: {exc}")
            )
        expected_session_id = (
            session.session_id
            if session is not None
            and session.state not in {DevSessionState.STOPPED, DevSessionState.FAILED}
            else None
        )
        if expected_session_id is not None and policy.session_id != expected_session_id:
            return self._task_error(
                TaskSandboxError(
                    "DEV_TASK_POLICY_CONTEXT_MISMATCH",
                    "leftover task policy не соответствует DevSession",
                )
            )
        return self._cleanup_task_state_locked(expected_session_id=expected_session_id)

    def _prepare_task_session_locked(
        self, task_plan: TaskPlan, session: DevSession
    ) -> DevResult:
        catalog: TaskCatalog | None = None
        mutation_started = False
        try:
            catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            payload = read_profile_payload(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            next_run = scheduler_time_text(self.now())
            planned_payload = apply_task_plan(
                payload,
                catalog,
                task_plan,
                next_run=next_run,
            )
            write_profile_payload(
                self.environment.profile_file,
                planned_payload,
                repository_root=self.environment.repository_root,
            )
            mutation_started = True
            verified_catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            verified = scheduler_state(
                read_profile_payload(
                    self.environment.profile_file,
                    repository_root=self.environment.repository_root,
                ),
                verified_catalog,
            )
            roots = set(task_plan.root_tasks)
            expected = {
                task: {
                    "enabled": task in roots,
                    "next_run": next_run if task in roots else SCHEDULER_RESET_TIME,
                }
                for task in verified_catalog.commands
            }
            if verified != expected:
                raise TaskSandboxError(
                    "DEV_TASK_PREPARE_UNCONFIRMED",
                    "Применённый task plan не прошёл повторную проверку",
                )
            store = TaskPolicyStore(self.environment)
            store.create(
                task_plan,
                session_id=session.session_id,
                timestamp=self._timestamp(),
            )
            persisted = store.read()
            if (
                persisted is None
                or persisted.state != TASK_POLICY_ACTIVE
                or persisted.session_id != session.session_id
            ):
                raise TaskSandboxError(
                    "DEV_TASK_POLICY_UNCONFIRMED",
                    "Сохранённый task policy не прошёл повторную проверку",
                )
            return DevResult(
                ok=True,
                code="DEV_TASK_SESSION_PREPARED",
                message="Task sandbox подготовлен до запуска gui.py",
                state=DevSessionState.CREATED.value,
                session_id=session.session_id,
                details={
                    "plan": task_plan.as_dict(),
                    "next_run": next_run,
                    "cleanup_confirmed": True,
                },
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            cleanup = (
                self._cleanup_task_state_locked(
                    expected_session_id=session.session_id,
                    catalog=catalog,
                )
                if mutation_started
                else DevResult(
                    ok=True,
                    code="DEV_TASK_CLEANUP_NOT_NEEDED",
                    message="Изменение scheduler-state не начиналось",
                    state=DevStatusKind.NO_SESSION.value,
                )
            )
            details = {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "cleanup": cleanup.as_dict(),
            }
            if not cleanup.ok:
                return DevResult(
                    ok=False,
                    code="DEV_CLEANUP_FAILED",
                    message="Подготовка task sandbox завершилась с неподтверждённой очисткой",
                    state=DevStatusKind.FAILED.value,
                    session_id=session.session_id,
                    details=details,
                )
            return DevResult(
                ok=False,
                code="DEV_TASK_PREPARE_FAILED",
                message="Не удалось подготовить task sandbox до запуска",
                state=DevStatusKind.FAILED.value,
                session_id=session.session_id,
                details=details,
            )

    def _cleanup_task_state_locked(
        self,
        *,
        expected_session_id: str | None,
        catalog: TaskCatalog | None = None,
        preserve_task_state: bool = False,
    ) -> DevResult:
        store = TaskPolicyStore(self.environment)
        try:
            policy = store.read()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        if (
            policy is not None
            and expected_session_id is not None
            and policy.session_id != expected_session_id
        ):
            return self._task_error(
                TaskSandboxError(
                    "DEV_TASK_POLICY_CONTEXT_MISMATCH",
                    "task policy не соответствует текущей DevSession",
                )
            )
        if preserve_task_state:
            try:
                marked = store.mark_preserved(timestamp=self._timestamp())
            except (OSError, RuntimeError, ValueError) as exc:
                return DevResult(
                    ok=False,
                    code="DEV_CLEANUP_FAILED",
                    message="Не удалось зафиксировать явный preserve_task_state",
                    state=DevStatusKind.FAILED.value,
                    details={"error": {"type": type(exc).__name__, "message": str(exc)}},
                )
            from module.logger import logger

            logger.warning(
                "[Dev Runtime] preserve_task_state=True: scheduler-state профиля ap оставлен без cleanup"
            )
            return DevResult(
                ok=True,
                code="DEV_TASK_STATE_PRESERVED",
                message="Scheduler-state оставлен по явному preserve_task_state",
                state=DevStatusKind.STOPPED.value,
                details={
                    "cleanup_confirmed": False,
                    "preserved_task_state": True,
                    "policy_marked": marked is not None,
                },
            )
        if policy is None and catalog is None:
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_NOT_NEEDED",
                message="Task policy отсутствует; cleanup не требуется",
                state=DevStatusKind.STOPPED.value,
                details={"cleanup_confirmed": True, "policy_removed": False},
            )
        try:
            fresh_catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            payload = read_profile_payload(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            cleaned = reset_scheduler_state(payload, fresh_catalog)
            current_state = scheduler_state(payload, fresh_catalog)
            already_clean = all(
                item["enabled"] is False and item["next_run"] == SCHEDULER_RESET_TIME
                for item in current_state.values()
            )
            if not already_clean:
                write_profile_payload(
                    self.environment.profile_file,
                    cleaned,
                    repository_root=self.environment.repository_root,
                )
            verified_catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
            )
            verified_state = scheduler_state(
                read_profile_payload(
                    self.environment.profile_file,
                    repository_root=self.environment.repository_root,
                ),
                verified_catalog,
            )
            if any(
                item["enabled"] is not False
                or item["next_run"] != SCHEDULER_RESET_TIME
                for item in verified_state.values()
            ):
                raise TaskSandboxError(
                    "DEV_CLEANUP_UNCONFIRMED",
                    "Scheduler-state не подтверждён сброшенным",
                )
            if policy is not None:
                store.remove()
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_COMPLETED",
                message="Scheduler-state всех schedulable tasks профиля ap сброшен",
                state=DevStatusKind.STOPPED.value,
                details={
                    "cleanup_confirmed": True,
                    "tasks_reset": len(verified_state),
                    "policy_removed": policy is not None,
                },
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            pending = False
            if policy is not None:
                try:
                    pending = (
                        store.mark_cleanup_pending(timestamp=self._timestamp()) is not None
                    )
                except (OSError, RuntimeError, ValueError):
                    pending = False
            return DevResult(
                ok=False,
                code="DEV_CLEANUP_FAILED",
                message="Scheduler-state cleanup не подтверждён",
                state=DevStatusKind.FAILED.value,
                details={
                    "cleanup_confirmed": False,
                    "policy_marked_cleanup_pending": pending,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
            )

    def _finish_stopped_locked(
        self,
        session: DevSession,
        *,
        code: str,
        message: str,
        preserve_task_state: bool,
    ) -> DevResult:
        session.state = DevSessionState.STOPPED
        session.process = None
        cleanup = self._cleanup_task_state_locked(
            expected_session_id=session.session_id,
            preserve_task_state=preserve_task_state,
        )
        if not cleanup.ok:
            session.state = DevSessionState.FAILED
            session.updated_at = self._timestamp()
            session.last_code = "DEV_CLEANUP_FAILED"
            session.last_message = cleanup.message
            self._write_session(session)
            return self._session_result(
                session,
                ok=False,
                code="DEV_CLEANUP_FAILED",
                message=cleanup.message,
                state=DevStatusKind.FAILED,
                details=cleanup.details,
            )
        session.updated_at = self._timestamp()
        session.last_code = (
            "DEV_SESSION_STOPPED_PRESERVED" if preserve_task_state else code
        )
        session.last_message = message
        self._write_session(session)
        details = dict(cleanup.details)
        details.setdefault("cleanup_confirmed", not preserve_task_state)
        return self._session_result(
            session,
            ok=True,
            code=("DEV_SESSION_STOPPED_PRESERVED" if preserve_task_state else code),
            message=message,
            state=DevStatusKind.STOPPED,
            details=details,
        )

    def start(
        self,
        *,
        root_tasks: Iterable[str] | str | None = None,
        excluded_tasks: Iterable[str] | str | None = None,
    ) -> DevResult:
        task_aware = root_tasks is not None or excluded_tasks is not None
        if not task_aware:
            return self._start_core()
        plan, plan_result = self._build_task_plan(root_tasks, excluded_tasks)
        if plan is None:
            return plan_result
        return self._start_core(plan)

    def _start_core(self, task_plan: TaskPlan | None = None) -> DevResult:
        preflight = self.preflight()
        if not preflight.ok:
            return DevResult(
                ok=False,
                code="DEV_START_PREFLIGHT_FAILED",
                message="Запуск DevSession заблокирован предварительной проверкой",
                state=DevSessionState.FAILED.value,
                details={"preflight": preflight.as_dict()},
            )

        with self._locked_state():
            current = self.status()
            if current.state not in {
                DevStatusKind.NO_SESSION.value,
                DevStatusKind.STOPPED.value,
                DevStatusKind.FAILED.value,
                DevStatusKind.STALE.value,
            }:
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_CONFLICT",
                    message="Другая DevSession уже активна",
                    state=current.state,
                    session_id=current.session_id,
                )
            if current.state in {
                DevStatusKind.FAILED.value,
                DevStatusKind.STALE.value,
            }:
                recovered = self._recover_locked()
                if not recovered.ok:
                    return recovered

            previous_cleanup = self._cleanup_leftover_task_state_locked()
            if not previous_cleanup.ok:
                return previous_cleanup

            timestamp = self._timestamp()
            session = DevSession(
                session_id=self.session_id_factory(),
                state=DevSessionState.CREATED,
                repository_root=str(self.environment.repository_root),
                created_at=timestamp,
                updated_at=timestamp,
                last_code="DEV_SESSION_CREATED",
                last_message="DevSession создана",
            )
            self._write_session(session)
            if task_plan is not None:
                preparation = self._prepare_task_session_locked(task_plan, session)
                if not preparation.ok:
                    session.state = DevSessionState.FAILED
                    session.updated_at = self._timestamp()
                    session.last_code = preparation.code
                    session.last_message = preparation.message
                    self._write_session(session)
                    return self._session_result(
                        session,
                        ok=False,
                        code=preparation.code,
                        message=preparation.message,
                        state=DevStatusKind.FAILED,
                        details=preparation.details,
                    )
            session.state = DevSessionState.STARTING
            session.updated_at = self._timestamp()
            session.last_code = "DEV_SESSION_STARTING"
            session.last_message = "Запускается штатный gui.py для профиля ap"
            self._write_session(session)

            pid: int | None = None
            launched_identity: ProcessIdentity | None = None
            try:
                pid = self.process_backend.launch(self.environment, session.session_id)
                identity = self.process_backend.capture(pid)
                if identity is None:
                    raise RuntimeError("Запущенный процесс завершился до фиксации владения")
                launched_identity = identity
                session.process = identity
                session.updated_at = self._timestamp()
                self._write_session(session)
            except Exception as exc:
                process_cleanup_confirmed = True
                if pid is not None:
                    identity = launched_identity
                    if identity is None:
                        try:
                            identity = self.process_backend.capture(pid)
                        except RuntimeError:
                            identity = None
                    if identity is not None:
                        process_cleanup_confirmed = self.process_backend.force_stop(identity)
                failure_code = "DEV_LAUNCH_FAILED"
                failure_details: dict[str, object] = {}
                if task_plan is not None:
                    task_cleanup = (
                        self._cleanup_task_state_locked(
                            expected_session_id=session.session_id,
                            catalog=task_plan.catalog,
                        )
                        if process_cleanup_confirmed
                        else DevResult(
                            ok=False,
                            code="DEV_CLEANUP_FAILED",
                            message="После ошибки запуска процесс не удалось безопасно завершить",
                            state=DevStatusKind.FAILED.value,
                            details={"cleanup_confirmed": False},
                        )
                    )
                    failure_details = {"cleanup": task_cleanup.as_dict()}
                    if not task_cleanup.ok:
                        failure_code = "DEV_CLEANUP_FAILED"
                session.state = DevSessionState.FAILED
                session.updated_at = self._timestamp()
                session.last_code = failure_code
                session.last_message = f"Не удалось запустить DevSession: {type(exc).__name__}"
                if failure_code == "DEV_CLEANUP_FAILED":
                    session.last_message += "; scheduler-state не подтверждён очищенным"
                self._write_session(session)
                return self._session_result(
                    session,
                    ok=False,
                    code=failure_code,
                    message=session.last_message,
                    state=DevStatusKind.FAILED,
                    details=failure_details,
                )

        assert session.process is not None
        ready, reason = self._wait_for_readiness(session.process)
        with self._locked_state():
            latest = self._read_session()
            if latest is None or latest.session_id != session.session_id:
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_STATE_CHANGED",
                    message="Маркер DevSession изменился во время запуска",
                    state=DevStatusKind.OWNERSHIP_MISMATCH.value,
                    session_id=session.session_id,
                )
            if (
                latest.state is not DevSessionState.STARTING
                or latest.process != session.process
            ):
                observed = self.status()
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_STATE_CHANGED",
                    message=(
                        "Состояние DevSession изменилось во время ожидания готовности; "
                        "более новое состояние сохранено"
                    ),
                    state=observed.state,
                    session_id=session.session_id,
                    details={"observed_code": observed.code},
                )
            if not ready:
                cleanup = self._stop_owned_process(latest.process)
                latest.state = DevSessionState.FAILED
                latest.updated_at = self._timestamp()
                failure_code = "DEV_READINESS_FAILED"
                latest.last_message = f"DevSession не достигла готовности: {reason}"
                if cleanup:
                    latest.process = None
                failure_details: dict[str, object] = {"cleanup_confirmed": cleanup}
                if task_plan is not None and cleanup:
                    task_cleanup = self._cleanup_task_state_locked(
                        expected_session_id=latest.session_id,
                        catalog=task_plan.catalog,
                    )
                    failure_details["task_cleanup"] = task_cleanup.as_dict()
                    if not task_cleanup.ok:
                        failure_code = "DEV_CLEANUP_FAILED"
                        latest.last_message += "; scheduler-state не подтверждён очищенным"
                latest.last_code = failure_code
                self._write_session(latest)
                return self._session_result(
                    latest,
                    ok=False,
                    code=failure_code,
                    message=latest.last_message,
                    state=DevStatusKind.FAILED,
                    details=failure_details,
                )
            try:
                owned = self.process_backend.matches(latest.process) if latest.process else None
            except RuntimeError:
                owned = False
            if owned is not True:
                latest.state = DevSessionState.STALE
                latest.updated_at = self._timestamp()
                latest.last_code = "DEV_OWNERSHIP_LOST"
                latest.last_message = "Владение DevSession потеряно до фиксации рабочего состояния"
                self._write_session(latest)
                return self._session_result(
                    latest,
                    ok=False,
                    code="DEV_OWNERSHIP_LOST",
                    message=latest.last_message,
                    state=DevStatusKind.OWNERSHIP_MISMATCH,
                )
            latest.state = DevSessionState.RUNNING
            latest.updated_at = self._timestamp()
            latest.last_code = "DEV_SESSION_READY"
            latest.last_message = "Dev-сессия готова"
            self._write_session(latest)
            return self._session_result(
                latest,
                ok=True,
                code="DEV_SESSION_READY",
                message="Dev-сессия готова",
                state=DevStatusKind.RUNNING_OWNED,
                details={
                    "host": self.environment.host,
                    "port": self.environment.port,
                    "profile": DEV_PROFILE,
                    "log": str(self.environment.log_file.relative_to(self.environment.repository_root)),
                },
            )

    def stop(self, *, preserve_task_state: bool = False) -> DevResult:
        with self._locked_state():
            try:
                session = self._read_session()
            except ValueError as exc:
                return DevResult(
                    ok=False,
                    code="DEV_STATE_CORRUPT",
                    message=f"Маркер DevSession повреждён; разрушительная очистка запрещена: {exc}",
                    state=DevStatusKind.CORRUPT.value,
                )
            if session is None:
                return DevResult(
                    ok=True,
                    code="DEV_STOP_NO_SESSION",
                    message="Останавливать нечего: DevSession отсутствует",
                    state=DevStatusKind.NO_SESSION.value,
                )
            if session.state is DevSessionState.STOPPED and session.process is None:
                return self._finish_stopped_locked(
                    session,
                    code="DEV_STOP_ALREADY_STOPPED",
                    message="DevSession уже остановлена",
                    preserve_task_state=preserve_task_state,
                )
            identity = session.process
            if identity is None:
                recovered = self._recover_locked()
                if recovered.ok:
                    return recovered
                return recovered
            try:
                matches = self.process_backend.matches(identity)
            except RuntimeError as exc:
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_OWNERSHIP_UNKNOWN",
                    message=f"Владение невозможно подтвердить; остановка запрещена: {exc}",
                    state=DevStatusKind.OWNERSHIP_MISMATCH,
                )
            if matches is None:
                return self._finish_stopped_locked(
                    session,
                    code="DEV_STALE_RECOVERED",
                    message="Завершённый процесс подтверждён; устаревший маркер логически закрыт",
                    preserve_task_state=preserve_task_state,
                )
            if matches is False:
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_OWNERSHIP_MISMATCH",
                    message="PID принадлежит другому процессу; остановка отклонена из соображений безопасности",
                    state=DevStatusKind.OWNERSHIP_MISMATCH,
                )
            session.state = DevSessionState.STOPPING
            session.updated_at = self._timestamp()
            session.last_code = "DEV_SESSION_STOPPING"
            session.last_message = "Останавливается принадлежащая DevSession"
            self._write_session(session)

        stopped = self._stop_owned_process(identity)
        with self._locked_state():
            latest = self._read_session()
            if latest is None or latest.session_id != session.session_id:
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_STATE_CHANGED",
                    message="Маркер DevSession изменился во время остановки",
                    state=DevStatusKind.OWNERSHIP_MISMATCH.value,
                    session_id=session.session_id,
                )
            if stopped:
                return self._finish_stopped_locked(
                    latest,
                    code="DEV_SESSION_STOPPED",
                    message="DevSession остановлена и процесс завершён",
                    preserve_task_state=preserve_task_state,
                )
            latest.state = DevSessionState.STALE
            latest.updated_at = self._timestamp()
            latest.last_code = "DEV_STOP_UNCONFIRMED"
            latest.last_message = "Не удалось безопасно подтвердить завершение DevSession"
            self._write_session(latest)
            return self._session_result(
                latest,
                ok=False,
                code="DEV_STOP_UNCONFIRMED",
                message=latest.last_message,
                state=DevStatusKind.STALE,
            )

    def recover(self) -> DevResult:
        with self._locked_state():
            return self._recover_locked()

    def smoke(self) -> DevResult:
        steps: list[dict[str, object]] = []
        preflight = self.preflight()
        steps.append(preflight.as_dict())
        if not preflight.ok:
            return DevResult(
                ok=False,
                code="DEV_SMOKE_PREFLIGHT_FAILED",
                message="Проверка жизненного цикла остановлена на предварительной проверке",
                state=preflight.state,
                details={"steps": steps},
            )
        started = self.start()
        steps.append(started.as_dict())
        if not started.ok:
            return DevResult(
                ok=False,
                code="DEV_SMOKE_START_FAILED",
                message="Проверка жизненного цикла не смогла запустить DevSession",
                state=started.state,
                session_id=started.session_id,
                details={"steps": steps},
            )
        observed = self.status()
        steps.append(observed.as_dict())
        if not observed.ok or observed.state != DevStatusKind.RUNNING_OWNED.value:
            stopped = self.stop()
            steps.append(stopped.as_dict())
            return DevResult(
                ok=False,
                code="DEV_SMOKE_STATUS_FAILED",
                message="Проверка жизненного цикла не подтвердила рабочее состояние и владение",
                state=observed.state,
                session_id=started.session_id,
                details={"steps": steps},
            )
        stopped = self.stop()
        steps.append(stopped.as_dict())
        final_status = self.status()
        steps.append(final_status.as_dict())
        ok = stopped.ok and final_status.state == DevStatusKind.STOPPED.value
        return DevResult(
            ok=ok,
            code="DEV_SMOKE_PASS" if ok else "DEV_SMOKE_STOP_FAILED",
            message=(
                "Проверка жизненного цикла Stage 1 пройдена"
                if ok
                else "Проверка жизненного цикла не подтвердила безопасную остановку"
            ),
            state=final_status.state,
            session_id=started.session_id,
            details={"steps": steps},
        )

    def _recover_locked(self) -> DevResult:
        try:
            session = self._read_session()
        except ValueError as exc:
            return DevResult(
                ok=False,
                code="DEV_STATE_CORRUPT",
                message=f"Маркер повреждён; автоматическое разрушительное восстановление запрещено: {exc}",
                state=DevStatusKind.CORRUPT.value,
            )
        if session is None:
            return DevResult(
                ok=True,
                code="DEV_RECOVERY_NOT_NEEDED",
                message="DevSession отсутствует",
                state=DevStatusKind.NO_SESSION.value,
            )
        if session.state is DevSessionState.STOPPED and session.process is None:
            return self._finish_stopped_locked(
                session,
                code="DEV_RECOVERY_NOT_NEEDED",
                message="DevSession уже находится в безопасном остановленном состоянии",
                preserve_task_state=False,
            )

        identity = session.process
        if identity is None:
            try:
                candidates = self.process_backend.find_by_session(
                    self.environment, session.session_id
                )
            except RuntimeError as exc:
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_RECOVERY_OWNERSHIP_UNKNOWN",
                    message=str(exc),
                    state=DevStatusKind.OWNERSHIP_MISMATCH,
                )
            if len(candidates) > 1:
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_RECOVERY_AMBIGUOUS",
                    message="Найдено несколько процессов с идентификатором сессии; восстановление отклонено из соображений безопасности",
                    state=DevStatusKind.OWNERSHIP_MISMATCH,
                )
            if len(candidates) == 1:
                session.process = candidates[0]
                session.state = DevSessionState.STALE
                session.updated_at = self._timestamp()
                session.last_code = "DEV_RECOVERY_PROCESS_FOUND"
                session.last_message = "Процесс найден по идентификатору сессии; разрушительное восстановление не выполнялось"
                self._write_session(session)
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_RECOVERY_PROCESS_FOUND",
                    message=session.last_message,
                    state=DevStatusKind.STALE,
                )
            return self._finish_stopped_locked(
                session,
                code="DEV_STALE_RECOVERED",
                message="Незавершённый маркер не имеет живого процесса; состояние закрыто без завершения процессов",
                preserve_task_state=False,
            )

        try:
            matches = self.process_backend.matches(identity)
        except RuntimeError as exc:
            return self._session_result(
                session,
                ok=False,
                code="DEV_RECOVERY_OWNERSHIP_UNKNOWN",
                message=str(exc),
                state=DevStatusKind.OWNERSHIP_MISMATCH,
            )
        if matches is None:
            return self._finish_stopped_locked(
                session,
                code="DEV_STALE_RECOVERED",
                message="Устаревший маркер закрыт после подтверждения отсутствия процесса",
                preserve_task_state=False,
            )
        if matches is False:
            return self._session_result(
                session,
                ok=False,
                code="DEV_OWNERSHIP_MISMATCH",
                message="PID из маркера принадлежит другому процессу; восстановление отклонено из соображений безопасности",
                state=DevStatusKind.OWNERSHIP_MISMATCH,
            )
        return self._session_result(
            session,
            ok=False,
            code="DEV_SESSION_ACTIVE",
            message="Точно принадлежащий DevSession процесс ещё работает; восстановление не выполняет разрушительную очистку",
            state=(
                DevStatusKind.RUNNING_OWNED
                if session.state is DevSessionState.RUNNING
                else DevStatusKind.STALE
            ),
        )

    def _wait_for_readiness(self, identity: ProcessIdentity) -> tuple[bool, str]:
        deadline = time.monotonic() + self.ready_timeout
        last_reason = "готовность ещё не подтверждена"
        while True:
            try:
                matches = self.process_backend.matches(identity)
            except RuntimeError as exc:
                return False, str(exc)
            if matches is None:
                return False, "корневой процесс завершился до подтверждения готовности"
            if matches is False:
                return False, "владение корневым процессом изменилось до подтверждения готовности"
            ready, reason = self.readiness_probe(self.environment, identity)
            if ready:
                return True, reason
            last_reason = reason
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, last_reason
            time.sleep(min(0.25, remaining))

    def _stop_owned_process(self, identity: ProcessIdentity | None) -> bool:
        if identity is None:
            return True
        try:
            matches = self.process_backend.matches(identity)
        except RuntimeError:
            return False
        if matches is None:
            return True
        if matches is not True:
            return False
        try:
            self.process_backend.request_stop(identity)
            if self.process_backend.wait_exit(identity, self.stop_timeout):
                return True
        except RuntimeError:
            return False
        try:
            matches = self.process_backend.matches(identity)
        except RuntimeError:
            return False
        if matches is None:
            return True
        if matches is not True:
            return False
        try:
            if not self.process_backend.force_stop(identity):
                return False
            return self.process_backend.wait_exit(identity, 5.0)
        except RuntimeError:
            return False

    def _read_session(self) -> DevSession | None:
        try:
            raw = self.environment.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if len(raw) > 64 * 1024:
            raise ValueError("маркер превышает допустимый размер")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("маркер содержит некорректный JSON") from exc
        session = DevSession.from_dict(payload)
        if not _same_path(session.repository_root, str(self.environment.repository_root)):
            raise ValueError("маркер принадлежит другой рабочей копии")
        return session

    def _write_session(self, session: DevSession) -> None:
        self.environment.state_file.parent.mkdir(parents=True, exist_ok=True)
        target = str(self.environment.state_file)
        temp = to_tmp_file(target)
        payload = json.dumps(session.as_dict(), ensure_ascii=True, sort_keys=True) + "\n"
        try:
            file_write(temp, payload)
            replace_tmp(temp, target)
        finally:
            try:
                Path(temp).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Сбой best-effort cleanup не должен скрывать исходную ошибку записи.
                pass

    def _raw_state_bytes(self) -> bytes | None:
        try:
            return self.environment.state_file.read_bytes()
        except FileNotFoundError:
            return None

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        self.environment.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with _state_thread_lock, _exclusive_file_lock(self.environment.lock_file):
            yield

    def _timestamp(self) -> str:
        return self.now().isoformat()

    @staticmethod
    def _session_result(
        session: DevSession,
        *,
        ok: bool,
        code: str,
        message: str,
        state: DevStatusKind,
        details: dict[str, object] | None = None,
    ) -> DevResult:
        return DevResult(
            ok=ok,
            code=code,
            message=message,
            state=state.value,
            session_id=session.session_id,
            details=details or {},
        )


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + _LOCK_TIMEOUT
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время ожидания блокировки DevSession")
                    time.sleep(_LOCK_RETRY_INTERVAL)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Истекло время ожидания блокировки DevSession")
                    time.sleep(_LOCK_RETRY_INTERVAL)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
