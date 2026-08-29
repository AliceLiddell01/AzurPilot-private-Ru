from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from module.dev_runtime import (
    DevEnvironment,
    DevResult,
    DevSession,
    DevSessionManager,
    DevSessionState,
    DevStatusKind,
    ProcessIdentity,
)


class OrphanBackend:
    def __init__(self, identity: ProcessIdentity) -> None:
        self.identity = identity
        self.launch_count = 0
        self.request_stop_count = 0
        self.force_stop_count = 0

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        if session_id in self.identity.command_line:
            return (self.identity,)
        return ()

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.launch_count += 1
        return self.identity.pid

    def matches(self, identity: ProcessIdentity) -> bool | None:
        return identity == self.identity

    def request_stop(self, identity: ProcessIdentity) -> bool:
        self.request_stop_count += 1
        return False

    def force_stop(self, identity: ProcessIdentity) -> bool:
        self.force_stop_count += 1
        return False


def test_failed_session_rechecks_orphan_under_start_lock(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
    )
    identity = ProcessIdentity(
        pid=8801,
        created_at=11.0,
        executable=str(environment.python_executable),
        command_line=(
            str(environment.python_executable),
            str(root / "gui.py"),
            "--dev-session-id",
            "failed-orphan",
        ),
        cwd=str(root),
    )
    backend = OrphanBackend(identity)
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        session_id_factory=lambda: "must-not-start",
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    manager.preflight = lambda: DevResult(
        ok=True,
        code="DEV_PREFLIGHT_OK",
        message="synthetic stale preflight",
        state=DevStatusKind.NO_SESSION.value,
    )
    manager._write_session(
        DevSession(
            session_id="failed-orphan",
            state=DevSessionState.FAILED,
            repository_root=str(root),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            process=None,
        )
    )

    result = manager.start()
    persisted = manager._read_session()

    assert result.ok is False
    assert result.code == "DEV_RECOVERY_PROCESS_FOUND"
    assert persisted is not None
    assert persisted.state is DevSessionState.STALE
    assert persisted.process == identity
    assert backend.launch_count == 0
    assert backend.request_stop_count == 0
    assert backend.force_stop_count == 0
