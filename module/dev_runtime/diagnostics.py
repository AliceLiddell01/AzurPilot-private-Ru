"""Диагностика только для чтения и проверки готовности Dev Runtime."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from module.config.profile import ProfileDiscoveryError, classify_profile_config
from module.dev_runtime.bounded_io import BoundedReadTooLarge, read_bounded_bytes
from module.dev_runtime.contracts import (
    DevEnvironment,
    DevResult,
    DevSessionState,
    DevStatusKind,
    ProcessIdentity,
)

_REGISTRY_MAX_BYTES = 1024 * 1024
_TASK_CLEANUP_RECOVERABLE_CODES = frozenset(
    {
        "DEV_TASK_POLICY_MISSING",
        "DEV_TASK_POLICY_NOT_ACTIVE",
        "DEV_TASK_STATE_PRESERVED",
        "DEV_TASK_CLEANUP_REQUIRED",
        "DEV_SESSION_RECOVERY_REQUIRED",
    }
)


class DevDiagnosticsMixin:
    def preflight(self) -> DevResult:
        checks: list[dict[str, object]] = []
        blockers: list[str] = []

        def add(name: str, ok: bool, code: str, message: str) -> None:
            checks.append({"name": name, "ok": ok, "code": code, "message": message})
            if not ok:
                blockers.append(code)

        root = self.environment.repository_root
        add(
            "repository",
            (root / "gui.py").is_file() and (root / "module").is_dir(),
            "DEV_REPOSITORY_INVALID",
            "Корень репозитория и gui.py найдены"
            if (root / "gui.py").is_file() and (root / "module").is_dir()
            else "Текущий каталог не похож на рабочую копию AzurPilot",
        )

        python_ok = self._project_python_is_supported()
        add(
            "python",
            python_ok,
            "DEV_PYTHON_UNSUPPORTED",
            "Используется поддерживаемый проектный Python"
            if python_ok
            else "Dev Runtime должен запускаться поддерживаемым Python из .venv этой рабочей копии",
        )

        profile_ok, profile_message = self._profile_check()
        add("development_target", profile_ok, "DEV_TARGET_INVALID", profile_message)

        storage_ok, storage_message = self.storage_probe(self.environment)
        add("storage", storage_ok, "DEV_STORAGE_NOT_READY", storage_message)

        dependency_sync_pending = _dependency_sync_pending(self.environment)
        add(
            "dependency_sync",
            not dependency_sync_pending,
            "DEV_DEPENDENCY_SYNC_PENDING",
            "Ожидающая синхронизация зависимостей отсутствует"
            if not dependency_sync_pending
            else (
                "Обнаружена ожидающая синхронизация зависимостей; Dev Runtime не запускает "
                "uv sync и требует заранее подготовленное окружение"
            ),
        )

        state = self.status()
        try:
            stored_session = self._read_session()
        except (OSError, ValueError):
            stored_session = None
        task_cleanup_recoverable = (
            stored_session is not None
            and stored_session.task_cleanup_needed
            and state.state
            in {
                DevStatusKind.STARTING.value,
                DevStatusKind.STOPPED.value,
                DevStatusKind.FAILED.value,
                DevStatusKind.STALE.value,
            }
            and state.code in _TASK_CLEANUP_RECOVERABLE_CODES
        )
        task_state_invalid = (
            state.code.startswith("DEV_TASK_POLICY_")
            or state.code.startswith("DEV_TASK_STATE_")
            or state.code
            in {"DEV_TASK_STATE_PRESERVED", "DEV_TASK_CLEANUP_REQUIRED"}
        )
        if task_state_invalid and not task_cleanup_recoverable:
            add(
                "task_policy",
                False,
                state.code,
                "Task policy или task lifecycle нельзя безопасно подтвердить",
            )
        failed_without_process = (
            state.state == DevStatusKind.FAILED.value
            and stored_session is not None
            and stored_session.process is None
            and not task_state_invalid
        )
        safely_recoverable_stale = (
            state.state == DevStatusKind.STALE.value
            and state.code == "DEV_SESSION_STALE"
        )
        state_ok = (
            state.ok
            and state.state
            in {DevStatusKind.NO_SESSION.value, DevStatusKind.STOPPED.value}
            or failed_without_process
            or safely_recoverable_stale
            or task_cleanup_recoverable
        )
        add(
            "session",
            state_ok,
            "DEV_SESSION_CONFLICT",
            "Нет активной конфликтующей DevSession"
            if state_ok
            else f"Старт заблокирован текущим состоянием DevSession: {state.state}",
        )

        registry_ok, registry_message = self._webui_registry_check()
        add("webui_registry", registry_ok, "DEV_WEBUI_CONFLICT", registry_message)

        port_busy = self.port_probe(self.environment.host, self.environment.port)
        add(
            "port",
            not port_busy,
            "DEV_PORT_IN_USE",
            f"Порт Dev WebUI {self.environment.port} свободен"
            if not port_busy
            else f"Порт Dev WebUI {self.environment.port} уже занят; владение не подтверждено",
        )

        ok = not blockers
        return DevResult(
            ok=ok,
            code="DEV_PREFLIGHT_OK" if ok else "DEV_PREFLIGHT_BLOCKED",
            message=(
                "Dev Runtime готов к безопасному запуску"
                if ok
                else "Dev Runtime не готов к безопасному запуску"
            ),
            state=(
                DevStatusKind.NO_SESSION.value if ok else DevStatusKind.FAILED.value
            ),
            details={
                "development_target": {"configured": profile_ok},
                "checks": checks,
                "blockers": blockers,
            },
        )

    def doctor(self) -> DevResult:
        before = self._raw_state_bytes()
        preflight = self.preflight()
        status = self.status()
        after = self._raw_state_bytes()
        read_only = before == after
        ok = preflight.ok and status.ok and read_only
        return DevResult(
            ok=ok,
            code="DEV_DOCTOR_OK" if ok else "DEV_DOCTOR_ISSUES",
            message=(
                "Диагностика Dev Runtime не обнаружила блокирующих проблем"
                if ok
                else "Диагностика Dev Runtime обнаружила проблемы"
            ),
            state=status.state,
            session_id=status.session_id,
            details={
                "preflight": preflight.as_dict(),
                "status": status.as_dict(),
                "read_only": read_only,
            },
        )

    def status(self) -> DevResult:
        try:
            session = self._read_session()
        except ValueError as exc:
            return DevResult(
                ok=False,
                code="DEV_STATE_CORRUPT",
                message=f"Маркер DevSession повреждён: {exc}",
                state=DevStatusKind.CORRUPT.value,
            )
        except OSError as exc:
            return DevResult(
                ok=False,
                code="DEV_STATE_UNREADABLE",
                message=f"Маркер DevSession невозможно прочитать: {exc}",
                state=DevStatusKind.FAILED.value,
            )

        if session is None:
            return DevResult(
                ok=True,
                code="DEV_NO_SESSION",
                message="DevSession отсутствует",
                state=DevStatusKind.NO_SESSION.value,
            )

        if session.state is DevSessionState.STOPPED and session.process is None:
            return self._session_result(
                session,
                ok=True,
                code="DEV_SESSION_STOPPED",
                message="DevSession остановлена",
                state=DevStatusKind.STOPPED,
            )
        if session.state is DevSessionState.FAILED and session.process is None:
            return self._session_result(
                session,
                ok=False,
                code=session.last_code or "DEV_SESSION_FAILED",
                message=session.last_message or "DevSession завершилась с ошибкой",
                state=DevStatusKind.FAILED,
            )

        identity = session.process
        if identity is None:
            return self._session_result(
                session,
                ok=False,
                code="DEV_SESSION_RECOVERY_REQUIRED",
                message="Маркер не содержит подтверждённую идентичность процесса",
                state=DevStatusKind.STALE,
            )
        try:
            matches = self.process_backend.matches(identity)
        except RuntimeError as exc:
            return self._session_result(
                session,
                ok=False,
                code="DEV_OWNERSHIP_UNKNOWN",
                message=str(exc),
                state=DevStatusKind.OWNERSHIP_MISMATCH,
            )
        if matches is None:
            return self._session_result(
                session,
                ok=False,
                code="DEV_SESSION_STALE",
                message="Маркер существует, но процесс DevSession уже завершён",
                state=DevStatusKind.STALE,
            )
        if matches is False:
            return self._session_result(
                session,
                ok=False,
                code="DEV_OWNERSHIP_MISMATCH",
                message="PID из маркера принадлежит другому процессу; разрушительная очистка запрещена",
                state=DevStatusKind.OWNERSHIP_MISMATCH,
            )

        if session.state in {DevSessionState.FAILED, DevSessionState.STOPPED}:
            code = (
                "DEV_FAILED_PROCESS_STILL_RUNNING"
                if session.state is DevSessionState.FAILED
                else "DEV_STOPPED_PROCESS_STILL_RUNNING"
            )
            return self._session_result(
                session,
                ok=False,
                code=code,
                message=(
                    f"DevSession помечена как {session.state.value}, но принадлежащий ей "
                    "процесс всё ещё работает; повторный старт запрещён до безопасной остановки"
                ),
                state=DevStatusKind.STALE,
            )

        if session.state is DevSessionState.RUNNING:
            session_environment = self.environment
            environment_for_session = getattr(self, "_environment_for_session", None)
            if callable(environment_for_session):
                session_environment = environment_for_session(session)
            ready, reason = self.readiness_probe(session_environment, identity)
            if not ready:
                return self._session_result(
                    session,
                    ok=False,
                    code="DEV_SESSION_DEGRADED",
                    message=f"Корневой процесс принадлежит DevSession, но готовность контура выполнения не подтверждена: {reason}",
                    state=DevStatusKind.STALE,
                )

        kind = {
            DevSessionState.CREATED: DevStatusKind.STARTING,
            DevSessionState.STARTING: DevStatusKind.STARTING,
            DevSessionState.RUNNING: DevStatusKind.RUNNING_OWNED,
            DevSessionState.STOPPING: DevStatusKind.STOPPING,
            DevSessionState.STALE: DevStatusKind.STALE,
        }.get(session.state, DevStatusKind.FAILED)
        return self._session_result(
            session,
            ok=session.state is DevSessionState.RUNNING,
            code=(
                "DEV_SESSION_RUNNING"
                if session.state is DevSessionState.RUNNING
                else session.last_code or "DEV_SESSION_NOT_READY"
            ),
            message=(
                "DevSession запущена, владение подтверждено"
                if session.state is DevSessionState.RUNNING
                else "Процесс DevSession существует, но жизненный цикл ещё не завершён"
            ),
            state=kind,
        )

    def _default_readiness_probe(
        self, environment: DevEnvironment, identity: ProcessIdentity
    ) -> tuple[bool, str]:
        try:
            from module.webui import worker_registry

            owner, workers = _read_worker_registry_snapshot(environment)
            if owner is None:
                return False, "WebUI ещё не зарегистрировала владельца"
            owner_pid = int(owner["pid"])
            owner_matches = worker_registry.process_matches(owner)
            if owner_matches is not True:
                return False, "владелец WebUI не подтверждён"
            if not self.process_backend.is_descendant(owner_pid, identity):
                return False, "владелец WebUI не принадлежит дереву DevSession"
            if not self.process_backend.listens_on(
                owner_pid, environment.host, environment.port
            ):
                return False, "локальный порт не принадлежит подтверждённому владельцу WebUI"
            worker = workers.get(environment.profile_name)
            if worker is None:
                return False, "рабочий процесс development target ещё не зарегистрирован"
            if worker_registry.process_matches(worker) is not True:
                return False, "рабочий процесс development target не подтверждён"
            worker_pid = int(worker["pid"])
            if not self.process_backend.is_descendant(worker_pid, identity):
                return False, "рабочий процесс назначенного development target не принадлежит дереву DevSession"
        except Exception as exc:
            return False, f"реестр рабочих процессов не готов: {type(exc).__name__}"
        if not _http_ready(environment.host, environment.port):
            return False, "Принадлежащий DevSession WebUI ещё не отвечает через локальный интерфейс"
        return True, "WebUI и рабочий процесс development target готовы, владение подтверждено"

    def _project_python_is_supported(self) -> bool:
        version_ok = (3, 14, 6) <= sys.version_info[:3] < (3, 15, 0)
        try:
            venv = Path(os.path.abspath(self.environment.repository_root / ".venv"))
            executable = Path(os.path.abspath(self.environment.python_executable))
            current = Path(os.path.abspath(sys.executable))
            python_ok = executable.is_relative_to(venv)
            python_ok = python_ok and current.is_relative_to(venv)
            python_ok = python_ok and os.path.normcase(str(executable)) == os.path.normcase(
                str(current)
            )
        except (OSError, RuntimeError, ValueError):
            python_ok = False
        return version_ok and python_ok

    def _profile_check(self) -> tuple[bool, str]:
        config_dir = self.environment.repository_root / "config"
        profile_path = self.environment.profile_file
        if not profile_path.exists():
            return False, "Назначенный development target отсутствует"
        try:
            profile = classify_profile_config(profile_path, config_dir, strict=True)
        except ProfileDiscoveryError as exc:
            return False, f"Development target небезопасен: {exc.code}"
        if (
            profile is None
            or profile.name != self.environment.profile_name
            or profile.mod_name != "alas"
        ):
            return False, "Назначенный development target не соответствует структурному контракту AzurPilot"
        return True, "Назначенный development target существует и структурно допустим"

    def _webui_registry_check(self) -> tuple[bool, str]:
        try:
            from module.webui import worker_registry

            owner, workers = _read_worker_registry_snapshot(self.environment)
            if owner is not None:
                matches = worker_registry.process_matches(owner)
                if matches is True:
                    return False, "В этой рабочей копии уже работает WebUI; второй владелец запрещён"
                if matches is False:
                    owner_message = "PID старого WebUI переиспользован"
                else:
                    owner_message = "Предыдущий WebUI завершён"
            else:
                owner_message = "Активный владелец WebUI отсутствует"

            for name, record in workers.items():
                worker_matches = worker_registry.process_matches(record)
                if worker_matches is True:
                    return (
                        False,
                        f"После прежнего WebUI всё ещё работает worker {name}; Dev Runtime не будет завершать чужой процесс",
                    )

            if owner is None and not workers:
                return True, owner_message
            return (
                True,
                f"{owner_message}; остались только безопасно устаревшие записи, штатный gui.py может их очистить",
            )
        except Exception as exc:
            return False, f"Нельзя безопасно проверить реестр рабочих процессов: {type(exc).__name__}"


def _dependency_sync_pending(environment: DevEnvironment) -> bool:
    marker = environment.repository_root / "config" / "webui-dependency-sync-pending"
    try:
        return marker.exists() or marker.is_symlink()
    except OSError:
        return True


def _read_worker_registry_snapshot(
    environment: DevEnvironment,
) -> tuple[dict[str, object] | None, dict[str, dict[str, object]]]:
    """Прочитать worker registry без блокировок, миграции и любых записей."""

    current = environment.repository_root / "cache" / "webui-workers.json"
    legacy = environment.repository_root / "config" / "webui-workers.json"
    current_payload = _read_registry_file(current)
    legacy_payload = _read_registry_file(legacy)

    if current_payload is not None and legacy_payload is not None:
        if current_payload != legacy_payload:
            raise RuntimeError("Новый и legacy worker registry конфликтуют")
        payload = current_payload
    else:
        payload = current_payload if current_payload is not None else legacy_payload

    if payload is None:
        return None, {}
    return _validate_registry_payload(payload)


def _read_registry_file(path: Path) -> object | None:
    try:
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise RuntimeError("worker registry не должен быть ссылкой или junction")
        raw = read_bounded_bytes(path, max_bytes=_REGISTRY_MAX_BYTES)
    except FileNotFoundError:
        return None
    except BoundedReadTooLarge as exc:
        raise RuntimeError("worker registry превышает допустимый размер") from exc
    except OSError as exc:
        raise RuntimeError("worker registry невозможно безопасно прочитать") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("worker registry содержит некорректный JSON") from exc


def _validate_registry_payload(
    payload: object,
) -> tuple[dict[str, object] | None, dict[str, dict[str, object]]]:
    if not isinstance(payload, dict):
        raise RuntimeError("worker registry должен быть объектом")
    workers_payload = payload.get("workers")
    if not isinstance(workers_payload, dict):
        raise RuntimeError("worker registry содержит некорректный workers")

    owner_pid = payload.get("owner_pid")
    owner_created_at = payload.get("owner_created_at")
    owner: dict[str, object] | None
    if owner_pid is None:
        if owner_created_at is not None:
            raise RuntimeError("worker registry содержит owner_created_at без owner_pid")
        owner = None
    else:
        owner = _validated_process_record(
            {"pid": owner_pid, "created_at": owner_created_at},
            label="owner",
        )

    workers: dict[str, dict[str, object]] = {}
    for name, record in workers_payload.items():
        if not isinstance(name, str) or not name:
            raise RuntimeError("worker registry содержит некорректное имя worker")
        workers[name] = _validated_process_record(record, label=f"worker {name}")
    return owner, workers


def _validated_process_record(record: object, *, label: str) -> dict[str, object]:
    if not isinstance(record, dict):
        raise RuntimeError(f"worker registry содержит некорректную запись {label}")
    try:
        pid = int(record["pid"])
        created_at = float(record["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"worker registry содержит неполную запись {label}") from exc
    if pid <= 0 or created_at <= 0:
        raise RuntimeError(f"worker registry содержит недопустимую запись {label}")
    return {"pid": pid, "created_at": created_at}


def _port_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _http_ready(host: str, port: int) -> bool:
    request = urllib.request.Request(
        f"http://{host}:{port}/",
        method="GET",
        headers={"User-Agent": "AzurPilot-DevRuntime/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.75) as response:
            return 100 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return 100 <= exc.code < 500
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _default_storage_probe(environment: DevEnvironment) -> tuple[bool, str]:
    marker = environment.repository_root / "config" / "state" / "storage_backend.json"
    if not marker.is_file() or marker.is_symlink():
        return False, "Канонический маркер PostgreSQL config/state/storage_backend.json не готов"
    probe = (
        "from pathlib import Path; "
        "from module.persistence.config import DatabaseSettings; "
        "from module.persistence.database import LazyEngine, StorageHealthChecker; "
        "from module.persistence.local_environment import read_local_postgres_environment; "
        "marker=Path('config/state/storage_backend.json'); "
        "settings=DatabaseSettings.from_backend_marker(marker); "
        "local_env=read_local_postgres_environment(Path('.env')); "
        "local_env is None or local_env.require_app_runtime_match(settings); "
        "local_env is None or local_env.install(role='app'); "
        "engine=LazyEngine(settings); "
        "StorageHealthChecker(engine).require_ready(); "
        "engine.dispose()"
    )
    command = [
        str(environment.python_executable),
        "-c",
        probe,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(environment.repository_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Проверка готовности PostgreSQL не выполнена: {type(exc).__name__}"
    if completed.returncode != 0:
        return (
            False,
            "Проверка готовности PostgreSQL завершилась ошибкой "
            f"(код возврата {completed.returncode})",
        )
    return True, "Готовность PostgreSQL подтверждена"
