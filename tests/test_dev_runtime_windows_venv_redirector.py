from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from module.dev_runtime import DevEnvironment, ProcessBackend, ProcessIdentity
from module.dev_runtime import contracts as contracts_module
from module.dev_runtime import process as process_module


def _environment(tmp_path: Path) -> DevEnvironment:
    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return DevEnvironment(repository_root=root, python_executable=python)


class _LaunchHandle:
    def __init__(self) -> None:
        self.running = True
        self.terminate_count = 0
        self.kill_count = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminate_count += 1
        self.running = False

    def kill(self) -> None:
        self.kill_count += 1
        self.running = False

    def wait(self, timeout: float):
        if self.running:
            raise process_module.subprocess.TimeoutExpired("synthetic", timeout)
        return 0


def _allow_synthetic_windows_venv(
    environment: DevEnvironment,
    base_python: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contracts_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(contracts_module.sys, "executable", str(environment.python_executable))
    monkeypatch.setattr(
        contracts_module.sys,
        "_base_executable",
        str(base_python),
        raising=False,
    )


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


def test_windows_redirector_runtime_base_argv_is_allowed_for_current_project_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "redirected-runtime"
    base_python = environment.repository_root / "base-python" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    _allow_synthetic_windows_venv(environment, base_python, monkeypatch)

    command = ProcessBackend.expected_command(environment, session_id)
    command[0] = str(base_python)
    identity = ProcessIdentity(
        pid=8124,
        created_at=13.5,
        executable=str(base_python),
        command_line=tuple(command),
        cwd=str(environment.repository_root),
    )

    assert ProcessBackend.identity_belongs_to_session(
        environment, session_id, identity
    ) is True


def test_windows_redirector_runtime_base_argv_is_rejected_for_other_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "foreign-venv"
    base_python = environment.repository_root / "base-python" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(contracts_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        contracts_module.sys,
        "executable",
        str(environment.repository_root / "foreign-venv" / "Scripts" / "python.exe"),
    )
    monkeypatch.setattr(
        contracts_module.sys,
        "_base_executable",
        str(base_python),
        raising=False,
    )

    command = ProcessBackend.expected_command(environment, session_id)
    command[0] = str(base_python)
    identity = ProcessIdentity(
        pid=8125,
        created_at=14.5,
        executable=str(base_python),
        command_line=tuple(command),
        cwd=str(environment.repository_root),
    )

    assert ProcessBackend.identity_belongs_to_session(
        environment, session_id, identity
    ) is False


def test_redirector_relaxation_does_not_allow_foreign_argv_python(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session_id = "foreign-argv"
    command = ProcessBackend.expected_command(environment, session_id)
    command[0] = str(environment.repository_root / "foreign" / "python.exe")
    identity = ProcessIdentity(
        pid=8126,
        created_at=15.5,
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
    pid = 8127
    actual_image = environment.repository_root / "base-python" / "python.exe"
    fake = SimpleNamespace(
        status=lambda: "running",
        create_time=lambda: 16.5,
        exe=lambda: str(actual_image),
        cmdline=lambda: ProcessBackend.expected_command(environment, session_id),
        cwd=lambda: str(environment.repository_root),
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: fake)

    backend = ProcessBackend()
    backend._launch_expectations[pid] = (environment, session_id)
    handle = _LaunchHandle()
    backend._launch_handles[pid] = handle

    captured = backend.capture(pid)

    assert captured is not None
    assert captured.executable == str(actual_image)
    assert captured.command_line == tuple(
        ProcessBackend.expected_command(environment, session_id)
    )
    assert handle.terminate_count == 0
    assert pid not in backend._launch_handles


def test_capture_adopts_windows_redirector_child_with_base_python_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "adopt-redirected-runtime"
    launcher_pid = 8128
    child_pid = 8129
    base_python = environment.repository_root / "base-python" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("", encoding="utf-8")
    _allow_synthetic_windows_venv(environment, base_python, monkeypatch)
    monkeypatch.setattr(process_module, "_IS_WINDOWS", True)

    launcher_command = ProcessBackend.expected_command(environment, session_id)
    child_command = list(launcher_command)
    child_command[0] = str(base_python)

    child = SimpleNamespace(
        pid=child_pid,
        status=lambda: "running",
        create_time=lambda: 17.6,
        exe=lambda: str(base_python),
        cmdline=lambda: child_command,
        cwd=lambda: str(environment.repository_root),
    )
    launcher = SimpleNamespace(
        pid=launcher_pid,
        status=lambda: "running",
        create_time=lambda: 17.5,
        exe=lambda: str(environment.python_executable),
        cmdline=lambda: launcher_command,
        cwd=lambda: str(environment.repository_root),
        children=lambda recursive=True: [child],
    )

    def get_process(pid: int):
        if pid == launcher_pid:
            return launcher
        if pid == child_pid:
            return child
        raise AssertionError(f"unexpected pid {pid}")

    monkeypatch.setattr(process_module.psutil, "Process", get_process)

    backend = ProcessBackend()
    backend._launch_expectations[launcher_pid] = (environment, session_id)
    handle = _LaunchHandle()
    backend._launch_handles[launcher_pid] = handle

    captured = backend.capture(launcher_pid)

    assert captured is not None
    assert captured.pid == child_pid
    assert captured.command_line == tuple(child_command)
    assert launcher_pid not in backend._launch_expectations
    assert backend._launch_expectations[child_pid] == (environment, session_id)
    assert launcher_pid not in backend._launch_handles
    assert handle.terminate_count == 0


def test_unverified_just_launched_process_is_stopped_through_owned_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment(tmp_path)
    session_id = "unverified-launch"
    pid = 8130
    command = ProcessBackend.expected_command(environment, session_id)
    command[-1] = "foreign-profile"
    fake = SimpleNamespace(
        status=lambda: "running",
        create_time=lambda: 18.5,
        exe=lambda: str(environment.repository_root / "base-python" / "python.exe"),
        cmdline=lambda: command,
        cwd=lambda: str(environment.repository_root),
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda _pid: fake)

    backend = ProcessBackend()
    backend._launch_expectations[pid] = (environment, session_id)
    handle = _LaunchHandle()
    backend._launch_handles[pid] = handle

    assert backend.capture(pid) is None
    assert handle.terminate_count == 1
    assert handle.running is False
    assert pid not in backend._launch_handles


def test_actual_image_still_participates_in_pid_reuse_detection(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    session_id = "image-reuse"
    stored = ProcessIdentity(
        pid=8131,
        created_at=19.5,
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
