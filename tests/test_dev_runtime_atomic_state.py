from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from module.dev_runtime import DevEnvironment, DevSession, DevSessionManager, DevSessionState
from module.dev_runtime import manager as manager_module


def test_failed_state_write_removes_owned_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
    )
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "ready"),
        port_probe=lambda _host, _port: False,
        now=lambda: datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    session = DevSession(
        session_id="atomic-failure",
        state=DevSessionState.CREATED,
        repository_root=str(root),
        created_at="2026-08-29T00:00:00+00:00",
        updated_at="2026-08-29T00:00:00+00:00",
    )

    def failing_write(path: str, data: str) -> None:
        Path(path).write_text(data[:1], encoding="utf-8")
        raise OSError("synthetic fsync/write failure")

    monkeypatch.setattr(manager_module, "file_write", failing_write)

    with pytest.raises(OSError, match="synthetic"):
        manager._write_session(session)

    assert not environment.state_file.exists()
    assert tuple(environment.state_file.parent.glob("dev-session.json.*.tmp")) == ()
