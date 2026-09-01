"""Менеджер жизненного цикла локальной DevSession назначенного target."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from deploy.atomic import file_write, replace_tmp, to_tmp_file
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.contracts import (
    DEFAULT_READY_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionState,
    DevStatusKind,
    DevTaskMode,
    DevTaskPhase,
    ProcessIdentity,
)
from module.dev_runtime.control import (
    ControlAction,
    ControlStore,
    RuntimeControlManager,
    RuntimeSessionState,
)
from module.dev_runtime.coordination import (
    RuntimeCoordinationError,
    runtime_coordination_lock,
)
from module.dev_runtime.diagnostics import (
    DevDiagnosticsMixin,
    _default_storage_probe,
    _port_is_listening,
)
from module.dev_runtime.evidence import (
    EvidenceCorrupt,
    EvidenceError,
    EvidenceScreenshot,
    EvidenceStore,
    validate_session_id,
)
from module.dev_runtime.process import ProcessBackend, _same_path
from module.dev_runtime.target import (
    DevTarget,
    DevTargetError,
    DevTargetRegistry,
    target_identity,
)
from module.dev_runtime.task_sandbox import (
    SCHEDULER_RESET_TIME,
    TASK_POLICY_ACTIVE,
    TASK_POLICY_PRESERVED,
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
_SAFE_GAME_ERROR_CODES = frozenset(
    {
        "DEV_GAME_CAPABILITY_CONFLICT",
        "DEV_GAME_CAPABILITY_INVALID",
        "DEV_GAME_CAPABILITY_LIMIT",
        "DEV_GAME_CAPABILITY_UNAVAILABLE",
        "DEV_GAME_CHECKPOINT_DUPLICATE",
        "DEV_GAME_CHECKPOINT_POLICY_INVALID",
        "DEV_GAME_MORALE_UNKNOWN",
        "DEV_GAME_OBSERVATION_CHECKSUM_INVALID",
        "DEV_GAME_OBSERVATION_CHECKSUM_MISMATCH",
        "DEV_GAME_OBSERVATION_CORRUPT",
        "DEV_GAME_OBSERVATION_INVALID",
        "DEV_GAME_OBSERVATION_LIMIT",
        "DEV_GAME_OBSERVATION_PAYLOAD_INVALID",
        "DEV_GAME_OBSERVATION_PROVIDER_INVALID",
        "DEV_GAME_OBSERVATION_PROVIDER_UNAVAILABLE",
        "DEV_GAME_OBSERVATION_SCHEMA_UNSUPPORTED",
        "DEV_GAME_OBSERVATION_SCOPE_MISMATCH",
        "DEV_GAME_OBSERVATION_TARGET_INVALID",
        "DEV_GAME_OBSERVATION_TARGET_MISMATCH",
        "DEV_GAME_OBSERVATION_TOO_LARGE",
        "DEV_GAME_OBSERVATION_UNSAFE_PATH",
        "DEV_GAME_OBSERVATION_WRITE_FAILED",
        "DEV_GAME_PARAMETERS_INVALID",
        "DEV_GAME_PROVIDER_UNAVAILABLE",
    }
)


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
        screenshot_provider: Callable[[str], object] | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        screenshot_timeout: float = 5.0,
        target_locked: bool = False,
        game_bridge_factory: Callable[[DevEnvironment], object] | None = None,
        database_diagnostics_factory: Callable[[DevEnvironment], object] | None = None,
    ):
        self.environment = environment or DevEnvironment.current()
        self.process_backend = process_backend or ProcessBackend()
        self.storage_probe = storage_probe or _default_storage_probe
        self.port_probe = port_probe or _port_is_listening
        self.readiness_probe = readiness_probe or self._default_readiness_probe
        self.now = now or (lambda: datetime.now(UTC))
        self.session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))
        self.screenshot_provider = screenshot_provider
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout
        self.screenshot_timeout = screenshot_timeout
        self._target_locked = target_locked
        self._game_bridge_factory = game_bridge_factory
        self._database_diagnostics_factory = database_diagnostics_factory
        self._evidence_store: EvidenceStore | None = None
        self._smoke_manager = None
        self._control_manager: RuntimeControlManager | None = None
        self._game_bridge: object | None = None
        self._database_diagnostics: object | None = None

    def _refresh_target(self) -> None:
        """Обновить target перед новым вызовом долгоживущего manager."""

        if self._target_locked:
            return
        try:
            current_target = DevTargetRegistry.load_for_environment(
                self.environment.repository_root,
                fallback=self.environment.dev_target,
            )
        except DevTargetError as exc:
            raise TaskSandboxError(exc.code, str(exc)) from exc
        if current_target == self.environment.dev_target:
            return
        self.environment = replace(self.environment, dev_target=current_target)
        # Эти фасады держат environment внутри себя; после смены registry они
        # не должны продолжать новые операции с прежним target.
        self._evidence_store = None
        self._smoke_manager = None
        self._control_manager = None
        self._game_bridge = None
        self._database_diagnostics = None

    def _session_profile_name(self, session: DevSession) -> str | None:
        return session.profile_name or (
            session.process.command_profile_name() if session.process is not None else None
        )

    def _environment_for_session(self, session: DevSession) -> DevEnvironment:
        profile_name = self._session_profile_name(session)
        if profile_name is None:
            if session.target_identity is not None:
                raise TaskSandboxError(
                    "DEV_TARGET_SESSION_MISMATCH",
                    "DevSession содержит target identity без profile_name",
                )
            return self.environment
        try:
            target = DevTarget(profile_name)
        except ValueError as exc:
            raise TaskSandboxError(
                "DEV_TARGET_INVALID",
                "Профиль DevSession нельзя безопасно разрешить в development target",
            ) from exc
        expected_identity = target_identity(target)
        if (
            session.target_identity is not None
            and session.target_identity != expected_identity
        ):
            raise TaskSandboxError(
                "DEV_TARGET_SESSION_MISMATCH",
                "DevSession не соответствует записанной target identity",
            )
        if target == self.environment.dev_target:
            return self.environment
        return replace(self.environment, dev_target=target)

    def _evidence_for_session(
        self,
        session_id: str,
        *,
        profile_name: str | None = None,
        validate_profile: bool = True,
    ) -> EvidenceStore | None:
        try:
            store = EvidenceStore.for_session(
                self.environment,
                session_id,
                profile_name=profile_name,
                validate_profile=validate_profile,
            )
        except (EvidenceError, ValueError):
            return None
        if not store.exists:
            return None
        return store

    def _evidence_store_for_current_session(self) -> EvidenceStore | None:
        try:
            session = self._read_session()
        except (OSError, ValueError):
            return None
        if session is None or not session.is_task_aware:
            return None
        cached = self._evidence_store
        if cached is not None and cached.session_id == session.session_id:
            return cached
        return self._evidence_for_session(
            session.session_id,
            profile_name=self._session_profile_name(session),
        )

    def _evidence_event(
        self,
        event_type: str,
        fields: dict[str, object] | None = None,
        *,
        store: EvidenceStore | None = None,
    ) -> None:
        active_store = store if store is not None else self._evidence_store_for_current_session()
        if active_store is None:
            return
        try:
            active_store.append_event(event_type, fields or {}, timestamp=self._timestamp())
        except Exception:
            active_store.mark_degraded("timeline_write_failed")

    def _evidence_error(
        self,
        exception: BaseException,
        *,
        phase: str,
        store: EvidenceStore | None = None,
    ) -> None:
        active_store = store if store is not None else self._evidence_store_for_current_session()
        if active_store is None:
            return
        try:
            active_store.record_error(exception, phase=phase)
        except Exception:
            active_store.mark_degraded("error_record_failed")

    def _finish_failed_evidence(
        self,
        session: DevSession,
        *,
        process_started: bool,
        process_stopped: bool,
        cleanup_attempted: bool,
        cleanup_confirmed: bool,
        reason: str,
    ) -> None:
        """Закрыть диагностические данные после неуспешного запуска, не скрывая неопределённость."""

        store = self._evidence_store
        if store is None and session.is_task_aware:
            store = self._evidence_for_session(
                session.session_id,
                profile_name=self._session_profile_name(session),
            )
        if store is None:
            return
        if process_started:
            self._evidence_event(
                "process_stopped",
                {"confirmed": process_stopped, "reason": reason},
                store=store,
            )
        if cleanup_attempted:
            self._evidence_event("cleanup_started", {"preserved": False}, store=store)
            if cleanup_confirmed:
                self._evidence_event(
                    "cleanup_completed",
                    {"confirmed": True, "preserved": False},
                    store=store,
                )
            else:
                self._evidence_event(
                    "runtime_warning",
                    {"code": "DEV_CLEANUP_FAILED", "phase": "cleanup"},
                    store=store,
                )
        if (not process_started or process_stopped) and cleanup_confirmed:
            self._evidence_event(
                "session_stopped",
                {"state": session.state.value},
                store=store,
            )
            try:
                store.finalize(
                    stopped_at=session.updated_at,
                    cleanup_confirmed=True,
                )
            except Exception:
                store.mark_degraded("timeline_write_failed")
        elif not process_started or process_stopped:
            try:
                store.finalize(
                    stopped_at=session.updated_at,
                    cleanup_confirmed=False,
                )
            except Exception:
                store.mark_degraded("timeline_write_failed")

    def _initialize_evidence(self, session: DevSession, task_plan: TaskPlan) -> None:
        try:
            store = EvidenceStore.create(
                self.environment,
                session_id=session.session_id,
                root_tasks=task_plan.root_tasks,
                excluded_tasks=task_plan.excluded_tasks,
                timestamp=session.created_at,
                now=self.now,
            )
            self._evidence_store = store
            self._evidence_event(
                "session_created",
                {"profile": self.environment.profile_name, "task_mode": "task_aware"},
                store=store,
            )
            if not EvidenceStore.prune(
                self.environment,
                active_session_id=session.session_id,
                now=self.now,
            ):
                store.mark_degraded("retention_failed")
        except Exception:
            # Сбой диагностических данных не должен скрывать обычный путь жизненного цикла.
            self._evidence_store = None

    def _evidence_target(
        self,
        session_id: str | None,
    ) -> tuple[DevSession | None, EvidenceStore | None, DevResult | None]:
        try:
            current = self._read_session()
        except ValueError as exc:
            return None, None, DevResult(
                False,
                "DEV_STATE_CORRUPT",
                f"Маркер DevSession повреждён: {exc}",
                DevStatusKind.CORRUPT.value,
            )
        if session_id is None:
            if current is None:
                return None, None, DevResult(
                    False,
                    "DEV_EVIDENCE_NOT_COLLECTED",
                    "Известная DevSession отсутствует",
                    DevStatusKind.NO_SESSION.value,
                    details={
                        "evidence_health": {
                            "status": "unavailable",
                            "reasons": ["not_collected"],
                        }
                    },
                )
            target_id = current.session_id
        else:
            try:
                target_id = validate_session_id(session_id)
            except ValueError:
                return None, None, DevResult(
                    False,
                    "DEV_EVIDENCE_SESSION_INVALID",
                    "session_id имеет недопустимый формат",
                    DevStatusKind.FAILED.value,
                )
        target_session = (
            current if current is not None and current.session_id == target_id else None
        )
        store = self._evidence_for_session(
            target_id,
            profile_name=(
                self._session_profile_name(target_session)
                if target_session is not None
                else None
            ),
            validate_profile=target_session is not None,
        )
        if store is None:
            return current, None, DevResult(
                False,
                "DEV_EVIDENCE_NOT_COLLECTED",
                "Для этой DevSession диагностические данные ещё не собирались",
                current.state.value if current is not None and current.session_id == target_id else DevStatusKind.STOPPED.value,
                target_id,
                {
                    "evidence_health": {
                        "status": "unavailable",
                        "reasons": ["not_collected"],
                    }
                },
            )
        return current, store, None

    def get_evidence(self, *, session_id: str | None = None) -> DevResult:
        current, store, error = self._evidence_target(session_id)
        if error is not None:
            return error
        assert store is not None
        active_owned = False
        if current is not None and current.session_id == store.session_id:
            if current.state is DevSessionState.RUNNING and current.process is not None:
                try:
                    active_owned = self.process_backend.matches(current.process) is True
                except RuntimeError:
                    active_owned = False
        try:
            summary = store.summary(active_owned=active_owned)
        except EvidenceCorrupt as exc:
            store.mark_corrupt("evidence_corrupt")
            return DevResult(
                False,
                exc.code,
                str(exc),
                DevStatusKind.CORRUPT.value,
                store.session_id,
                {"evidence_health": {"status": "corrupt", "reasons": ["evidence_corrupt"]}},
            )
        except EvidenceError as exc:
            return DevResult(
                False,
                exc.code,
                str(exc),
                DevStatusKind.CORRUPT.value,
                store.session_id,
                {"evidence_health": {"status": "corrupt", "reasons": ["read_failed"]}},
            )
        return DevResult(
            True,
            "DEV_EVIDENCE_READY",
            "Сводка диагностики готова",
            current.state.value if current is not None and current.session_id == store.session_id else DevStatusKind.STOPPED.value,
            store.session_id,
            summary,
        )

    def get_timeline(
        self,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> DevResult:
        current, store, error = self._evidence_target(session_id)
        if error is not None:
            return error
        assert store is not None
        try:
            page = store.timeline_page(after_sequence=after_sequence, limit=limit)
        except EvidenceCorrupt as exc:
            store.mark_corrupt("timeline_corrupt")
            return DevResult(
                False,
                exc.code,
                str(exc),
                DevStatusKind.CORRUPT.value,
                store.session_id,
                {"evidence_health": {"status": "corrupt", "reasons": ["timeline_corrupt"]}},
            )
        except EvidenceError as exc:
            return DevResult(False, exc.code, str(exc), "failed", store.session_id)
        return DevResult(
            True,
            "DEV_TIMELINE_READY",
            "Каноническая хронология выполнения прочитана",
            current.state.value if current is not None and current.session_id == store.session_id else DevStatusKind.STOPPED.value,
            store.session_id,
            page,
        )

    def get_logs(
        self,
        *,
        session_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DevResult:
        current, store, error = self._evidence_target(session_id)
        if error is not None:
            return error
        assert store is not None
        active_owned = False
        if current is not None and current.session_id == store.session_id:
            if current.state is DevSessionState.RUNNING and current.process is not None:
                try:
                    active_owned = self.process_backend.matches(current.process) is True
                except RuntimeError:
                    active_owned = False
        try:
            page = store.logs_page(cursor=cursor, limit=limit, active_owned=active_owned)
        except EvidenceCorrupt as exc:
            store.mark_corrupt("log_corrupt")
            return DevResult(
                False,
                exc.code,
                str(exc),
                DevStatusKind.CORRUPT.value,
                store.session_id,
                {"evidence_health": {"status": "corrupt", "reasons": ["log_corrupt"]}},
            )
        except EvidenceError as exc:
            return DevResult(False, exc.code, str(exc), "failed", store.session_id)
        return DevResult(
            True,
            "DEV_LOGS_READY",
            "Журнал в пределах сессии прочитан",
            current.state.value if current is not None and current.session_id == store.session_id else DevStatusKind.STOPPED.value,
            store.session_id,
            page,
        )

    def get_screenshot(self) -> EvidenceScreenshot:
        try:
            session = self._read_session()
        except ValueError as exc:
            return EvidenceScreenshot(
                DevResult(False, "DEV_STATE_CORRUPT", str(exc), DevStatusKind.CORRUPT.value)
            )
        if session is None:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_SCREENSHOT_NO_SESSION",
                    "Нет активной DevSession для наблюдения за снимком экрана",
                    DevStatusKind.NO_SESSION.value,
                )
            )
        if session.state is not DevSessionState.RUNNING or session.process is None:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_SCREENSHOT_SESSION_NOT_ACTIVE",
                    "Снимок экрана разрешён только для активной DevSession",
                    session.state.value,
                    session.session_id,
                )
            )
        try:
            owned = self.process_backend.matches(session.process)
        except RuntimeError as exc:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_SCREENSHOT_OWNERSHIP_UNKNOWN",
                    f"Владение DevSession невозможно подтвердить: {exc}",
                    DevStatusKind.OWNERSHIP_MISMATCH.value,
                    session.session_id,
                )
            )
        if owned is not True:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_SCREENSHOT_OWNERSHIP_MISMATCH",
                    "Снимок экрана не привязывается к неизвестному или чужому процессу",
                    DevStatusKind.OWNERSHIP_MISMATCH.value,
                    session.session_id,
                )
            )
        store = self._evidence_for_session(
            session.session_id,
            profile_name=self._session_profile_name(session),
        )
        if store is None:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_EVIDENCE_NOT_COLLECTED",
                    "Для активной DevSession отсутствует хранилище диагностики",
                    DevStatusKind.FAILED.value,
                    session.session_id,
                )
            )
        if self.screenshot_provider is not None:
            try:
                return store.persist_screenshot(self.screenshot_provider(session.session_id))
            except Exception as exc:
                store.mark_degraded("screenshot_failed")
                return EvidenceScreenshot(
                    DevResult(
                        False,
                        "DEV_SCREENSHOT_PROVIDER_FAILED",
                        f"Источник снимка экрана завершился: {type(exc).__name__}",
                        "failed",
                        session.session_id,
                    )
                )
        return store.request_screenshot(timeout=self.screenshot_timeout)

    def get_historical_screenshot(self, *, session_id: str, screenshot_id: str) -> EvidenceScreenshot:
        """Прочитать сохранённый снимок Evidence API по двум проверенным идентификаторам."""

        try:
            target_id = validate_session_id(session_id)
        except ValueError:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_SCREENSHOT_SESSION_INVALID",
                    "session_id имеет недопустимый формат",
                    DevStatusKind.FAILED.value,
                )
            )
        store = self._evidence_for_session(target_id, validate_profile=False)
        if store is None:
            return EvidenceScreenshot(
                DevResult(
                    False,
                    "DEV_EVIDENCE_NOT_COLLECTED",
                    "Для этой DevSession диагностические данные отсутствуют",
                    DevStatusKind.STOPPED.value,
                    target_id,
                )
            )
        return store.read_persisted_screenshot(screenshot_id)

    def _get_smoke_manager(self):
        """Лениво создать Smoke facade, не меняя startup обычного runtime."""

        self._refresh_target()
        smoke_manager = self._smoke_manager
        if smoke_manager is not None:
            return smoke_manager
        from module.dev_runtime.smoke import SmokeRunManager

        smoke_manager = SmokeRunManager(
            environment=self.environment,
            now=self.now,
            game_bridge_factory=lambda: self._get_game_bridge(refresh_target=False),
        )
        self._smoke_manager = smoke_manager
        return smoke_manager

    def list_smoke_capabilities(self) -> DevResult:
        return self._get_smoke_manager().list_capabilities()

    def validate_smoke(self, spec: object) -> DevResult:
        return self._get_smoke_manager().validate_smoke(spec)

    def start_smoke(self, spec: object) -> DevResult:
        return self._get_smoke_manager().start_smoke(spec)

    def get_smoke(self, smoke_id: str) -> DevResult:
        return self._get_smoke_manager().get_smoke(smoke_id)

    def cancel_smoke(self, smoke_id: str) -> DevResult:
        return self._get_smoke_manager().cancel_smoke(smoke_id)

    def get_smoke_evaluation(self, smoke_id: str) -> EvidenceScreenshot:
        return self._get_smoke_manager().get_smoke_evaluation(smoke_id)

    def submit_smoke_evaluation(
        self,
        smoke_id: str,
        assertion_id: str,
        verdict: str,
        rationale: str,
    ) -> DevResult:
        return self._get_smoke_manager().submit_smoke_evaluation(smoke_id, assertion_id, verdict, rationale)

    def _get_game_bridge(self, *, refresh_target: bool = True) -> object:
        if refresh_target:
            self._refresh_target()
        bridge = self._game_bridge
        if bridge is not None:
            return bridge
        if self._game_bridge_factory is not None:
            bridge = self._game_bridge_factory(self.environment)
        else:
            from module.dev_runtime.game_bridge import build_runtime_game_bridge

            bridge = build_runtime_game_bridge(self.environment, clock=self.now)
        self._game_bridge = bridge
        return bridge

    def _get_database_diagnostics(self, *, refresh_target: bool = True) -> object:
        if refresh_target:
            self._refresh_target()
        diagnostics = self._database_diagnostics
        if diagnostics is not None:
            return diagnostics
        if self._database_diagnostics_factory is not None:
            diagnostics = self._database_diagnostics_factory(self.environment)
        else:
            from module.persistence.runtime import build_runtime_database_diagnostics

            diagnostics = build_runtime_database_diagnostics(self.environment)
        self._database_diagnostics = diagnostics
        return diagnostics

    def _observation_target(
        self,
        session_id: str | None,
    ) -> tuple[DevEnvironment | None, DevSession | None, DevResult | None]:
        try:
            self._refresh_target()
            current = self._read_session()
        except TaskSandboxError as exc:
            return None, None, self._task_error(exc)
        except (OSError, ValueError) as exc:
            return (
                None,
                None,
                DevResult(
                    False,
                    "DEV_STATE_CORRUPT",
                    f"Маркер DevSession повреждён: {type(exc).__name__}",
                    DevStatusKind.CORRUPT.value,
                ),
            )
        if session_id is not None:
            try:
                session_id = validate_session_id(session_id)
            except ValueError:
                return (
                    None,
                    current,
                    DevResult(
                        False,
                        "DEV_SESSION_ID_INVALID",
                        "session_id имеет недопустимый формат",
                        DevStatusKind.FAILED.value,
                    ),
                )
            if current is None or current.session_id != session_id:
                return (
                    None,
                    current,
                    DevResult(
                        False,
                        "DEV_SESSION_NOT_FOUND",
                        "Указанная DevSession не является текущей сессией",
                        DevStatusKind.NO_SESSION.value,
                        session_id,
                    ),
                )
        if current is None:
            return self.environment, None, None
        try:
            return self._environment_for_session(current), current, None
        except TaskSandboxError as exc:
            return None, current, self._task_error(exc)

    def list_game_observation_capabilities(self) -> DevResult:
        try:
            bridge = self._get_game_bridge()
            descriptors = bridge.descriptors()
            return DevResult(
                True,
                "DEV_GAME_OBSERVATION_CAPABILITIES_READY",
                "Реестр capabilities game observation прочитан",
                DevStatusKind.NO_SESSION.value,
                details={"capabilities": [item.as_dict() for item in descriptors]},
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except Exception as exc:
            return DevResult(
                False,
                "DEV_GAME_OBSERVATION_UNAVAILABLE",
                f"Реестр game observations недоступен: {type(exc).__name__}",
                DevStatusKind.FAILED.value,
            )

    def get_game_observation(
        self,
        capability_id: str,
        parameters: Mapping[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> DevResult:
        environment, session, error = self._observation_target(session_id)
        if error is not None:
            return error
        assert environment is not None
        try:
            from module.dev_runtime.game_bridge import (
                GameObservationError,
                GameObservationSnapshot,
            )

            bridge = self._get_game_bridge(refresh_target=False)
            snapshot = bridge.capture(
                environment.dev_target,
                capability_id,
                parameters,
                checkpoint_id="standalone",
                session_id=session.session_id if session is not None else None,
                captured_at=self.now(),
            )
            expected_target = target_identity(environment.dev_target)
            expected_session_id = session.session_id if session is not None else None
            if not isinstance(snapshot, GameObservationSnapshot):
                raise GameObservationError(
                    "DEV_GAME_OBSERVATION_PROVIDER_INVALID",
                    "Bridge вернул некорректный game snapshot",
                )
            if (
                snapshot.profile_name != environment.profile_name
                or snapshot.target_identity != expected_target
                or snapshot.session_id != expected_session_id
            ):
                raise GameObservationError(
                    "DEV_GAME_OBSERVATION_TARGET_MISMATCH",
                    "Bridge вернул observation с другой session или target",
                )
            if snapshot.checkpoint_id != "standalone" or snapshot.capability_id != capability_id:
                raise GameObservationError(
                    "DEV_GAME_OBSERVATION_PROVIDER_INVALID",
                    "Bridge вернул observation с другой checkpoint или capability",
                )
            known = snapshot.status.value == "known"
            code = (
                "DEV_GAME_OBSERVATION_READY"
                if known
                else (
                    "DEV_GAME_OBSERVATION_UNKNOWN"
                    if snapshot.status.value == "unknown"
                    else "DEV_GAME_OBSERVATION_UNAVAILABLE"
                )
            )
            return DevResult(
                known,
                code,
                "Наблюдение game прочитано" if known else "Наблюдение game не подтверждено",
                session.state.value if session is not None else DevStatusKind.NO_SESSION.value,
                session.session_id if session is not None else None,
                {"observation": snapshot.as_dict()},
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except Exception as exc:
            raw_code = getattr(exc, "code", None)
            code = (
                raw_code
                if isinstance(raw_code, str) and raw_code in _SAFE_GAME_ERROR_CODES
                else "DEV_GAME_OBSERVATION_UNAVAILABLE"
            )
            return DevResult(
                False,
                code,
                f"Наблюдение game недоступно: {type(exc).__name__}",
                session.state.value if session is not None else DevStatusKind.NO_SESSION.value,
                session.session_id if session is not None else None,
            )

    def capture_smoke_game_checkpoint(
        self,
        smoke_id: str,
        checkpoint_id: str,
    ) -> DevResult:
        return self._get_smoke_manager().capture_game_checkpoint(
            smoke_id,
            checkpoint_id,
        )

    def get_smoke_game_observations(
        self,
        smoke_id: str,
        checkpoint_id: str | None = None,
    ) -> DevResult:
        return self._get_smoke_manager().get_game_observations(
            smoke_id,
            checkpoint_id=checkpoint_id,
        )

    @staticmethod
    def _database_check_dict(value: object) -> dict[str, object]:
        as_dict = getattr(value, "as_dict", None)
        if not callable(as_dict):
            raise TypeError("Диагностика базы данных вернула объект без as_dict()")
        payload = as_dict()
        if not isinstance(payload, dict):
            raise TypeError("Диагностика базы данных вернула некорректный словарь")
        return payload

    def get_database_status(self, *, session_id: str | None = None) -> DevResult:
        environment, session, error = self._observation_target(session_id)
        if error is not None:
            return error
        assert environment is not None
        state = session.state.value if session is not None else DevStatusKind.NO_SESSION.value
        resolved_session_id = session.session_id if session is not None else None
        try:
            diagnostics = self._get_database_diagnostics(refresh_target=False)
            snapshot = diagnostics.get_status(environment.profile_name)
            return DevResult(
                True,
                "DEV_DATABASE_STATUS_READY",
                "Сводка developer-only диагностики PostgreSQL прочитана",
                state,
                resolved_session_id,
                {"database_status": self._database_check_dict(snapshot)},
            )
        except Exception as exc:
            return DevResult(
                False,
                "DEV_DATABASE_DIAGNOSTICS_UNAVAILABLE",
                f"PostgreSQL diagnostics недоступны: {type(exc).__name__}",
                DevStatusKind.FAILED.value,
                resolved_session_id,
            )

    def list_database_checks(self) -> DevResult:
        try:
            diagnostics = self._get_database_diagnostics()
            checks = diagnostics.list_checks()
            return DevResult(
                True,
                "DEV_DATABASE_CHECKS_READY",
                "Каталог фиксированных диагностических проверок PostgreSQL прочитан",
                DevStatusKind.NO_SESSION.value,
                details={"database_checks": [self._database_check_dict(item) for item in checks]},
            )
        except Exception as exc:
            return DevResult(
                False,
                "DEV_DATABASE_DIAGNOSTICS_UNAVAILABLE",
                f"Каталог PostgreSQL diagnostics недоступен: {type(exc).__name__}",
                DevStatusKind.FAILED.value,
            )

    def run_database_check(
        self,
        check_id: str,
        *,
        session_id: str | None = None,
    ) -> DevResult:
        environment, session, error = self._observation_target(session_id)
        if error is not None:
            return error
        assert environment is not None
        state = session.state.value if session is not None else DevStatusKind.NO_SESSION.value
        resolved_session_id = session.session_id if session is not None else None
        try:
            diagnostics = self._get_database_diagnostics(refresh_target=False)
            result = diagnostics.run_check(check_id, environment.profile_name)
            status = getattr(result, "status", None)
            status_value = getattr(status, "value", status)
            ok = status_value == "pass"
            return DevResult(
                ok,
                "DEV_DATABASE_CHECK_PASS" if ok else "DEV_DATABASE_CHECK_NOT_PASS",
                "Диагностическая проверка PostgreSQL завершена",
                state,
                resolved_session_id,
                {"database_check": self._database_check_dict(result)},
            )
        except Exception as exc:
            return DevResult(
                False,
                "DEV_DATABASE_DIAGNOSTICS_UNAVAILABLE",
                f"PostgreSQL diagnostic check недоступен: {type(exc).__name__}",
                DevStatusKind.FAILED.value,
                resolved_session_id,
            )

    def list_database_repairs(self) -> DevResult:
        return DevResult(
            True,
            "DEV_DATABASE_REPAIRS_READY",
            "Каталог безопасных восстановлений базы данных пуст",
            DevStatusKind.NO_SESSION.value,
            details={"repairs": []},
        )

    def preview_database_repair(
        self,
        repair_id: str,
        *,
        session_id: str | None = None,
    ) -> DevResult:
        return DevResult(
            False,
            "DEV_DATABASE_REPAIR_UNAVAILABLE",
            "Для текущего database contract безопасные восстановления не зарегистрированы",
            DevStatusKind.NO_SESSION.value,
            session_id,
            {"repair": {"repair_id": repair_id, "available": False}},
        )

    def _get_control_manager(self) -> RuntimeControlManager:
        self._refresh_target()
        control_manager = self._control_manager
        if control_manager is None:
            control_manager = RuntimeControlManager(
                self.environment,
                session_state_provider=self._control_session_state,
                smoke_active_provider=self._control_smoke_active,
                now=self.now,
            )
            self._control_manager = control_manager
        return control_manager

    def _control_session_state(self) -> RuntimeSessionState | None:
        session = self._read_session()
        if session is None:
            return None
        process_alive = None
        if session.process is not None:
            try:
                process_alive = self.process_backend.matches(session.process)
            except RuntimeError:
                process_alive = None
        return RuntimeSessionState(session.state.value, process_alive)

    def _control_smoke_active(self) -> bool:
        return self._get_smoke_manager().has_active_run()

    def get_runtime_status(self) -> DevResult:
        return self._get_control_manager().status()

    def start_game(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.START_GAME)

    def stop_game(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.STOP_GAME)

    def restart_game(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.RESTART_GAME)

    def start_emulator(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.START_EMULATOR)

    def stop_emulator(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.STOP_EMULATOR)

    def restart_emulator(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.RESTART_EMULATOR)

    def restart_adb(self) -> DevResult:
        return self._get_control_manager().start(ControlAction.RESTART_ADB)

    def get_control_operation(self, control_id: str) -> DevResult:
        return self._get_control_manager().get_operation(control_id)

    def list_tasks(self) -> DevResult:
        """Вернуть каталог из исходного профиля без изменения состояния."""

        try:
            self._refresh_target()
            catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
                profile_name=self.environment.profile_name,
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        return DevResult(
            ok=True,
            code="DEV_TASK_CATALOG_READY",
            message="Каталог планируемых задач development target прочитан",
            state=DevStatusKind.NO_SESSION.value,
            details=catalog.as_dict(),
        )

    def status(self) -> DevResult:
        """Вернуть статус Dev Runtime и безопасный снимок политики задач только для чтения."""

        try:
            self._refresh_target()
            result = super().status()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        try:
            session = self._read_session()
        except (OSError, ValueError):
            # Базовый status уже вернул машиночитаемую ошибку маркера.
            return result
        details = dict(result.details)
        if session is not None:
            details["task_lifecycle"] = session.task_lifecycle_as_dict()
        try:
            task_policy_environment = (
                self._environment_for_session(session)
                if session is not None
                else self.environment
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        task_policy = TaskPolicyStore(task_policy_environment).inspect()
        if task_policy.get("present") is not True:
            if session is not None and session.task_cleanup_needed:
                return DevResult(
                    ok=False,
                    code="DEV_TASK_POLICY_MISSING",
                    message="Жизненный цикл с учётом задач требует политики до подтверждённой очистки",
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
        details["task_policy"] = task_policy
        if session is not None and session.task_cleanup_needed and task_policy.get("valid") is not True:
            return DevResult(
                ok=False,
                code=str(task_policy.get("code", "DEV_TASK_POLICY_INVALID")),
                message="Политику задач невозможно безопасно подтвердить",
                state=result.state,
                session_id=result.session_id,
                details=details,
            )
        if task_policy.get("valid") is False:
            code = str(task_policy.get("code", "DEV_TASK_POLICY_INVALID"))
            return DevResult(
                ok=False,
                code=code,
                message="Политику задач невозможно безопасно подтвердить",
                state=result.state,
                session_id=result.session_id,
                details=details,
            )
        if session is not None and session.task_cleanup_needed:
            policy_state = task_policy.get("state")
            if policy_state == TASK_POLICY_PRESERVED:
                return DevResult(
                    ok=False,
                    code="DEV_TASK_STATE_PRESERVED",
                    message="Состояние планировщика сохранено по явному запросу; требуется очистка",
                    state=result.state,
                    session_id=result.session_id,
                    details=details,
                )
            if policy_state != TASK_POLICY_ACTIVE:
                return DevResult(
                    ok=False,
                    code="DEV_TASK_POLICY_NOT_ACTIVE",
                    message="Политика с учётом задач не находится в активном состоянии",
                    state=result.state,
                    session_id=result.session_id,
                    details=details,
                )
            if result.state == DevStatusKind.STOPPED.value:
                return DevResult(
                    ok=False,
                    code="DEV_TASK_CLEANUP_REQUIRED",
                    message="Состояние планировщика с учётом задач ещё не подтверждено очищенным",
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
        """Сформировать план задач только для чтения будущей сессии с учётом задач."""

        try:
            self._refresh_target()
            _plan, result = self._build_task_plan(root_tasks, excluded_tasks)
        except TaskSandboxError as exc:
            return self._task_error(exc)
        return result

    def task_smoke(
        self,
        *,
        root_tasks: Iterable[str] | str,
        excluded_tasks: Iterable[str] | str | None = None,
        preserve_task_state: bool = False,
    ) -> DevResult:
        """Запустить проверку жизненного цикла Task Sandbox через штатный путь Dev Runtime."""

        steps: list[dict[str, object]] = []
        planned = self.plan(root_tasks=root_tasks, excluded_tasks=excluded_tasks)
        steps.append(planned.as_dict())
        if not planned.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_PLAN_FAILED",
                message="Проверка с учётом задач остановлена на проверке плана",
                state=planned.state,
                details={"steps": steps},
            )
        preflight = self.preflight()
        steps.append(preflight.as_dict())
        if not preflight.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_PREFLIGHT_FAILED",
                message="Проверка с учётом задач остановлена на предварительной проверке",
                state=preflight.state,
                details={"steps": steps},
            )
        started = self.start(root_tasks=root_tasks, excluded_tasks=excluded_tasks)
        steps.append(started.as_dict())
        if not started.ok:
            return DevResult(
                ok=False,
                code="DEV_TASK_SMOKE_START_FAILED",
                message="Проверка с учётом задач не смогла запустить DevSession",
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
                message="Проверка с учётом задач не подтвердила рабочее состояние и владение",
                state=observed.state,
                session_id=started.session_id,
                details={
                    "steps": steps,
                    "preserve_task_state": preserve_task_state,
                    "cleanup_required": preserve_task_state,
                },
            )
        stopped = self.stop(preserve_task_state=preserve_task_state)
        steps.append(stopped.as_dict())
        final_status = self.status()
        steps.append(final_status.as_dict())
        ok = stopped.ok and final_status.state == DevStatusKind.STOPPED.value
        return DevResult(
            ok=ok,
            code="DEV_TASK_SMOKE_PASS" if ok else "DEV_TASK_SMOKE_STOP_FAILED",
            message=(
                "Проверка task sandbox и lifecycle пройдена"
                if ok
                else "Проверка с учётом задач не подтвердила безопасное завершение"
            ),
            state=final_status.state,
            session_id=started.session_id,
            details={
                "steps": steps,
                "preserve_task_state": preserve_task_state,
                "cleanup_required": preserve_task_state,
            },
        )

    def cleanup(self) -> DevResult:
        try:
            self._refresh_target()
            return self._cleanup_impl()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except RuntimeCoordinationError as exc:
            return self._coordination_error(exc)

    def _cleanup_impl(self) -> DevResult:
        """Явно завершить очистку планировщика, не останавливая рабочий процесс."""

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
            try:
                cleanup_environment = (
                    self._environment_for_session(session)
                    if session is not None
                    else self.environment
                )
            except TaskSandboxError as exc:
                return self._task_error(exc)
            evidence_store = (
                self._evidence_for_session(
                    session.session_id,
                    profile_name=self._session_profile_name(session),
                )
                if session is not None and session.is_task_aware
                else None
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
                        cleanup_environment, session.session_id
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
            if session is not None and session.is_task_aware:
                self._evidence_event("cleanup_started", {"preserved": False}, store=evidence_store)
            cleanup = self._cleanup_task_state_locked(
                expected_session_id=session.session_id if session is not None else None,
                session=session,
                environment=cleanup_environment,
            )
            if not cleanup.ok:
                if session is not None:
                    session.state = DevSessionState.FAILED
                    session.updated_at = self._timestamp()
                    session.last_code = "DEV_CLEANUP_FAILED"
                    session.last_message = cleanup.message
                    self._write_session(session)
                    self._evidence_event(
                        "runtime_warning",
                        {"code": "DEV_CLEANUP_FAILED", "phase": "cleanup"},
                        store=evidence_store,
                    )
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
            session.last_message = "Состояние планировщика назначенного development target очищено"
            self._write_session(session)
            self._evidence_event(
                "cleanup_completed",
                {"confirmed": True, "preserved": False},
                store=evidence_store,
            )
            self._evidence_event(
                "session_stopped",
                {"state": DevSessionState.STOPPED.value},
                store=evidence_store,
            )
            if evidence_store is not None:
                try:
                    evidence_store.finalize(
                        stopped_at=session.updated_at,
                        cleanup_confirmed=True,
                    )
                except Exception:
                    evidence_store.mark_degraded("timeline_write_failed")
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
                profile_name=self.environment.profile_name,
            )
            plan = TaskPlan.from_catalog(
                catalog,
                root_tasks,
                excluded_tasks,
                profile_name=self.environment.profile_name,
            )
        except TaskSandboxError as exc:
            return None, self._task_error(exc)
        return plan, DevResult(
            ok=True,
            code="DEV_TASK_PLAN_READY",
            message="План назначенного development target с учётом задач сформирован",
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

    @staticmethod
    def _coordination_error(exc: RuntimeCoordinationError) -> DevResult:
        return DevResult(
            ok=False,
            code=exc.code,
            message=str(exc),
            state=DevStatusKind.FAILED.value,
        )

    def _cleanup_leftover_task_state_locked(self) -> DevResult:
        try:
            session = self._read_session()
            cleanup_environment = (
                self._environment_for_session(session)
                if session is not None
                else self.environment
            )
            store = TaskPolicyStore(cleanup_environment)
            policy = store.read()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except ValueError as exc:
            return self._task_error(
                TaskSandboxError("DEV_STATE_CORRUPT", f"Нельзя проверить leftover state: {exc}")
            )
        task_cleanup_required = session is not None and session.task_cleanup_needed
        if policy is None and not task_cleanup_required:
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_NOT_NEEDED",
                    message="Безопасная оставшаяся политика задач отсутствует",
                state=DevStatusKind.NO_SESSION.value,
                details={"cleanup_confirmed": True},
            )
        expected_session_id = (
            session.session_id
            if session is not None
            and (
                session.task_cleanup_needed
                or session.state not in {DevSessionState.STOPPED, DevSessionState.FAILED}
            )
            else None
        )
        if (
            policy is not None
            and expected_session_id is not None
            and policy.session_id != expected_session_id
        ):
            return self._task_error(
                TaskSandboxError(
                    "DEV_TASK_POLICY_CONTEXT_MISMATCH",
                    "Оставшаяся политика задач не соответствует DevSession",
                )
            )
        return self._cleanup_task_state_locked(
            expected_session_id=expected_session_id,
            session=session,
            environment=cleanup_environment,
        )

    def _prepare_task_session_locked(
        self, task_plan: TaskPlan, session: DevSession
    ) -> DevResult:
        catalog: TaskCatalog | None = None
        mutation_started = False
        try:
            catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
                profile_name=self.environment.profile_name,
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
            # Факт вызова записи уже означает, что изменение могло завершиться
            # до исключения; постоянный маркер задачи записан до этого места.
            mutation_started = True
            write_profile_payload(
                self.environment.profile_file,
                planned_payload,
                repository_root=self.environment.repository_root,
            )
            verified_catalog = TaskCatalog.from_path(
                self.environment.profile_file,
                repository_root=self.environment.repository_root,
                profile_name=self.environment.profile_name,
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
            session.task_phase = DevTaskPhase.PREPARED
            session.updated_at = self._timestamp()
            self._write_session(session)
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
                    "Сохранённая политика задач не прошла повторную проверку",
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
                    session=session,
                )
                if mutation_started
                else DevResult(
                    ok=True,
                    code="DEV_TASK_CLEANUP_NOT_NEEDED",
                    message="Изменение состояния планировщика не начиналось",
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
        session: DevSession | None = None,
        preserve_task_state: bool = False,
        environment: DevEnvironment | None = None,
    ) -> DevResult:
        try:
            cleanup_environment = (
                environment
                or (
                    self._environment_for_session(session)
                    if session is not None
                    else self.environment
                )
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        store = TaskPolicyStore(cleanup_environment)
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
                    "Политика задач не соответствует текущей DevSession",
                )
            )
        task_cleanup_required = session is not None and session.task_cleanup_needed
        if preserve_task_state:
            try:
                marked = store.mark_preserved(timestamp=self._timestamp())
                if session is not None and session.is_task_aware:
                    self._set_task_lifecycle_locked(
                        session,
                        phase=DevTaskPhase.PRESERVED,
                        cleanup_required=True,
                        policy_expected=True,
                    )
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
                "[Dev Runtime] preserve_task_state=True: состояние планировщика назначенного development target оставлено без очистки"
            )
            return DevResult(
                ok=True,
                code="DEV_TASK_STATE_PRESERVED",
                message="Состояние планировщика оставлено по явному preserve_task_state",
                state=DevStatusKind.STOPPED.value,
                details={
                    "cleanup_confirmed": False,
                    "preserved_task_state": True,
                    "policy_marked": marked is not None,
                },
            )
        if policy is None and catalog is None and not task_cleanup_required:
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_NOT_NEEDED",
                message="Политика задач отсутствует; очистка не требуется",
                state=DevStatusKind.STOPPED.value,
                details={"cleanup_confirmed": True, "policy_removed": False},
            )
        try:
            if task_cleanup_required:
                self._set_task_lifecycle_locked(
                    session,
                    phase=DevTaskPhase.CLEANUP_PENDING,
                    cleanup_required=True,
                    policy_expected=True,
                )
            fresh_catalog = TaskCatalog.from_path(
                cleanup_environment.profile_file,
                repository_root=cleanup_environment.repository_root,
                profile_name=cleanup_environment.profile_name,
            )
            payload = read_profile_payload(
                cleanup_environment.profile_file,
                repository_root=cleanup_environment.repository_root,
            )
            cleaned = reset_scheduler_state(payload, fresh_catalog)
            current_state = scheduler_state(payload, fresh_catalog)
            already_clean = all(
                item["enabled"] is False and item["next_run"] == SCHEDULER_RESET_TIME
                for item in current_state.values()
            )
            if not already_clean:
                write_profile_payload(
                    cleanup_environment.profile_file,
                    cleaned,
                    repository_root=cleanup_environment.repository_root,
                )
            verified_catalog = TaskCatalog.from_path(
                cleanup_environment.profile_file,
                repository_root=cleanup_environment.repository_root,
                profile_name=cleanup_environment.profile_name,
            )
            verified_state = scheduler_state(
                read_profile_payload(
                    cleanup_environment.profile_file,
                    repository_root=cleanup_environment.repository_root,
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
                    "Состояние планировщика не подтверждено сброшенным",
                )
            if policy is not None:
                store.remove()
            if session is not None and session.is_task_aware:
                self._set_task_lifecycle_locked(
                    session,
                    phase=DevTaskPhase.CLEAN,
                    cleanup_required=False,
                    policy_expected=False,
                )
            return DevResult(
                ok=True,
                code="DEV_TASK_CLEANUP_COMPLETED",
                message="Состояние планировщика всех доступных ему задач назначенного development target сброшено",
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
            lifecycle_pending = False
            if session is not None and session.is_task_aware:
                try:
                    self._set_task_lifecycle_locked(
                        session,
                        phase=DevTaskPhase.CLEANUP_PENDING,
                        cleanup_required=True,
                        policy_expected=True,
                    )
                    lifecycle_pending = True
                except (OSError, RuntimeError, ValueError):
                    lifecycle_pending = False
            return DevResult(
                ok=False,
                code="DEV_CLEANUP_FAILED",
                message="Очистка состояния планировщика не подтверждена",
                state=DevStatusKind.FAILED.value,
                details={
                    "cleanup_confirmed": False,
                    "policy_marked_cleanup_pending": pending,
                    "lifecycle_marked_cleanup_pending": lifecycle_pending,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
            )

    def _set_task_lifecycle_locked(
        self,
        session: DevSession,
        *,
        phase: DevTaskPhase,
        cleanup_required: bool,
        policy_expected: bool,
    ) -> None:
        if not session.is_task_aware:
            return
        session.task_phase = phase
        session.task_cleanup_required = cleanup_required
        session.task_policy_expected = policy_expected
        session.updated_at = self._timestamp()
        self._write_session(session)

    def _task_cleanup_unconfirmed_locked(
        self,
        *,
        message: str,
        session: DevSession | None = None,
        environment: DevEnvironment | None = None,
    ) -> DevResult:
        """Заблокировать политику, если владение процессом нельзя подтвердить."""

        try:
            cleanup_environment = (
                environment
                or (
                    self._environment_for_session(session)
                    if session is not None
                    else self.environment
                )
            )
        except TaskSandboxError as exc:
            return self._task_error(exc)
        pending = False
        try:
            pending = (
                TaskPolicyStore(cleanup_environment).mark_cleanup_pending(
                    timestamp=self._timestamp()
                )
                is not None
            )
        except (OSError, RuntimeError, ValueError):
            pending = False
        lifecycle_pending = False
        if session is not None and session.is_task_aware:
            try:
                self._set_task_lifecycle_locked(
                    session,
                    phase=DevTaskPhase.CLEANUP_PENDING,
                    cleanup_required=True,
                    policy_expected=True,
                )
                lifecycle_pending = True
            except (OSError, RuntimeError, ValueError):
                lifecycle_pending = False
        return DevResult(
            ok=False,
            code="DEV_CLEANUP_FAILED",
            message=message,
            state=DevStatusKind.FAILED.value,
            details={
                "cleanup_confirmed": False,
                "policy_marked_cleanup_pending": pending,
                "lifecycle_marked_cleanup_pending": lifecycle_pending,
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
        try:
            cleanup_environment = self._environment_for_session(session)
        except TaskSandboxError as exc:
            return self._task_error(exc)
        was_stopped = session.state is DevSessionState.STOPPED and session.process is None
        evidence_store = self._evidence_store
        if evidence_store is None and session.is_task_aware:
            evidence_store = self._evidence_for_session(
                session.session_id,
                profile_name=self._session_profile_name(session),
            )
        if not was_stopped:
            self._evidence_event(
                "process_stopped",
                {
                    "confirmed": code != "DEV_STALE_RECOVERED",
                    "reason": code,
                },
                store=evidence_store,
            )
        if session.is_task_aware and not was_stopped:
            self._evidence_event("cleanup_started", {"preserved": preserve_task_state}, store=evidence_store)
        session.state = DevSessionState.STOPPED
        session.process = None
        cleanup = self._cleanup_task_state_locked(
            expected_session_id=session.session_id,
            session=session,
            preserve_task_state=preserve_task_state,
            environment=cleanup_environment,
        )
        if not cleanup.ok:
            session.state = DevSessionState.FAILED
            session.updated_at = self._timestamp()
            session.last_code = "DEV_CLEANUP_FAILED"
            session.last_message = cleanup.message
            self._write_session(session)
            self._evidence_event(
                "runtime_warning",
                {"code": "DEV_CLEANUP_FAILED", "phase": "cleanup"},
                store=evidence_store,
            )
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
        if session.is_task_aware and not was_stopped:
            self._evidence_event(
                "cleanup_completed",
                {
                    "confirmed": not preserve_task_state,
                    "preserved": preserve_task_state,
                },
                store=evidence_store,
            )
            self._evidence_event(
                "session_stopped",
                {"state": DevSessionState.STOPPED.value},
                store=evidence_store,
            )
            if evidence_store is not None:
                try:
                    evidence_store.finalize(
                        stopped_at=session.updated_at,
                        cleanup_confirmed=not preserve_task_state,
                        preserved=preserve_task_state,
                    )
                except Exception:
                    evidence_store.mark_degraded("timeline_write_failed")
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
        try:
            self._refresh_target()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        task_aware = root_tasks is not None or excluded_tasks is not None
        if not task_aware:
            try:
                return self._start_core()
            except RuntimeCoordinationError as exc:
                return self._coordination_error(exc)
        plan, plan_result = self._build_task_plan(root_tasks, excluded_tasks)
        if plan is None:
            return plan_result
        try:
            return self._start_core(plan)
        except RuntimeCoordinationError as exc:
            return self._coordination_error(exc)

    def _runtime_start_conflict(self) -> DevResult | None:
        """Проверить durable reservations control и Smoke под общей lock."""

        try:
            control_store = ControlStore(self.environment)
            with control_store.lock(create=False):
                control_operation = control_store.read()
            if control_operation is not None and control_operation.active:
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_CONFLICT_CONTROL",
                    message="DevSession запрещена при активной control operation",
                    state=DevStatusKind.FAILED.value,
                    details={"outcome": "CONFLICT"},
                )
            if self._get_smoke_manager().has_active_run():
                return DevResult(
                    ok=False,
                    code="DEV_SESSION_CONFLICT_SMOKE",
                    message="DevSession запрещена при активном SmokeRun",
                    state=DevStatusKind.FAILED.value,
                    details={"outcome": "CONFLICT"},
                )
        except Exception as exc:  # noqa: BLE001 — отсутствие общей картины блокирует новый owner
            return DevResult(
                ok=False,
                code="DEV_RUNTIME_OWNER_STATE_UNAVAILABLE",
                message=f"Нельзя подтвердить отсутствие другого runtime owner: {type(exc).__name__}",
                state=DevStatusKind.FAILED.value,
            )
        return None

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
            conflict = self._runtime_start_conflict()
            if conflict is not None:
                return conflict
            current = self.status()
            if current.state not in {
                DevStatusKind.NO_SESSION.value,
                DevStatusKind.STARTING.value,
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
                DevStatusKind.STARTING.value,
                DevStatusKind.FAILED.value,
                DevStatusKind.STALE.value,
            }:
                recovered = self._recover_locked()
                if not recovered.ok:
                    return recovered

            previous_cleanup = self._cleanup_leftover_task_state_locked()
            if not previous_cleanup.ok:
                return previous_cleanup

            self._evidence_store = None
            timestamp = self._timestamp()
            session = DevSession(
                session_id=self.session_id_factory(),
                state=DevSessionState.CREATED,
                repository_root=str(self.environment.repository_root),
                created_at=timestamp,
                updated_at=timestamp,
                profile_name=self.environment.profile_name,
                target_identity=target_identity(self.environment.dev_target),
                last_code="DEV_SESSION_CREATED",
                last_message="DevSession создана",
                task_mode=(
                    DevTaskMode.TASK_AWARE
                    if task_plan is not None
                    else DevTaskMode.NONE
                ),
                task_phase=(
                    DevTaskPhase.PREPARING
                    if task_plan is not None
                    else DevTaskPhase.NONE
                ),
                task_cleanup_required=task_plan is not None,
                task_policy_expected=task_plan is not None,
            )
            self._write_session(session)
            if task_plan is not None:
                self._initialize_evidence(session, task_plan)
            if task_plan is not None:
                preparation = self._prepare_task_session_locked(task_plan, session)
                if not preparation.ok:
                    self._evidence_event(
                        "runtime_warning",
                        {"code": preparation.code, "phase": "task_prepare"},
                    )
                    session.state = DevSessionState.FAILED
                    session.updated_at = self._timestamp()
                    session.last_code = preparation.code
                    session.last_message = preparation.message
                    self._write_session(session)
                    cleanup_details = preparation.details.get("cleanup")
                    cleanup_confirmed = isinstance(cleanup_details, dict) and cleanup_details.get("ok") is True
                    self._finish_failed_evidence(
                        session,
                        process_started=False,
                        process_stopped=True,
                        cleanup_attempted=True,
                        cleanup_confirmed=cleanup_confirmed,
                        reason=preparation.code,
                    )
                    return self._session_result(
                        session,
                        ok=False,
                        code=preparation.code,
                        message=preparation.message,
                        state=DevStatusKind.FAILED,
                        details=preparation.details,
                    )
                self._evidence_event(
                    "policy_prepared",
                    {"profile": self.environment.profile_name, "state": TASK_POLICY_ACTIVE},
                )
                try:
                    if self._evidence_store is not None:
                        self._evidence_store.capture_log_boundary()
                except EvidenceError:
                    pass
            session.state = DevSessionState.STARTING
            session.updated_at = self._timestamp()
            session.last_code = "DEV_SESSION_STARTING"
            session.last_message = "Запускается штатный gui.py для назначенного development target"
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
                self._evidence_event(
                    "process_started",
                    {"state": DevSessionState.STARTING.value},
                )
            except Exception as exc:
                self._evidence_error(exc, phase="session_start")
                process_cleanup_confirmed = pid is None
                if pid is not None:
                    identity = launched_identity
                    if identity is None:
                        try:
                            identity = self.process_backend.capture(pid)
                        except RuntimeError:
                            identity = None
                    if identity is not None:
                        process_cleanup_confirmed = self.process_backend.force_stop(identity)
                    else:
                        process_cleanup_confirmed = False
                failure_code = "DEV_LAUNCH_FAILED"
                failure_details: dict[str, object] = {}
                if task_plan is not None:
                    task_cleanup = (
                        self._cleanup_task_state_locked(
                            expected_session_id=session.session_id,
                            catalog=task_plan.catalog,
                            session=session,
                        )
                        if process_cleanup_confirmed
                        else self._task_cleanup_unconfirmed_locked(
                            message="После ошибки запуска процесс не удалось безопасно завершить",
                            session=session,
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
                    session.last_message += "; состояние планировщика не подтверждено очищенным"
                self._write_session(session)
                cleanup_confirmed = (
                    task_plan is None
                    or (
                        isinstance(failure_details.get("cleanup"), dict)
                        and failure_details["cleanup"].get("ok") is True
                    )
                )
                self._finish_failed_evidence(
                    session,
                    process_started=pid is not None,
                    process_stopped=process_cleanup_confirmed,
                    cleanup_attempted=task_plan is not None,
                    cleanup_confirmed=cleanup_confirmed and process_cleanup_confirmed,
                    reason=failure_code,
                )
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
                self._evidence_event(
                    "runtime_warning",
                    {"code": "DEV_READINESS_FAILED", "reason": reason, "phase": "readiness"},
                )
                cleanup = self._stop_owned_process(latest.process)
                latest.state = DevSessionState.FAILED
                latest.updated_at = self._timestamp()
                failure_code = "DEV_READINESS_FAILED"
                latest.last_message = f"DevSession не достигла готовности: {reason}"
                if cleanup:
                    latest.process = None
                failure_details: dict[str, object] = {"cleanup_confirmed": cleanup}
                if task_plan is not None:
                    task_cleanup = (
                        self._cleanup_task_state_locked(
                            expected_session_id=latest.session_id,
                            catalog=task_plan.catalog,
                            session=latest,
                        )
                        if cleanup
                        else self._task_cleanup_unconfirmed_locked(
                            message="После сбоя готовности процесс не удалось безопасно завершить",
                            session=latest,
                        )
                    )
                    failure_details["task_cleanup"] = task_cleanup.as_dict()
                    if not task_cleanup.ok:
                        failure_code = "DEV_CLEANUP_FAILED"
                        latest.last_message += "; состояние планировщика не подтверждено очищенным"
                latest.last_code = failure_code
                self._write_session(latest)
                cleanup_confirmed = (
                    cleanup
                    and (
                        task_plan is None
                        or (
                            isinstance(failure_details.get("task_cleanup"), dict)
                            and failure_details["task_cleanup"].get("ok") is True
                        )
                    )
                )
                self._finish_failed_evidence(
                    latest,
                    process_started=True,
                    process_stopped=cleanup,
                    cleanup_attempted=task_plan is not None,
                    cleanup_confirmed=cleanup_confirmed,
                    reason=failure_code,
                )
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
                self._evidence_event(
                    "runtime_warning",
                    {"code": "DEV_OWNERSHIP_LOST", "phase": "readiness"},
                )
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
            if latest.is_task_aware:
                latest.task_phase = DevTaskPhase.RUNNING
            latest.updated_at = self._timestamp()
            latest.last_code = "DEV_SESSION_READY"
            latest.last_message = "Dev-сессия готова"
            self._write_session(latest)
            self._evidence_event(
                "session_ready",
                {
                    "state": DevSessionState.RUNNING.value,
                    "profile": self.environment.profile_name,
                },
            )
            return self._session_result(
                latest,
                ok=True,
                code="DEV_SESSION_READY",
                message="Dev-сессия готова",
                state=DevStatusKind.RUNNING_OWNED,
                details={
                    "host": self.environment.host,
                    "port": self.environment.port,
                    "profile": self.environment.profile_name,
                    "log": str(self.environment.log_file.relative_to(self.environment.repository_root)),
                },
            )

    def stop(self, *, preserve_task_state: bool = False) -> DevResult:
        try:
            self._refresh_target()
            return self._stop_impl(preserve_task_state=preserve_task_state)
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except RuntimeCoordinationError as exc:
            return self._coordination_error(exc)

    def _stop_impl(self, *, preserve_task_state: bool = False) -> DevResult:
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
            self._evidence_event("stop_requested", {"state": DevSessionState.STOPPING.value})

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
            if latest.is_task_aware:
                try:
                    self._set_task_lifecycle_locked(
                        latest,
                        phase=DevTaskPhase.CLEANUP_PENDING,
                        cleanup_required=True,
                        policy_expected=True,
                    )
                except (OSError, RuntimeError, ValueError):
                    pass
            latest.updated_at = self._timestamp()
            latest.last_code = "DEV_STOP_UNCONFIRMED"
            latest.last_message = "Не удалось безопасно подтвердить завершение DevSession"
            self._write_session(latest)
            self._evidence_event(
                "runtime_warning",
                {"code": "DEV_STOP_UNCONFIRMED", "phase": "stop"},
                store=self._evidence_store,
            )
            return self._session_result(
                latest,
                ok=False,
                code="DEV_STOP_UNCONFIRMED",
                message=latest.last_message,
                state=DevStatusKind.STALE,
            )

    def recover(self) -> DevResult:
        try:
            self._refresh_target()
            with self._locked_state():
                return self._recover_locked()
        except TaskSandboxError as exc:
            return self._task_error(exc)
        except RuntimeCoordinationError as exc:
            return self._coordination_error(exc)

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
                "Проверка жизненного цикла Dev Runtime пройдена"
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
        try:
            session_environment = self._environment_for_session(session)
        except TaskSandboxError as exc:
            return self._task_error(exc)
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
                    session_environment, session.session_id
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
            raw = read_bounded_bytes(self.environment.state_file, max_bytes=64 * 1024)
        except FileNotFoundError:
            return None
        except BoundedReadTooLarge as exc:
            raise ValueError("маркер превышает допустимый размер") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
                # Сбой предварительной очистки не должен скрывать исходную ошибку записи.
                pass

    def _raw_state_bytes(self) -> bytes | None:
        try:
            return read_bounded_bytes(self.environment.state_file, max_bytes=64 * 1024)
        except FileNotFoundError:
            return None
        except (BoundedReadTooLarge, OSError):
            return None

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        with runtime_coordination_lock(self.environment):
            self.environment.lock_file.parent.mkdir(parents=True, exist_ok=True)
            with _state_thread_lock, _exclusive_file_lock(self.environment.lock_file):
                yield

    def _timestamp(self) -> str:
        timestamp = self.now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        return timestamp.isoformat()

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
