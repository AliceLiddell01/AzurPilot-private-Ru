from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from module.dev_runtime import DevEnvironment, ProcessBackend, ProcessIdentity
from module.dev_runtime import process as process_module


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return DevEnvironment(repository_root=root, python_executable=python)


def test_windows_redirector_image_is_allowed_when_exact_argv_identifies_project_python(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    session_id = "redirector-session"
    actual_image = environment.repository_root / "base-python" / "python.exe"
    identity = ProcessIdentity(
        pid=8123,
        created_at=12.5,
        executable=str(actual_image),
        command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
        cwd=str(environment.repository_root),
    )

    assert ProcessBackend.identity_belongs_to_session(
        environment, session_id, identity
    ) is True


def test_redirector_relaxation_does_not_allow_foreign_argv_python(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session_id = "foreign-argv"
    command = ProcessBackend.expected_command(environment, session_id)
    command[0] = str(environment.repository_root / "foreign" / "python.exe")
    identity = ProcessIdentity(
        pid=8124,
        created_at=13.5,
        executable=str(environment.repository_root / "base-python" / "python.exe"),
        command_line=tuple(command),
        cwd=str(environment.repository_root),
    )

    assert ProcessBackend.identity_belongs_to_session(
        environment, session_id, identity
    ) is False


def test_capture_accepts_redirector_image_for_just_launched_exact_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "capture-redirector"
    pid = 8125
    actual_image = environment.repository_root / "base-python" / "python.exe"
    fake = SimpleNamespace(
        status=lambda: "running",
        create_time=lambda: 14.5,
        exe=lambda: str(actual_image),
        cmdline=lambda: ProcessBackend.expected_command(environment, session_id),
        cwd=lambda: str(environment.repository_root),
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: fake)

    backend = ProcessBackend()
    backend._launch_expectations[pid] = (environment, session_id)

    captured = backend.capture(pid)

    assert captured is not None
    assert captured.executable == str(actual_image)
    assert captured.command_line == tuple(ProcessBackend.expected_command(environment, session_id))


def test_actual_image_still_participates_in_pid_reuse_detection(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session_id = "image-reuse"
    stored = ProcessIdentity(
        pid=8126,
        created_at=15.5,
        executable=str(environment.repository_root / "base-python-a" / "python.exe"),
        command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
        cwd=str(environment.repository_root),
    )
    current = ProcessIdentity(
        pid=stored.pid,
        created_at=stored.created_at,
        executable=str(environment.repository_root / "base-python-b" / "python.exe"),
        command_line=stored.command_line,
        cwd=stored.cwd,
    )
    backend = ProcessBackend()
    backend.capture = lambda _pid: current

    assert backend.matches(stored) is False
