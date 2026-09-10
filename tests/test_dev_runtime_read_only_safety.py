from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from module.dev_runtime import diagnostics as diagnostics_module


class _Backend:
    def __init__(self, identity: ProcessIdentity | None = None) -> None:
        self.identity = identity
        self.alive = identity is not None
        self.launch_count = 0
        self.request_stop_count = 0
        self.force_stop_count = 0

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if not self.alive:
            return None
        return identity == self.identity

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.launch_count += 1
        raise AssertionError("launch не должен вызываться в safety regression")

    def capture(self, pid: int) -> ProcessIdentity | None:
        if self.identity is not None and self.identity.pid == pid and self.alive:
            return self.identity
        return None

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return ()

    def is_descendant(self, child_pid: int, parent: ProcessIdentity) -> bool:
        return True

    def listens_on(self, pid: int, host: str, port: int) -> bool:
        return True

    def request_stop(self, identity: ProcessIdentity) -> bool:
        self.request_stop_count += 1
        return False

    def wait_exit(self, identity: ProcessIdentity, timeout: float) -> bool:
        return not self.alive

    def force_stop(self, identity: ProcessIdentity) -> bool:
        self.force_stop_count += 1
        return False


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
    backend: _Backend | None = None,
) -> DevSessionManager:
    manager = DevSessionManager(
        _environment(tmp_path),
        process_backend=backend or _Backend(),
        shared_webui=False,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: False,
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    return manager


def _write_registry(path: Path, payload: dict[str, object]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_preflight_blocks_pending_dependency_sync_without_starting_gui(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager._webui_registry_check = lambda: (True, "registry ready")
    marker = manager.environment.repository_root / "config" / "webui-dependency-sync-pending"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending\n", encoding="utf-8")

    result = manager.start()

    assert result.ok is False
    assert result.code == "DEV_START_PREFLIGHT_FAILED"
    assert "DEV_DEPENDENCY_SYNC_PENDING" in result.details["preflight"]["details"]["blockers"]
    assert manager.process_backend.launch_count == 0


def test_doctor_reads_legacy_worker_registry_without_migration_or_lock_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    legacy = manager.environment.repository_root / "config" / "webui-workers.json"
    expected = _write_registry(
        legacy,
        {
            "owner_pid": 111,
            "owner_created_at": 1.0,
            "workers": {
                "ap": {"pid": 222, "created_at": 2.0},
            },
        },
    )
    from module.webui import worker_registry

    monkeypatch.setattr(worker_registry, "process_matches", lambda _record: None)

    result = manager.doctor()

    assert result.details["read_only"] is True
    assert legacy.read_bytes() == expected
    assert not (manager.environment.repository_root / "cache" / "webui-workers.json").exists()
    assert not (manager.environment.repository_root / "cache" / "webui-workers.json.lock").exists()
    assert not (manager.environment.repository_root / "config" / "webui-workers.json.lock").exists()


def test_preflight_refuses_live_orphan_worker_without_killing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    registry = manager.environment.repository_root / "cache" / "webui-workers.json"
    _write_registry(
        registry,
        {
            "owner_pid": None,
            "owner_created_at": None,
            "workers": {
                "production": {"pid": 333, "created_at": 3.0},
            },
        },
    )
    from module.webui import worker_registry

    monkeypatch.setattr(worker_registry, "process_matches", lambda _record: True)

    preflight = manager.preflight()

    assert preflight.ok is False
    assert "DEV_WEBUI_CONFLICT" in preflight.details["blockers"]
    assert manager.process_backend.request_stop_count == 0
    assert manager.process_backend.force_stop_count == 0


def test_readiness_uses_read_only_registry_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    identity = ProcessIdentity(
        pid=444,
        created_at=4.0,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py"),
        cwd=str(environment.repository_root),
    )
    manager = DevSessionManager(
        environment,
        process_backend=_Backend(identity),
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )
    registry = environment.repository_root / "cache" / "webui-workers.json"
    expected = _write_registry(
        registry,
        {
            "owner_pid": 555,
            "owner_created_at": 5.0,
            "workers": {
                "ap": {"pid": 666, "created_at": 6.0},
            },
        },
    )
    from module.webui import worker_registry

    monkeypatch.setattr(worker_registry, "process_matches", lambda _record: True)
    monkeypatch.setattr(diagnostics_module, "_http_ready", lambda _host, _port: True)

    ready, _reason = manager._default_readiness_probe(environment, identity)

    assert ready is True
    assert registry.read_bytes() == expected
    assert not registry.with_name("webui-workers.json.lock").exists()


def test_failed_live_process_remains_blocked_after_preflight_race(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    identity = ProcessIdentity(
        pid=777,
        created_at=7.0,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py", "--dev-session-id", "failed-live"),
        cwd=str(environment.repository_root),
    )
    backend = _Backend(identity)
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
    )
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
    manager.preflight = lambda: DevResult(
        ok=True,
        code="DEV_PREFLIGHT_OK",
        message="synthetic stale preflight",
        state=DevStatusKind.NO_SESSION.value,
    )

    status = manager.status()
    result = manager.start()

    assert status.code == "DEV_FAILED_PROCESS_STILL_RUNNING"
    assert status.state == DevStatusKind.STALE.value
    assert result.ok is False
    assert result.code == "DEV_SESSION_ACTIVE"
    assert backend.launch_count == 0
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0


def test_readiness_rejects_ap_worker_outside_devsession_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    identity = ProcessIdentity(
        pid=7301,
        created_at=73.01,
        executable=str(environment.python_executable),
        command_line=("python", "gui.py"),
        cwd=str(environment.repository_root),
    )
    registry = environment.repository_root / "cache" / "webui-workers.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "owner_pid": 7302,
                "owner_created_at": 73.02,
                "workers": {"ap": {"pid": 7303, "created_at": 73.03}},
            }
        ),
        encoding="utf-8",
    )

    class TreeBackend:
        def is_descendant(self, child_pid: int, _parent: ProcessIdentity) -> bool:
            return child_pid == 7302

        def listens_on(self, pid: int, host: str, port: int) -> bool:
            return pid == 7302 and host == environment.host and port == environment.port

    manager = DevSessionManager(
        environment,
        process_backend=TreeBackend(),
        shared_webui=False,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
    )

    from module.webui import worker_registry

    monkeypatch.setattr(worker_registry, "process_matches", lambda _record: True)
    monkeypatch.setattr(diagnostics_module, "_http_ready", lambda _host, _port: True)

    ready, reason = manager._default_readiness_probe(environment, identity)

    assert ready is False
    assert "рабочий процесс назначенного development target не принадлежит дереву DevSession" in reason
