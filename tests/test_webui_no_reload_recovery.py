"""Регрессии восстановления WebUI и защиты от осиротевших процессов."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gui
import module.webui.setting as webui_setting
import module.webui.windows_process_lifetime as process_lifetime


ROOT = Path(__file__).resolve().parents[1]


class TestWebUiNoReloadRecovery(unittest.TestCase):
    def test_no_reload_starts_only_after_orphan_recovery(self):
        call_order = []

        def recover():
            call_order.append("recover")
            return True

        def run_webui(*args):
            call_order.append(("func", args))

        with (
            patch.object(gui, "_recover_orphaned_workers", side_effect=recover) as recovery,
            patch.object(gui, "func", side_effect=run_webui) as func,
        ):
            self.assertTrue(gui._run_webui_without_reload())

        recovery.assert_called_once_with()
        func.assert_called_once_with(None, None)
        self.assertEqual(["recover", ("func", (None, None))], call_order)

    def test_no_reload_refuses_start_when_orphan_recovery_fails(self):
        with (
            patch.object(gui, "_recover_orphaned_workers", return_value=False) as recovery,
            patch.object(gui, "func", new=Mock()) as func,
        ):
            self.assertFalse(gui._run_webui_without_reload())

        recovery.assert_called_once_with()
        func.assert_not_called()

    def test_lifetime_guard_is_enabled_only_for_windows_gui_main_process(self):
        with (
            patch.object(webui_setting.os, "name", "nt"),
            patch.object(
                webui_setting.multiprocessing,
                "current_process",
                return_value=SimpleNamespace(name="MainProcess"),
            ),
            patch.object(sys, "argv", ["gui.py"]),
            patch(
                "module.webui.windows_process_lifetime.install_windows_process_lifetime_guards",
                return_value=12345,
            ) as install_guard,
            patch("module.logger.logger.info") as log_info,
        ):
            webui_setting._ensure_gui_process_lifetime_guard()

        install_guard.assert_called_once_with()
        log_info.assert_called_once()

    def test_lifetime_guard_is_not_enabled_in_spawned_webui_process(self):
        with (
            patch.object(webui_setting.os, "name", "nt"),
            patch.object(
                webui_setting.multiprocessing,
                "current_process",
                return_value=SimpleNamespace(name="gui"),
            ),
            patch.object(sys, "argv", ["gui.py"]),
            patch(
                "module.webui.windows_process_lifetime.install_windows_process_lifetime_guards",
            ) as install_guard,
        ):
            webui_setting._ensure_gui_process_lifetime_guard()

        install_guard.assert_not_called()

    def test_lifecycle_stop_watcher_opens_repository_event(self):
        kernel32 = Mock()
        kernel32.OpenEventW.return_value = 123

        with (
            patch.dict(
                process_lifetime.os.environ,
                {"AZURPILOT_LIFECYCLE_STOP_EVENT": "Local\\AzurPilot.StopRequested.test"},
                clear=False,
            ),
            patch.object(process_lifetime, "_kernel32", return_value=kernel32),
            patch.object(process_lifetime.threading, "Thread") as thread,
        ):
            self.assertTrue(process_lifetime._start_lifecycle_stop_watcher())

        kernel32.OpenEventW.assert_called_once_with(
            process_lifetime._SYNCHRONIZE,
            False,
            "Local\\AzurPilot.StopRequested.test",
        )
        thread.return_value.start.assert_called_once_with()

    def test_lifecycle_stop_watcher_ignores_missing_event_name(self):
        with (
            patch.dict(process_lifetime.os.environ, {}, clear=True),
            patch.object(process_lifetime.threading, "Thread") as thread,
        ):
            self.assertFalse(process_lifetime._start_lifecycle_stop_watcher())

        thread.assert_not_called()

    def test_lifecycle_stop_watcher_handles_open_race_nonfatally(self):
        kernel32 = Mock()
        kernel32.OpenEventW.return_value = 0

        with (
            patch.dict(
                process_lifetime.os.environ,
                {"AZURPILOT_LIFECYCLE_STOP_EVENT": "Local\\AzurPilot.StopRequested.test"},
                clear=False,
            ),
            patch.object(process_lifetime, "_kernel32", return_value=kernel32),
            patch.object(
                process_lifetime.ctypes,
                "get_last_error",
                return_value=2,
                create=True,
            ),
            patch.object(
                process_lifetime.ctypes,
                "FormatError",
                return_value="not found",
                create=True,
            ),
            patch("module.logger.logger.warning") as warning,
            patch.object(process_lifetime.threading, "Thread") as thread,
        ):
            self.assertFalse(process_lifetime._start_lifecycle_stop_watcher())

        warning.assert_called_once()
        thread.assert_not_called()

    def test_lifecycle_stop_event_interrupts_main_thread(self):
        kernel32 = Mock()
        kernel32.WaitForSingleObject.return_value = process_lifetime._WAIT_OBJECT_0

        with patch.object(process_lifetime._thread, "interrupt_main") as interrupt_main:
            process_lifetime._wait_for_lifecycle_stop_event(
                kernel32,
                123,
                "Local\\AzurPilot.StopRequested.test",
            )

        kernel32.CloseHandle.assert_called_once_with(123)
        interrupt_main.assert_called_once_with()

    def test_lifecycle_stop_event_warns_on_unexpected_wait_result(self):
        kernel32 = Mock()
        kernel32.WaitForSingleObject.return_value = 258

        with (
            patch.object(process_lifetime._thread, "interrupt_main") as interrupt_main,
            patch("module.logger.logger.warning") as warning,
        ):
            process_lifetime._wait_for_lifecycle_stop_event(
                kernel32,
                123,
                "Local\\AzurPilot.StopRequested.test",
            )

        kernel32.CloseHandle.assert_called_once_with(123)
        warning.assert_called_once()
        interrupt_main.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Проверка требует Windows Job Object")
    def test_windows_console_death_reaps_gui_process_tree(self):
        import psutil

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell 7 не найден")

        controller = None
        root_process = None
        grandchild_process = None
        with tempfile.TemporaryDirectory(prefix="azurpilot-lifetime-") as temp_dir:
            temp_path = Path(temp_dir)
            probe_path = temp_path / "probe.py"
            state_path = temp_path / "state.json"
            controller_path = temp_path / "controller.ps1"

            probe_path.write_text(
                """
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, sys.argv[2])
from module.webui.windows_process_lifetime import install_windows_process_lifetime_guards

