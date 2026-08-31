from __future__ import annotations

import inspect
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from module.dev_runtime import (
    DEV_HOST,
    DEV_PORT,
    DevTarget,
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionManager,
    DevSessionState,
    DevTaskMode,
    DevTaskPhase,
    EvidenceStore,
    DevStatusKind,
    ProcessBackend,
    ProcessIdentity,
)
from module.dev_runtime import diagnostics as runtime_module


class FakeProcessBackend:
    def __init__(self) -> None:
        self.alive = False
        self.mismatch = False
        self.identity: ProcessIdentity | None = None
        self.launch_count = 0
        self.request_stop_count = 0
        self.force_stop_count = 0
        self.fail_launch = False
        self.candidates: tuple[ProcessIdentity, ...] = ()

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.launch_count += 1
        if self.fail_launch:
            raise OSError("synthetic launch failure")
        self.alive = True
        self.identity = ProcessIdentity(
            pid=42000 + self.launch_count,
            created_at=1000.0 + self.launch_count,
            executable=str(environment.python_executable),
            command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
            cwd=str(environment.repository_root),
        )
        return self.identity.pid

    def capture(self, pid: int) -> ProcessIdentity | None:
        if self.identity is None or self.identity.pid != pid or not self.alive:
            return None
        return self.identity

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if self.mismatch:
            return False
        if not self.alive:
            return None
        return self.identity == identity

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return self.candidates

    def is_descendant(self, child_pid: int, parent: ProcessIdentity) -> bool:
        return self.matches(parent) is True

    def listens_on(self, pid: int, host: str, port: int) -> bool:
        return self.alive and not self.mismatch

    def request_stop(self, identity: ProcessIdentity) -> bool:
        self.request_stop_count += 1
        if self.matches(identity) is not True:
            return False
        self.alive = False
        return True

    def wait_exit(self, identity: ProcessIdentity, timeout: float) -> bool:
        return not self.alive

    def force_stop(self, identity: ProcessIdentity) -> bool:
        self.force_stop_count += 1
        if self.matches(identity) is not True:
            return False
        self.alive = False
        return True


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    return DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )


