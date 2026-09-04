from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from module.application.runtime_control import (
    RuntimeControlOperation,
    RuntimeControlResult,
    RuntimeOwnerIdentity,
)
from module.application.runtime_state import RuntimeStateStore
from module.dev_runtime import (
    DevEnvironment,
    DevSessionManager,
    DevSessionState,
    DevTarget,
    ProcessIdentity,
)
from module.dev_runtime.shared_webui import SharedWebUIRuntime


class OwnerProcessBackend:
    def __init__(self, identity: ProcessIdentity) -> None:
        self.identity = identity

    def capture(self, pid: int) -> ProcessIdentity | None:
        return self.identity if pid == self.identity.pid else None


class SharedLifecycle:
    def __init__(self, root: Path) -> None:
        self.active = False
        self.session_id: str | None = None
        self.owner = RuntimeOwnerIdentity(pid=7001, created_at=8001.0)
        self.log_file = root / "log" / "ap.txt"

    def start_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        self.active = True
        self.session_id = session_id
        return RuntimeControlResult(
            True,
            "RUNTIME_STARTED",
            "Профиль запущен",
            RuntimeControlOperation.START_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=self.owner,
        )

    def stop_profile(self, *, session_id: str, idempotency_key: str | None = None) -> RuntimeControlResult:
        if session_id != self.session_id:
            return RuntimeControlResult(
                False,
                "RUNTIME_OWNERSHIP_MISMATCH",
                "Сессия не владеет профилем",
                RuntimeControlOperation.STOP_PROFILE,
                "ap",
                session_id,
                idempotency_key or session_id,
                owner=self.owner,
            )
        self.active = False
        return RuntimeControlResult(
            True,
            "RUNTIME_STOPPED",
            "Профиль остановлен",
            RuntimeControlOperation.STOP_PROFILE,
            "ap",
            session_id,
            idempotency_key or session_id,
            owner=self.owner,
        )

    def owner_identity(self) -> RuntimeOwnerIdentity:
        return self.owner

    def matches_session(self, session_id: str, profile: str = "ap") -> bool:
        return self.active and profile == "ap" and session_id == self.session_id

    def worker_present(self, profile: str = "ap") -> bool:
        return self.active and profile == "ap"

    def ready(self, profile: str = "ap", session_id: str | None = None) -> tuple[bool, str]:
        return self.active and profile == "ap" and (session_id is None or session_id == self.session_id), "shared ready"


def _manager(tmp_path: Path) -> tuple[DevSessionManager, SharedLifecycle]:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# синтетический gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    shared = SharedLifecycle(root)
    identity = ProcessIdentity(
        pid=shared.owner.pid,
        created_at=shared.owner.created_at,
        executable=str(environment.python_executable),
        command_line=("gui.py",),
        cwd=str(root),
    )
    manager = DevSessionManager(
        environment,
        process_backend=OwnerProcessBackend(identity),
        shared_webui=True,
        shared_lifecycle=shared,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: True,
        session_id_factory=lambda: "shared-session",
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "shared owner ready")
    return manager, shared


def test_dev_runtime_uses_existing_shared_webui_and_never_owns_server(tmp_path: Path) -> None:
    manager, shared = _manager(tmp_path)

    started = manager.start()
    assert started.ok is True
    assert started.state == "running_owned"
    assert started.details["runtime_mode"] == "shared_webui"
    assert manager.status().ok is True
    assert manager.status().details["runtime_mode"] == "shared_webui"
    assert shared.active is True

    stopped = manager.stop()
    assert stopped.ok is True
    assert stopped.state == "stopped"
    assert shared.active is False
    assert not (tmp_path / "config" / "state" / "dev-runtime-gui.log").exists()


def test_shared_runtime_runs_pre_execution_hook_before_owner_start(
    tmp_path: Path,
) -> None:
    manager, shared = _manager(tmp_path)
    events: list[str] = []
    original_start = shared.start_profile

    def record_start(
        *, session_id: str, idempotency_key: str | None = None
    ) -> RuntimeControlResult:
        events.append("start")
        return original_start(session_id=session_id, idempotency_key=idempotency_key)

    shared.start_profile = record_start  # type: ignore[method-assign]

    started = manager.start_with_pre_execution_hook(
        before_process_launch=lambda _session_id: events.append("hook"),
    )

    assert started.ok is True
    assert events == ["hook", "start"]
    assert shared.active is True
    assert manager.stop().ok is True


def test_shared_runtime_requires_worker_registry_identity_to_match_runtime_state(
    tmp_path: Path,
) -> None:
    shared = SharedWebUIRuntime(tmp_path)
    owner = RuntimeOwnerIdentity(pid=7001, created_at=8001.0)
    record = {"pid": 7010, "created_at": 8010.0}
    shared._owner_reader = lambda: owner  # type: ignore[method-assign]
    shared._owner_matches = lambda _owner: True  # type: ignore[method-assign]
    shared._worker_record = lambda _profile: record  # type: ignore[method-assign]
    shared._process_matches = lambda _record: True  # type: ignore[method-assign]
    RuntimeStateStore(tmp_path).mark_resource_ready(
        "ap",
        worker_pid=7010,
        worker_created_at=8010.0,
        operation_id="operation-1",
        session_id="session-1",
    )

    assert shared.matches_session("session-1", "ap") is True
    record["created_at"] = 8011.0
    assert shared.matches_session("session-1", "ap") is False


def test_shared_recovery_does_not_close_marker_while_worker_is_present(tmp_path: Path) -> None:
    manager, shared = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True

    session = manager._read_session()
    assert session is not None
    session.process = None
    session.state = DevSessionState.FAILED
    manager._write_session(session)

    recovered = manager.recover()

    assert recovered.ok is False
    assert recovered.code == "DEV_OWNERSHIP_MISMATCH"
    assert shared.active is True
    preserved = manager._read_session()
    assert preserved is not None
    assert preserved.state is DevSessionState.FAILED
    assert preserved.process is None


def test_shared_runtime_log_file_translates_target_registry_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.dev_runtime import target as target_module

    def fail(_root: Path) -> object:
        raise target_module.DevTargetError(
            "DEV_TARGET_INVALID", "синтетическая ошибка registry target"
        )

    monkeypatch.setattr(target_module.DevTargetRegistry, "load", fail)

    with pytest.raises(RuntimeError, match="log target"):
        _ = SharedWebUIRuntime(tmp_path).log_file


def test_shared_manager_falls_back_to_environment_log_for_outside_target(
    tmp_path: Path,
) -> None:
    manager, shared = _manager(tmp_path)
    shared.log_file = tmp_path.parent / "outside.log"

    assert manager._evidence_log_path() == manager.environment.log_file
