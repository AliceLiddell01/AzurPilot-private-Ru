from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from module.dev_runtime import DevEnvironment, DevTarget, ProcessBackend, ProcessIdentity
from module.dev_runtime import process as process_module


_TEST_CTRL_BREAK_EVENT = 0x7FFF


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return DevEnvironment(repository_root=root, python_executable=python, dev_target=DevTarget("ap"))


def test_windows_redirected_child_stop_targets_launcher_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "group-stop"
    launcher_pid = 9101
    child_pid = 9102
    base_python = environment.repository_root / "base-python" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")

    launcher_command = ProcessBackend.expected_command(environment, session_id)
    child_command = list(launcher_command)

    identity = ProcessIdentity(
        pid=child_pid,
        created_at=91.02,
        executable=str(base_python),
        command_line=tuple(child_command),
        cwd=str(environment.repository_root),
    )
    launcher = SimpleNamespace(
        pid=launcher_pid,
        create_time=lambda: 91.01,
        exe=lambda: str(environment.python_executable),
        cmdline=lambda: launcher_command,
        cwd=lambda: str(environment.repository_root),
    )
    child = SimpleNamespace(
        pid=child_pid,
        parent=lambda: launcher,
    )

    monkeypatch.setattr(process_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        process_module,
        "_WINDOWS_CTRL_BREAK_EVENT",
        _TEST_CTRL_BREAK_EVENT,
    )
    monkeypatch.setattr(
        process_module.psutil,
        "Process",
        lambda pid: child if pid == child_pid else launcher,
    )

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_module.os,
        "kill",
        lambda pid, sig: killed.append((pid, sig)),
    )

    backend = ProcessBackend()
    monkeypatch.setattr(backend, "_identity_is_destructively_trusted", lambda _identity: True)
    monkeypatch.setattr(backend, "matches", lambda _identity: True)

    assert backend.request_stop(identity) is True
    assert killed == [(launcher_pid, _TEST_CTRL_BREAK_EVENT)]


def test_windows_redirected_child_stop_fails_closed_without_exact_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "group-stop-missing"
    child_pid = 9202
    base_python = environment.repository_root / "base-python" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")

    child_command = ProcessBackend.expected_command(environment, session_id)
    identity = ProcessIdentity(
        pid=child_pid,
        created_at=92.02,
        executable=str(base_python),
        command_line=tuple(child_command),
        cwd=str(environment.repository_root),
    )
    foreign_parent = SimpleNamespace(
        pid=9201,
        create_time=lambda: 92.01,
        exe=lambda: str(environment.python_executable),
        cmdline=lambda: [
            str(environment.python_executable),
            str(environment.repository_root / "foreign.py"),
        ],
        cwd=lambda: str(environment.repository_root),
    )
    child = SimpleNamespace(
        pid=child_pid,
        parent=lambda: foreign_parent,
    )

    monkeypatch.setattr(process_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        process_module,
        "_WINDOWS_CTRL_BREAK_EVENT",
        _TEST_CTRL_BREAK_EVENT,
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: child)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_module.os,
        "kill",
        lambda pid, sig: killed.append((pid, sig)),
    )

    backend = ProcessBackend()
    monkeypatch.setattr(backend, "_identity_is_destructively_trusted", lambda _identity: True)
    monkeypatch.setattr(backend, "matches", lambda _identity: True)

    assert backend.request_stop(identity) is False
    assert killed == []
