from datetime import datetime, timezone
from pathlib import Path

from module.dev_runtime import (
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionManager,
    DevRuntimeMode,
    DevSessionState,
    DevStatusKind,
    DevTarget,
    ProcessIdentity,
)


class LiveOwnedBackend:
    def __init__(self, identity: ProcessIdentity) -> None:
        self.identity = identity
        self.launch_count = 0
        self.request_stop_count = 0
        self.force_stop_count = 0

    def matches(self, identity: ProcessIdentity) -> bool | None:
        return identity == self.identity

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.launch_count += 1
        return self.identity.pid

    def capture(self, pid: int) -> ProcessIdentity | None:
        return self.identity if pid == self.identity.pid else None

    def request_stop(self, identity: ProcessIdentity) -> bool:
        self.request_stop_count += 1
        return False

    def force_stop(self, identity: ProcessIdentity) -> bool:
        self.force_stop_count += 1
        return False


def test_failed_marker_with_live_owned_process_blocks_second_start(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    identity = ProcessIdentity(
        pid=777,
        created_at=7.0,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py", "--dev-session-id", "failed-live"),
        cwd=str(environment.repository_root),
    )
    backend = LiveOwnedBackend(identity)
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
        session_id_factory=lambda: "unexpected-new-session",
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    manager._write_session(
        DevSession(
            session_id="failed-live",
            state=DevSessionState.FAILED,
            repository_root=str(environment.repository_root),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            process=identity,
            runtime_mode=DevRuntimeMode.STANDALONE_PROCESS,
        )
    )

    preflight = manager.preflight()
    result = manager.start()

    assert preflight.ok is False
    assert "DEV_SESSION_CONFLICT" in preflight.details["blockers"]
    assert result.ok is False
    assert result.code == "DEV_START_PREFLIGHT_FAILED"
    assert backend.launch_count == 0
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0


def _safe_preflight() -> DevResult:
    return DevResult(
        ok=True,
        code="DEV_PREFLIGHT_OK",
        message="synthetic safe preflight",
        state=DevStatusKind.NO_SESSION.value,
    )


def test_failed_live_process_blocks_start_after_stale_preflight(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    identity = ProcessIdentity(
        pid=778,
        created_at=8.0,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py", "--dev-session-id", "failed-race"),
        cwd=str(environment.repository_root),
    )
    backend = LiveOwnedBackend(identity)
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
        session_id_factory=lambda: "unexpected-new-session",
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager._write_session(
        DevSession(
            session_id="failed-race",
            state=DevSessionState.FAILED,
            repository_root=str(environment.repository_root),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            process=identity,
            runtime_mode=DevRuntimeMode.STANDALONE_PROCESS,
        )
    )
    manager.preflight = _safe_preflight

    status = manager.status()
    result = manager.start()

    assert status.state == DevStatusKind.STALE.value
    assert result.ok is False
    assert result.code == "DEV_SESSION_ACTIVE"
    assert backend.launch_count == 0
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0


def test_stopped_marker_with_live_process_is_not_treated_as_safe(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    identity = ProcessIdentity(
        pid=779,
        created_at=9.0,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py", "--dev-session-id", "stopped-live"),
        cwd=str(environment.repository_root),
    )
    backend = LiveOwnedBackend(identity)
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
        session_id_factory=lambda: "unexpected-new-session",
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    manager._write_session(
        DevSession(
            session_id="stopped-live",
            state=DevSessionState.STOPPED,
            repository_root=str(environment.repository_root),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            process=identity,
            runtime_mode=DevRuntimeMode.STANDALONE_PROCESS,
        )
    )

    status = manager.status()
    preflight = manager.preflight()
    manager.preflight = _safe_preflight
    result = manager.start()

    assert status.state == DevStatusKind.STALE.value
    assert preflight.ok is False
    assert "DEV_SESSION_CONFLICT" in preflight.details["blockers"]
    assert result.ok is False
    assert result.code == "DEV_SESSION_ACTIVE"
    assert backend.launch_count == 0
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0
