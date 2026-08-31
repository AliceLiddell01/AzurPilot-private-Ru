from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_tools import dev_runtime as cli_module
from module.dev_runtime import (
    DevEnvironment,
    DevSession,
    DevSessionManager,
    DevSessionState,
    DevStatusKind,
    DevTarget,
    ProcessIdentity,
)


class _StopErrorBackend:
    def __init__(self) -> None:
        self.request_stop_count = 0
        self.force_stop_count = 0

    def matches(self, identity: ProcessIdentity) -> bool | None:
        return True

    def request_stop(self, identity: ProcessIdentity) -> bool:
        self.request_stop_count += 1
        raise RuntimeError("synthetic ownership read failure")

    def wait_exit(self, identity: ProcessIdentity, timeout: float) -> bool:
        raise AssertionError("wait_exit must not run after ownership error")

    def force_stop(self, identity: ProcessIdentity) -> bool:
        self.force_stop_count += 1
        return True

    def find_by_session(
        self, environment: DevEnvironment, session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return ()


def test_stop_converts_mid_stop_runtime_error_to_fail_closed_result(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    identity = ProcessIdentity(
        pid=9911,
        created_at=12.0,
        executable=str(environment.python_executable),
        command_line=(str(environment.python_executable), str(root / "gui.py")),
        cwd=str(root),
    )
    backend = _StopErrorBackend()
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
        stop_timeout=0.01,
    )
    manager._write_session(
        DevSession(
            session_id="stop-runtime-error",
            state=DevSessionState.RUNNING,
            repository_root=str(root),
            created_at="2026-08-29T00:00:00+00:00",
            updated_at="2026-08-29T00:00:00+00:00",
            process=identity,
        )
    )

    result = manager.stop()
    persisted = manager._read_session()

    assert result.ok is False
    assert result.code == "DEV_STOP_UNCONFIRMED"
    assert result.state == DevStatusKind.STALE.value
    assert persisted is not None
    assert persisted.state is DevSessionState.STALE
    assert persisted.process == identity
    assert backend.request_stop_count == 1
    assert backend.force_stop_count == 0


def test_cli_converts_manager_runtime_error_to_structured_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenManager:
        def status(self):
            raise RuntimeError("synthetic operational failure")

    monkeypatch.setattr(cli_module, "DevSessionManager", BrokenManager)
    monkeypatch.setattr(sys, "argv", ["dev_runtime.py", "status"])

    exit_code = cli_module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["code"] == "DEV_CLI_FAILED"
    assert payload["state"] == DevStatusKind.FAILED.value
    assert "RuntimeError" in payload["message"]
