"""Менеджер жизненного цикла фиксированной локальной DevSession профиля ap."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

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
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout

    def start(self) -> DevResult:
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
            if current.state == DevStatusKind.STALE.value:
                recovered = self._recover_locked()
                if not recovered.ok:
                    return recovered

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
            session.state = DevSessionState.STARTING
            session.updated_at = self._timestamp()
            session.last_code = "DEV_SESSION_STARTING"
            session.last_message = "Запускается штатный gui.py для профиля ap"
            self._write_session(session)

            pid: int | None = None
            try:
                pid = self.process_backend.launch(self.environment, session.session_id)
                identity = self.process_backend.capture(pid)
                if identity is None:
                    raise RuntimeError("Запущенный процесс завершился до фиксации владения")
                session.process = identity
                session.updated_at = self._timestamp()
                self._write_session(session)
            except Exception as exc:
                if pid is not None:
                    try:
                        identity = self.process_backend.capture(pid)
                    except RuntimeError:
                        identity = None
                    if identity is not None:
                        self.process_backend.force_stop(identity)
                session.state = DevSessionState.FAILED
                session.updated_at = self._timestamp()
                session.last_code = "DEV_LAUNCH_FAILED"
                session.last_message = f"Не удалось запустить DevSession: {type(exc).__name__}"
                self._write_session(session)
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_LAUNCH_FAILED",
                    message=session.last_message,
                    state=DevStatusKind.FAILED,
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
            if not ready:
                cleanup = self._stop_owned_process(latest.process)
                latest.state = DevSessionState.FAILED
                latest.updated_at = self._timestamp()
                latest.last_code = "DEV_READINESS_FAILED"
                latest.last_message = f"DevSession не достигла готовности: {reason}"
                if cleanup:
                    latest.process = None
                self._write_session(latest)
                return self._session_result(
                    latest,
                    ok=False,
                    code="DEV_READINESS_FAILED",
                    message=latest.last_message,
                    state=DevStatusKind.FAILED,
                    details={"cleanup_confirmed": cleanup},
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

    def stop(self) -> DevResult:
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
                return self._session_result(
                    session,
                    ok=True,
                    code="DEV_STOP_ALREADY_STOPPED",
                    message="DevSession уже остановлена",
                    state=DevStatusKind.STOPPED,
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
                session.state = DevSessionState.STOPPED
                session.process = None
                session.updated_at = self._timestamp()
                session.last_code = "DEV_STALE_RECOVERED"
                session.last_message = "Завершённый процесс подтверждён; устаревший маркер логически закрыт"
                self._write_session(session)
                return self._session_result(
                    session,
                    ok=True,
                    code="DEV_STALE_RECOVERED",
                    message=session.last_message,
                    state=DevStatusKind.STOPPED,
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
                latest.state = DevSessionState.STOPPED
                latest.process = None
                latest.updated_at = self._timestamp()
                latest.last_code = "DEV_SESSION_STOPPED"
                latest.last_message = "DevSession остановлена и процесс завершён"
                self._write_session(latest)
                return self._session_result(
                    latest,
                    ok=True,
                    code="DEV_SESSION_STOPPED",
                    message=latest.last_message,
                    state=DevStatusKind.STOPPED,
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
            return self._session_result(
                session,
                ok=True,
                code="DEV_RECOVERY_NOT_NEEDED",
                message="DevSession уже находится в безопасном остановленном состоянии",
                state=DevStatusKind.STOPPED,
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
            session.state = DevSessionState.STOPPED
            session.updated_at = self._timestamp()
            session.last_code = "DEV_STALE_RECOVERED"
            session.last_message = "Незавершённый маркер не имеет живого процесса; состояние закрыто без завершения процессов"
            self._write_session(session)
            return self._session_result(
                session,
                ok=True,
                code="DEV_STALE_RECOVERED",
                message=session.last_message,
                state=DevStatusKind.STOPPED,
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
            session.state = DevSessionState.STOPPED
            session.process = None
            session.updated_at = self._timestamp()
            session.last_code = "DEV_STALE_RECOVERED"
            session.last_message = "Устаревший маркер закрыт после подтверждения отсутствия процесса"
            self._write_session(session)
            return self._session_result(
                session,
                ok=True,
                code="DEV_STALE_RECOVERED",
                message=session.last_message,
                state=DevStatusKind.STOPPED,
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
        self.process_backend.request_stop(identity)
        if self.process_backend.wait_exit(identity, self.stop_timeout):
            return True
        try:
            matches = self.process_backend.matches(identity)
        except RuntimeError:
            return False
        if matches is None:
            return True
        if matches is not True:
            return False
        if not self.process_backend.force_stop(identity):
            return False
        return self.process_backend.wait_exit(identity, 5.0)

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
        with _state_thread_lock:
            with _exclusive_file_lock(self.environment.lock_file):
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
