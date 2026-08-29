from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from module.dev_runtime import (
    DevEnvironment,
    DevResult,
    DevSessionManager,
    DevSessionState,
    DevStatusKind,
    ProcessBackend,
    ProcessIdentity,
)


class _RaceBackend:
    def __init__(self) -> None:
        self.alive = False
        self.identity: ProcessIdentity | None = None
        self.launch_count = 0
        self.request_stop_count = 0
        self.force_stop_count = 0

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.launch_count += 1
        self.alive = True
        self.identity = ProcessIdentity(
            pid=9901,
            created_at=99.0,
            executable=str(environment.python_executable),
            command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
            cwd=str(environment.repository_root),
        )
        return self.identity.pid

    def capture(self, pid: int) -> ProcessIdentity | None:
        if not self.alive or self.identity is None or self.identity.pid != pid:
            return None
        return self.identity

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if not self.alive:
            return None
        return self.identity == identity

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return ()

    def is_descendant(self, child_pid: int, parent: ProcessIdentity) -> bool:
        return self.matches(parent) is True

    def listens_on(self, pid: int, host: str, port: int) -> bool:
        return self.alive

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


def test_concurrent_stop_wins_over_stale_start_readiness(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
    )
    backend = _RaceBackend()
    readiness_entered = threading.Event()
    release_readiness = threading.Event()

    def delayed_ready(
        _environment: DevEnvironment, _identity: ProcessIdentity
    ) -> tuple[bool, str]:
        readiness_entered.set()
        assert release_readiness.wait(timeout=2)
        return True, "stale ready observation"

    manager = DevSessionManager(
        environment,
        process_backend=backend,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=delayed_ready,
        session_id_factory=lambda: "start-stop-race",
        ready_timeout=3,
        stop_timeout=0.01,
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")

    start_results: list[DevResult] = []
    start_thread = threading.Thread(target=lambda: start_results.append(manager.start()))
    start_thread.start()
    assert readiness_entered.wait(timeout=2)

    stopped = manager.stop()
    release_readiness.set()
    start_thread.join(timeout=3)

    assert len(start_results) == 1
    started = start_results[0]
    persisted = manager._read_session()

    assert stopped.ok is True
    assert stopped.state == DevStatusKind.STOPPED.value
    assert started.ok is False
    assert started.code == "DEV_SESSION_STATE_CHANGED"
    assert started.state == DevStatusKind.STOPPED.value
    assert persisted is not None
    assert persisted.state is DevSessionState.STOPPED
    assert persisted.process is None
    assert backend.launch_count == 1
    assert backend.request_stop_count == 1
    assert backend.force_stop_count == 0
