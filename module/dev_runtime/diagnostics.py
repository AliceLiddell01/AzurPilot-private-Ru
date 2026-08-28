"""Диагностика только для чтения и проверки готовности Dev Runtime."""

from __future__ import annotations

import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from module.config.profile import ProfileDiscoveryError, classify_profile_config
from module.dev_runtime.contracts import (
    DEV_PROFILE,
    DevEnvironment,
    DevResult,
    DevSessionState,
    DevStatusKind,
    ProcessIdentity,
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
        add("profile", profile_ok, "DEV_PROFILE_INVALID", profile_message)

        storage_ok, storage_message = self.storage_probe(self.environment)
        add("storage", storage_ok, "DEV_STORAGE_NOT_READY", storage_message)

        state = self.status()
        try:
            stored_session = self._read_session()
        except (OSError, ValueError):
            stored_session = None
        failed_without_process = (
            state.state == DevStatusKind.FAILED.value
            and stored_session is not None
            and stored_session.process is None
        )
        safely_recoverable_stale = (
            state.state == DevStatusKind.STALE.value
            and state.code == "DEV_SESSION_STALE"
        )
        state_ok = (
            state.state
            in {DevStatusKind.NO_SESSION.value, DevStatusKind.STOPPED.value}
            or failed_without_process
            or safely_recoverable_stale
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
            details={"profile": DEV_PROFILE, "checks": checks, "blockers": blockers},
        )

    def doctor(self) -> DevResult:
        before = self._raw_state_bytes()
        preflight = self.preflight()
        status = self.status()
        after = self._raw_state_bytes()
        read_only = before == after
        ok = preflight.ok and read_only
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

        if session.state is DevSessionState.RUNNING:
            ready, reason = self.readiness_probe(self.environment, identity)
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
            DevSessionState.FAILED: DevStatusKind.STALE,
            DevSessionState.STOPPED: DevStatusKind.STALE,
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

            owner = worker_registry.get_owner_record()
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
            workers = worker_registry.get_workers(owner_pid)
            worker = workers.get(DEV_PROFILE)
            if worker is None:
                return False, "рабочий процесс профиля ap ещё не зарегистрирован"
            if worker_registry.process_matches(worker) is not True:
                return False, "рабочий процесс профиля ap не подтверждён"
        except Exception as exc:
            return False, f"реестр рабочих процессов не готов: {type(exc).__name__}"
        if not _http_ready(environment.host, environment.port):
            return False, "Принадлежащий DevSession WebUI ещё не отвечает через локальный интерфейс"
        return True, "WebUI и рабочий процесс ap готовы, владение подтверждено"

    def _project_python_is_supported(self) -> bool:
        version_ok = (3, 14, 6) <= sys.version_info[:3] < (3, 15, 0)
        venv = self.environment.repository_root / ".venv"
        try:
            python_ok = self.environment.python_executable.resolve().is_relative_to(
                venv.resolve()
            )
        except (OSError, RuntimeError):
            python_ok = False
        return version_ok and python_ok

    def _profile_check(self) -> tuple[bool, str]:
        config_dir = self.environment.repository_root / "config"
        profile_path = config_dir / f"{DEV_PROFILE}.json"
        if not profile_path.exists():
            return False, "Локальный скрытый профиль config/ap.json отсутствует"
        try:
            profile = classify_profile_config(profile_path, config_dir, strict=True)
        except ProfileDiscoveryError as exc:
            return False, f"Профиль ap небезопасен: {exc.code}"
        if profile is None or profile.name != DEV_PROFILE or profile.mod_name != "alas":
            return False, "config/ap.json не соответствует структурному контракту профиля AzurPilot"
        return True, "Локальный профиль ap существует и структурно допустим"

    def _webui_registry_check(self) -> tuple[bool, str]:
        try:
            from module.webui import worker_registry

            owner = worker_registry.get_owner_record()
            if owner is None:
                return True, "Активный владелец WebUI отсутствует"
            matches = worker_registry.process_matches(owner)
            if matches is True:
                return False, "В этой рабочей копии уже работает WebUI; второй владелец запрещён"
            if matches is False:
                return True, "PID старого WebUI переиспользован; новый gui.py выполнит штатную очистку при восстановлении"
            return True, "Предыдущий WebUI завершён; новый gui.py выполнит штатную очистку при восстановлении"
        except Exception as exc:
            return False, f"Нельзя безопасно проверить реестр рабочих процессов: {type(exc).__name__}"


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
        return False, "Проверка готовности PostgreSQL завершилась ошибкой"
    return True, "Готовность PostgreSQL подтверждена"
