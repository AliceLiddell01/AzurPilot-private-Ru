from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import psutil
import pytest

from module.dev_runtime import DevEnvironment, ProcessBackend, ProcessIdentity


pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Интеграция требует настоящий Windows venv redirector",
)


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
            os.path.abspath(os.fspath(right))
        )


def _cleanup_launcher(
    launcher_pid: int | None,
    launcher_created_at: float | None,
) -> None:
    if launcher_pid is None or launcher_created_at is None:
        return
    try:
        launcher = psutil.Process(launcher_pid)
        if abs(launcher.create_time() - launcher_created_at) >= 0.01:
            return
        children = launcher.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            launcher.kill()
        except psutil.NoSuchProcess:
            pass
        psutil.wait_procs([launcher, *children], timeout=5)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return


def test_real_windows_venv_redirector_launch_capture_and_stop(tmp_path: Path) -> None:
    project_python = Path(sys.executable)
    expected_project_python = (
        Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
    )
    assert _same_path(project_python, expected_project_python), (
        "Windows integration test должен выполняться именно project .venv Python: "
        f"current={project_python}, expected={expected_project_python}"
    )
    assert hasattr(signal, "CTRL_BREAK_EVENT")
    assert hasattr(signal, "SIGBREAK")

    root = tmp_path.resolve()
    (root / "module").mkdir(parents=True)
    gui_path = root / "gui.py"
    gui_path.write_text(
        """
import signal
import time

stopping = False


def stop_handler(_signum, _frame):
    global stopping
    stopping = True


signal.signal(signal.SIGBREAK, stop_handler)
while not stopping:
    time.sleep(0.05)
""".lstrip(),
        encoding="utf-8",
    )

    environment = DevEnvironment(
        repository_root=root,
        python_executable=project_python,
    )
    backend = ProcessBackend()
    session_id = "ci-real-windows-venv-redirector"
    launcher_pid: int | None = None
    launcher_created_at: float | None = None
    identity: ProcessIdentity | None = None

    try:
        launcher_pid = backend.launch(environment, session_id)
        launcher = psutil.Process(launcher_pid)
        launcher_created_at = launcher.create_time()

        identity = backend.capture(launcher_pid)
        assert identity is not None, environment.log_file.read_text(
            encoding="utf-8",
            errors="replace",
        )
        assert identity.pid != launcher_pid, (
            "Windows venv redirector не был adopted: "
            f"launcher_pid={launcher_pid}, captured_pid={identity.pid}, "
            f"captured_executable={identity.executable}, "
            f"captured_argv={identity.command_line}"
        )
        assert ProcessBackend.identity_belongs_to_session(
            environment,
            session_id,
            identity,
        )
        assert backend.matches(identity) is True

        child = psutil.Process(identity.pid)
        parent = child.parent()
        assert parent is not None
        assert parent.pid == launcher_pid
        assert _same_path(parent.exe(), project_python)

        process_group_id = backend._windows_process_group_id(identity)
        assert process_group_id == launcher_pid, (
            "Ctrl-Break обязан адресоваться process group venv launcher: "
            f"group={process_group_id}, launcher={launcher_pid}, child={identity.pid}, "
            f"child_executable={identity.executable}, child_argv={identity.command_line}"
        )

        assert backend.request_stop(identity) is True
        assert backend.wait_exit(identity, 10.0) is True
        launcher.wait(timeout=10.0)
    finally:
        if identity is not None:
            try:
                if backend.matches(identity) is True:
                    backend.force_stop(identity)
            except RuntimeError:
                pass
        _cleanup_launcher(launcher_pid, launcher_created_at)