def _manager(
    tmp_path: Path,
    *,
    backend: FakeProcessBackend | None = None,
    readiness=None,
    port_probe=None,
    session_ids: list[str] | None = None,
    ready_timeout: float = 0.05,
) -> tuple[DevSessionManager, FakeProcessBackend]:
    environment = _environment(tmp_path)
    process_backend = backend or FakeProcessBackend()
    ids = iter(session_ids or ["session-1", "session-2", "session-3"])
    manager = DevSessionManager(
        environment,
        process_backend=process_backend,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=port_probe or (lambda _host, _port: False),
        readiness_probe=readiness or (lambda _environment, _identity: (True, "ready")),
        session_id_factory=lambda: next(ids),
        ready_timeout=ready_timeout,
        stop_timeout=0.01,
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    return manager, process_backend


def _session(
    environment: DevEnvironment,
    *,
    state: DevSessionState,
    process: ProcessIdentity | None = None,
    session_id: str = "session-existing",
) -> DevSession:
    return DevSession(
        session_id=session_id,
        state=state,
        repository_root=str(environment.repository_root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
        process=process,
    )


def test_public_runtime_uses_configured_target_and_loopback_only(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    assert environment.profile_name == "ap"
    assert "profile" not in inspect.signature(DevSessionManager.start).parameters
    assert "profile" not in inspect.signature(DevSessionManager.stop).parameters
    command = ProcessBackend.expected_command(environment, "session-token")
    assert command[-2:] == ["--run", "ap"]
    assert command[2:4] == ["--dev-session-id", "session-token"]
    assert command[command.index("--host") + 1] == "127.0.0.1"

    with pytest.raises(ValueError):
        DevEnvironment(environment.repository_root, environment.python_executable, host="0.0.0.0")
    with pytest.raises(ValueError):
        DevEnvironment(environment.repository_root, environment.python_executable, port=DEV_PORT + 1)


def test_evidence_hooks_rebind_cached_store_to_current_session(tmp_path: Path) -> None:
    manager, _backend = _manager(tmp_path)
    session_a = EvidenceStore.create(
        manager.environment,
        session_id="session-a",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp="2026-08-29T00:00:00+00:00",
    )
    session_b = EvidenceStore.create(
        manager.environment,
        session_id="session-b",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp="2026-08-29T00:00:00+00:00",
    )
    current = _session(
        manager.environment,
        state=DevSessionState.CREATED,
        session_id="session-b",
    )
    current.task_mode = DevTaskMode.TASK_AWARE
    current.task_phase = DevTaskPhase.PREPARING
    current.task_cleanup_required = True
    current.task_policy_expected = True
    manager._write_session(current)
    manager._evidence_store = session_a

    manager._evidence_event("runtime_warning", {"code": "SESSION_B_EVENT"})
    manager._evidence_error(ValueError("SESSION_B_ERROR"), phase="test")

    assert session_a.timeline_page(limit=10)["events"] == []
    events = session_b.timeline_page(limit=10)["events"]
    assert [event["type"] for event in events] == ["runtime_warning", "runtime_error"]


def test_profile_check_accepts_only_structural_local_ap(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    config_dir = environment.repository_root / "config"
    config_dir.mkdir()
    (config_dir / "ap.json").write_text(
        json.dumps(
            {
                "Alas": {"Emulator": {}},
                "General": {},
                "SyntheticTask": {"Scheduler": {}},
            }
        ),
        encoding="utf-8",
    )
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )

    assert manager._profile_check()[0] is True
    assert "development target" in manager._profile_check()[1]


def test_preflight_reports_invalid_target_as_unconfigured(tmp_path: Path) -> None:
    manager, _backend = _manager(tmp_path)
    manager._profile_check = lambda: (False, "target invalid")

    result = manager.preflight()

    assert result.ok is False
    assert result.details["development_target"]["configured"] is False
    assert "DEV_TARGET_INVALID" in result.details["blockers"]


def test_start_persists_created_starting_running_transitions(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    states: list[DevSessionState] = []
    original_write = manager._write_session

    def recording_write(session: DevSession) -> None:
        states.append(session.state)
        original_write(session)

    manager._write_session = recording_write
    result = manager.start()

    assert result.ok is True
    assert result.code == "DEV_SESSION_READY"
    distinct_states = [state for index, state in enumerate(states) if index == 0 or state != states[index - 1]]
    assert distinct_states[:3] == [
        DevSessionState.CREATED,
        DevSessionState.STARTING,
        DevSessionState.RUNNING,
    ]
    assert backend.launch_count == 1


def test_session_ids_are_unique_across_sequential_sessions(tmp_path: Path) -> None:
    manager, _backend = _manager(
        tmp_path,
        session_ids=["session-a", "session-b"],
    )

    first = manager.start()
    assert manager.stop().ok is True
    second = manager.start()

    assert first.session_id == "session-a"
    assert second.session_id == "session-b"
    assert first.session_id != second.session_id


def test_structured_result_has_stable_machine_fields() -> None:
    result = DevResult(
        ok=False,
        code="DEV_TEST_ERROR",
        message="Тестовая ошибка",
        state=DevStatusKind.FAILED.value,
        session_id="session-x",
        details={"reason": "synthetic"},
    )

    assert result.as_dict() == {
        "ok": False,
        "code": "DEV_TEST_ERROR",
        "message": "Тестовая ошибка",
        "state": "failed",
        "session_id": "session-x",
        "details": {"reason": "synthetic"},
    }


def test_atomic_state_roundtrip_and_corrupt_marker_is_fail_closed(tmp_path: Path) -> None:
    manager, _backend = _manager(tmp_path)
    session = _session(manager.environment, state=DevSessionState.STOPPED)

    manager._write_session(session)
    payload = json.loads(manager.environment.state_file.read_text(encoding="utf-8"))
    assert payload["session_id"] == session.session_id
    assert not list(manager.environment.state_file.parent.glob("*.tmp"))

    manager.environment.state_file.write_text('{"schema_version": 1, "state":', encoding="utf-8")
    status = manager.status()
    stopped = manager.stop()

    assert status.code == "DEV_STATE_CORRUPT"
    assert status.state == DevStatusKind.CORRUPT.value
    assert stopped.code == "DEV_STATE_CORRUPT"
    assert stopped.state == DevStatusKind.CORRUPT.value


def test_missing_required_process_field_is_classified_as_corrupt(tmp_path: Path) -> None:
    manager, _backend = _manager(tmp_path)
    payload = _session(
        manager.environment,
        state=DevSessionState.RUNNING,
        process=ProcessIdentity(1, 1.0, "python", ("python", "gui.py"), "."),
    ).as_dict()
    del payload["process"]["pid"]
    manager.environment.state_file.parent.mkdir(parents=True, exist_ok=True)
    manager.environment.state_file.write_text(json.dumps(payload), encoding="utf-8")

    status = manager.status()

    assert status.code == "DEV_STATE_CORRUPT"
    assert status.state == DevStatusKind.CORRUPT.value


def test_concurrent_second_start_is_refused(tmp_path: Path) -> None:
    entered_readiness = threading.Event()
    release_readiness = threading.Event()

    def delayed_readiness(_environment, _identity):
        entered_readiness.set()
        assert release_readiness.wait(timeout=2)
        return True, "ready"

    manager, backend = _manager(tmp_path, readiness=delayed_readiness, ready_timeout=3)
    first_result: list[DevResult] = []

    first_thread = threading.Thread(target=lambda: first_result.append(manager.start()))
    first_thread.start()
    assert entered_readiness.wait(timeout=2)

    second = manager.start()
    release_readiness.set()
    first_thread.join(timeout=3)

    assert second.ok is False
    assert second.code == "DEV_START_PREFLIGHT_FAILED"
    assert first_result[0].ok is True
    assert backend.launch_count == 1


def test_stale_marker_with_dead_process_recovers_without_kill(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    identity = ProcessIdentity(
        pid=100,
        created_at=1.0,
        executable=str(manager.environment.python_executable),
        command_line=("python", "gui.py"),
        cwd=str(manager.environment.repository_root),
    )
    backend.identity = identity
    backend.alive = False
    manager._write_session(
        _session(manager.environment, state=DevSessionState.RUNNING, process=identity)
    )

    result = manager.recover()
    persisted = manager._read_session()

    assert result.ok is True
    assert result.code == "DEV_STALE_RECOVERED"
    assert persisted is not None and persisted.state is DevSessionState.STOPPED
    assert persisted.process is None
    assert backend.force_stop_count == 0
    assert backend.request_stop_count == 0


def test_pid_reuse_mismatch_refuses_to_kill(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    backend.alive = True
    backend.mismatch = True
    identity = ProcessIdentity(
        pid=101,
        created_at=1.0,
        executable=str(manager.environment.python_executable),
        command_line=("python", "gui.py"),
        cwd=str(manager.environment.repository_root),
    )
    backend.identity = identity
    manager._write_session(
        _session(manager.environment, state=DevSessionState.RUNNING, process=identity)
    )

    result = manager.stop()

    assert result.ok is False
    assert result.code == "DEV_OWNERSHIP_MISMATCH"
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0


def test_owned_process_stop_is_bounded_and_idempotent(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True

    stopped = manager.stop()
    stopped_again = manager.stop()
    recovered_again = manager.recover()

    assert stopped.ok is True
    assert stopped.code == "DEV_SESSION_STOPPED"
    assert backend.request_stop_count == 1
    assert backend.force_stop_count == 0
    assert stopped_again.ok is True
    assert stopped_again.code == "DEV_STOP_ALREADY_STOPPED"
    assert recovered_again.ok is True
    assert recovered_again.code == "DEV_RECOVERY_NOT_NEEDED"


def test_status_is_read_only_and_requires_runtime_readiness(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True
    before = manager.environment.state_file.read_bytes()
    manager.environment.lock_file.unlink(missing_ok=True)

    status = manager.status()
    after = manager.environment.state_file.read_bytes()

    assert status.ok is True
    assert status.state == DevStatusKind.RUNNING_OWNED.value
    assert before == after
    assert not manager.environment.lock_file.exists()

    manager.readiness_probe = lambda _environment, _identity: (False, "worker missing")
    degraded = manager.status()
    assert degraded.ok is False
    assert degraded.code == "DEV_SESSION_DEGRADED"
    assert degraded.state == DevStatusKind.STALE.value


def test_doctor_does_not_mutate_session_state(tmp_path: Path) -> None:
    manager, _backend = _manager(tmp_path)
    manager._write_session(_session(manager.environment, state=DevSessionState.STOPPED))
    before = manager.environment.state_file.read_bytes()
    manager.environment.lock_file.unlink(missing_ok=True)

    result = manager.doctor()

    assert result.details["read_only"] is True
    assert manager.environment.state_file.read_bytes() == before
    assert not manager.environment.lock_file.exists()


def test_readiness_failure_cleans_exact_owned_process_and_marks_failed(tmp_path: Path) -> None:
    manager, backend = _manager(
        tmp_path,
        readiness=lambda _environment, _identity: (False, "not ready"),
        ready_timeout=0,
    )

    result = manager.start()
    persisted = manager._read_session()

    assert result.ok is False
    assert result.code == "DEV_READINESS_FAILED"
    assert result.details["cleanup_confirmed"] is True
    assert backend.request_stop_count == 1
    assert persisted is not None and persisted.state is DevSessionState.FAILED
    assert persisted.process is None


def test_launch_failure_is_structured_and_does_not_report_running(tmp_path: Path) -> None:
    backend = FakeProcessBackend()
    backend.fail_launch = True
    manager, _backend = _manager(tmp_path, backend=backend)

    result = manager.start()
    persisted = manager._read_session()

    assert result.ok is False
    assert result.code == "DEV_LAUNCH_FAILED"
    assert result.state == DevStatusKind.FAILED.value
    assert persisted is not None and persisted.state is DevSessionState.FAILED


def test_foreign_port_is_a_preflight_blocker_not_ownership_evidence(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path, port_probe=lambda _host, _port: True)

    preflight = manager.preflight()
    status = manager.status()

    assert preflight.ok is False
    assert "DEV_PORT_IN_USE" in preflight.details["blockers"]
    assert status.state == DevStatusKind.NO_SESSION.value
    assert backend.launch_count == 0


def test_recovery_with_live_exact_owned_process_is_non_destructive(tmp_path: Path) -> None:
    manager, backend = _manager(tmp_path)
    started = manager.start()
    assert started.ok is True

    recovered = manager.recover()

    assert recovered.ok is False
    assert recovered.code == "DEV_SESSION_ACTIVE"
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0


def test_storage_probe_uses_read_only_components_not_runtime_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment(tmp_path)
    marker = environment.repository_root / "config" / "state" / "storage_backend.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    ok, _message = runtime_module._default_storage_probe(environment)
    probe = captured["command"][2]

    assert ok is True
    assert "DatabaseSettings.from_backend_marker" in probe
    assert "StorageHealthChecker" in probe
    assert "runtime_health" not in probe
    assert "migrate_legacy_backend_marker" not in probe


def test_session_state_paths_are_repository_scoped_and_ignored_area(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    assert environment.state_file == (
        environment.repository_root / "config" / "state" / "dev-runtime-session.json"
    )
    assert environment.lock_file.parent == environment.state_file.parent
    assert environment.log_file.parent == environment.state_file.parent
    assert environment.host == DEV_HOST
    assert environment.port == DEV_PORT


def test_default_port_probe_is_callable_after_module_split() -> None:
    assert runtime_module._port_is_listening("127.0.0.1", 1) is False
