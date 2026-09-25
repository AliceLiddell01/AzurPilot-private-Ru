from __future__ import annotations

import multiprocessing
import os
import struct
import sys
from multiprocessing import spawn as multiprocessing_spawn
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

import psutil
import pytest

from module.application import runtime_process_manager, runtime_worker_registry
from module.application.runtime_process_manager import BotRuntimeWorkerManager


def _report_headless_spawn(connection, stop_event):
    import ctypes
    import site
    import sys

    import inflection

    import module.logger

    connection.send(
        {
            "console_window": int(ctypes.windll.kernel32.GetConsoleWindow()),
            "executable": sys.executable,
            "inflection": str(Path(inflection.__file__).resolve()),
            "logger_imported": module.logger is not None,
            "prefix": sys.prefix,
            "site_packages": [
                str(Path(path).resolve()) for path in site.getsitepackages()
            ],
            "stderr_is_none": sys.stderr is None,
            "stdout_is_none": sys.stdout is None,
        }
    )
    connection.send({"cooperative_stop": stop_event.wait(30)})
    connection.close()


def _report_manager_headless_spawn(
    _config_name,
    connection,
    stop_event,
    _repository_root,
    _operation_id,
    _session_id,
):
    _report_headless_spawn(connection, stop_event)


def _read_pe_subsystem(executable: Path) -> int:
    image = executable.read_bytes()
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    assert image[pe_offset : pe_offset + 4] == b"PE\0\0"
    optional_header_offset = pe_offset + 24
    return struct.unpack_from("<H", image, optional_header_offset + 68)[0]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def test_windows_worker_executable_uses_base_pythonw_without_global_mutation(
    monkeypatch, tmp_path
):
    base_python = tmp_path / "base" / "python.exe"
    base_python.parent.mkdir()
    base_python.touch()
    base_pythonw = base_python.with_name("pythonw.exe")
    base_pythonw.touch()

    monkeypatch.setattr(runtime_process_manager.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime_process_manager.sys,
        "executable",
        str(tmp_path / ".venv" / "Scripts" / "python.exe"),
    )
    monkeypatch.setattr(
        runtime_process_manager.sys, "_base_executable", str(base_python)
    )
    initial_multiprocessing_executable = multiprocessing_spawn.get_executable()
    initial_environment = os.environ.copy()

    assert (
        runtime_process_manager._headless_worker_executable() == base_pythonw.absolute()
    )
    assert multiprocessing_spawn.get_executable() == initial_multiprocessing_executable
    assert os.environ.copy() == initial_environment


def test_windows_worker_spawn_fails_closed_when_base_pythonw_is_missing(
    monkeypatch, tmp_path
):
    base_python = tmp_path / "python.exe"
    base_python.touch()
    monkeypatch.setattr(runtime_process_manager.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime_process_manager.sys, "_base_executable", str(base_python)
    )

    with pytest.raises(RuntimeError, match="pythonw.exe"):
        runtime_process_manager._headless_worker_executable()


def test_worker_manager_starts_the_selected_process_implementation(monkeypatch):
    manager = BotRuntimeWorkerManager("alas")
    order: list[str] = []
    process = Mock()
    process.pid = 12345
    process.start.side_effect = lambda: order.append("process-start")

    monkeypatch.setattr(manager, "_reconcile_runtime_state_before_start", lambda: None)
    monkeypatch.setattr(manager, "_registered_worker", lambda: (None, None, True))
    monkeypatch.setattr(manager, "_register_process", lambda _pid: None)

    def create_process(**_kwargs):
        order.append("process-create")
        return process

    monkeypatch.setattr(runtime_process_manager, "Process", create_process)
    with patch.object(
        BotRuntimeWorkerManager,
        "alive",
        new_callable=PropertyMock,
        return_value=False,
    ):
        manager.start("alas", ev=object())

    assert order == ["process-create", "process-start"]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Требуется Windows и запуск multiprocessing через spawn",
)
def test_windows_spawn_runs_headless_worker_with_venv_identity_and_cooperative_stop(
    monkeypatch, tmp_path
):
    assert sys.version_info[:2] == (3, 14)
    executable = runtime_process_manager._headless_worker_executable()
    assert _read_pe_subsystem(executable) == 2

    repository_root = tmp_path / "runtime-root"
    repository_root.mkdir()
    monkeypatch.setattr(runtime_process_manager, "_REPOSITORY_ROOT", repository_root)
    runtime_worker_registry.claim_owner(os.getpid(), repository_root=repository_root)

    stop_event = multiprocessing.Event()
    receiver, sender = multiprocessing.Pipe(duplex=False)
    manager = BotRuntimeWorkerManager("alas")
    monkeypatch.setattr(
        BotRuntimeWorkerManager,
        "run_process",
        staticmethod(_report_manager_headless_spawn),
    )
    process = None
    launcher_before = os.environ.get("__PYVENV_LAUNCHER__")

    try:
        manager.start(
            sender,
            ev=stop_event,
            operation_id="headless-spawn-test",
        )
        process = manager._process
        assert process is not None
        sender.close()

        assert os.environ.get("__PYVENV_LAUNCHER__") == launcher_before
        assert receiver.poll(20), "Рабочий процесс pythonw не вернул ответ IPC"
        result = receiver.recv()

        process_image = Path(psutil.Process(process.pid).exe()).resolve()
        assert process_image == executable.resolve()
        assert _read_pe_subsystem(process_image) == 2
        assert Path(result["executable"]).resolve() == Path(sys.executable).resolve()
        assert Path(result["prefix"]).resolve() == Path(sys.prefix).resolve()
        assert result["site_packages"]
        assert any(
            _is_within(Path(result["inflection"]), Path(site_packages))
            and _is_within(Path(site_packages), Path(sys.prefix))
            for site_packages in result["site_packages"]
        )
        assert result["console_window"] == 0
        assert result["logger_imported"]
        assert result["stdout_is_none"]
        assert result["stderr_is_none"]

        record = runtime_worker_registry.get_workers(
            os.getpid(), repository_root=repository_root
        )["alas"]
        assert record["pid"] == process.pid
        assert (
            abs(record["created_at"] - psutil.Process(process.pid).create_time()) < 0.01
        )

        assert manager.request_cooperative_stop()
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert receiver.poll(10), (
            "Рабочий процесс pythonw не подтвердил штатную остановку"
        )
        assert receiver.recv() == {"cooperative_stop": True}

        assert manager.stop()
        assert (
            runtime_worker_registry.get_workers(
                os.getpid(), repository_root=repository_root
            )
            == {}
        )
        assert manager._process is None
        assert manager._stop_event is None
    finally:
        process = manager._process or process
        if process is not None and process.pid is not None:
            if process.is_alive():
                stop_event.set()
                process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if manager._process is not None:
                manager.stop()
        receiver.close()
        sender.close()


def test_logger_imports_when_gui_process_streams_are_missing():
    import subprocess

    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout = None; sys.stderr = None; import module.logger",
        ],
        cwd=repository_root,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