lineage = []
for process in psutil.Process(os.getpid()).parents():
    try:
        lineage.append({"pid": process.pid, "name": process.name()})
    except psutil.Error as exc:
        lineage.append({"pid": process.pid, "name": f"<{type(exc).__name__}>"})

parent_pid = install_windows_process_lifetime_guards()
grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
)
state_path = Path(sys.argv[1])
temp_state_path = state_path.with_suffix(".tmp")
temp_state_path.write_text(
    json.dumps(
        {
            "root_pid": os.getpid(),
            "grandchild_pid": grandchild.pid,
            "parent_pid": parent_pid,
            "lineage": lineage,
        }
    ),
    encoding="utf-8",
)
os.replace(temp_state_path, state_path)
while True:
    time.sleep(1)
""".lstrip(),
                encoding="utf-8",
            )
            controller_path.write_text(
                """
param(
    [Parameter(Mandatory)]
    [string]$PythonExecutable,

    [Parameter(Mandatory)]
    [string]$ProbePath,

    [Parameter(Mandatory)]
    [string]$StatePath,

    [Parameter(Mandatory)]
    [string]$RepositoryRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

& $PythonExecutable $ProbePath $StatePath $RepositoryRoot
$pythonExitCode = $LASTEXITCODE
exit $pythonExitCode
""".lstrip(),
                encoding="utf-8",
            )

            try:
                controller = subprocess.Popen(
                    [
                        pwsh,
                        "-NoLogo",
                        "-NoProfile",
                        "-File",
                        str(controller_path),
                        "-PythonExecutable",
                        sys.executable,
                        "-ProbePath",
                        str(probe_path),
                        "-StatePath",
                        str(state_path),
                        "-RepositoryRoot",
                        str(ROOT),
                    ],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if state_path.exists():
                        break
                    if controller.poll() is not None:
                        output, _ = controller.communicate(timeout=5)
                        self.fail(
                            "Контроллер завершился до готовности probe-процесса:\n"
                            f"{output}"
                        )
                    time.sleep(0.1)
                self.assertTrue(state_path.exists(), "Probe-процесс не сообщил о готовности")

                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    controller.pid,
                    state["parent_pid"],
                    f"Фактическая цепочка родителей: {state['lineage']}",
                )
                root_process = psutil.Process(state["root_pid"])
                grandchild_process = psutil.Process(state["grandchild_pid"])
                self.assertTrue(root_process.is_running())
                self.assertTrue(grandchild_process.is_running())

                controller.kill()
                controller.wait(timeout=5)

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    root_alive = root_process.is_running()
                    grandchild_alive = grandchild_process.is_running()
                    if not root_alive and not grandchild_alive:
                        break
                    time.sleep(0.1)

                self.assertFalse(
                    root_process.is_running(),
                    "Корневой WebUI-процесс пережил завершение управляющей консоли",
                )
                self.assertFalse(
                    grandchild_process.is_running(),
                    "Дочерний процесс пережил завершение корневого WebUI",
                )
            finally:
                if controller is not None and controller.poll() is None:
                    controller.kill()
                    controller.wait(timeout=5)
                if root_process is not None and root_process.is_running():
                    subprocess.run(
                        ["taskkill", "/PID", str(root_process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                if controller is not None and controller.stdout is not None:
                    controller.stdout.close()


if __name__ == "__main__":
    unittest.main()
