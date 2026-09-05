from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATHS = (
    Path("scripts/Start-AzurPilot.ps1"),
    Path("scripts/Stop-AzurPilot.ps1"),
    Path("scripts/Update-AzurPilot.ps1"),
    Path("scripts/Repair-AzurPilot.ps1"),
    Path("scripts/Build-AzurPilot.ps1"),
    Path("scripts/lib/AzurPilot.Shortcut.psm1"),
    Path("scripts/lib/AzurPilot.Lifecycle.psm1"),
)
EXPECTED_EXIT_CODES = {
    "scripts/Start-AzurPilot.ps1": {
        "Success": 0,
        "PreconditionFailure": 20,
        "ForeignPortOwner": 21,
        "ConcurrentStartTimeout": 22,
        "EnvironmentFailure": 23,
        "ReadinessTimeout": 24,
        "BackendFailure": 25,
        "BrowserFailure": 26,
        "UnexpectedFailure": 30,
    },
    "scripts/Stop-AzurPilot.ps1": {
        "Success": 0,
        "PreconditionFailure": 20,
        "ForeignOwnership": 21,
        "Timeout": 22,
        "EnvironmentFailure": 23,
        "UnexpectedFailure": 30,
    },
    "scripts/Update-AzurPilot.ps1": {
        "Success": 0,
        "NetworkFailure": 10,
        "PreconditionFailure": 20,
        "LocalAhead": 21,
        "Diverged": 22,
        "DependencyFailure": 23,
        "UnexpectedFailure": 30,
    },
    "scripts/Repair-AzurPilot.ps1": {
        "Success": 0,
        "PreconditionFailure": 20,
        "ActiveProcess": 21,
        "TransactionConflict": 22,
        "BootstrapUnavailable": 23,
        "RepairFailedRollbackSucceeded": 24,
        "RollbackFailed": 25,
        "DiagnosticFailure": 26,
        "ShortcutFailure": 27,
        "ElevationRequired": 28,
        "UnexpectedFailure": 30,
    },
    "scripts/Build-AzurPilot.ps1": {
        "Success": 0,
        "PreconditionFailure": 20,
        "ActiveProcess": 21,
        "ConcurrentBuild": 22,
        "BootstrapUnavailable": 23,
        "ExistingEnvironmentBroken": 24,
        "DependencyBuildFailure": 25,
        "AdbFailure": 26,
        "ConfigFailure": 27,
        "ShortcutFailure": 28,
        "ElevationRequired": 29,
        "UnexpectedFailure": 30,
    },
}


class PowerShellContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            path.as_posix(): (ROOT / path).read_text(encoding="utf-8-sig")
            for path in SCRIPT_PATHS
        }

    @staticmethod
    def _disposable_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def test_exit_code_contract_is_stable(self) -> None:
        pattern = re.compile(
            r"^\$script:ExitCode([A-Za-z]+)\s*=\s*(\d+)\s*$",
            re.MULTILINE,
        )
        for relative_path, expected in EXPECTED_EXIT_CODES.items():
            with self.subTest(path=relative_path):
                actual = {
                    name: int(value)
                    for name, value in pattern.findall(self.sources[relative_path])
                }
                self.assertEqual(expected, actual)

    def test_log_format_and_external_output_contract(self) -> None:
        for relative_path in (
            "scripts/Start-AzurPilot.ps1",
            "scripts/Stop-AzurPilot.ps1",
            "scripts/Update-AzurPilot.ps1",
            "scripts/Repair-AzurPilot.ps1",
            "scripts/Build-AzurPilot.ps1",
        ):
            with self.subTest(path=relative_path):
                source = self.sources[relative_path]
                self.assertIn("Get-Date -Format 'yyyy-MM-dd HH:mm:ss'", source)
                supported_formats = (
                    '$line = "[$timestamp] [$Level] $safeMessage"',
                    "$line = '[{0}] [{1}] {2}' -f $timestamp, $Level, $safeMessage",
                )
                self.assertTrue(any(value in source for value in supported_formats))

        self.assertIn(
            "Write-UpdateLog -Level $Level -Message $text",
            self.sources["scripts/Update-AzurPilot.ps1"],
        )
        self.assertIn(
            "Write-RepairLog -Level $Level -Message $message",
            self.sources["scripts/Repair-AzurPilot.ps1"],
        )
        self.assertIn(
            "Write-BuildLog -Level $Level -Message $message",
            self.sources["scripts/Build-AzurPilot.ps1"],
        )
        self.assertIn(
            "$message = '[gui {0}] {1}' -f $StreamName, $Line",
            self.sources["scripts/Start-AzurPilot.ps1"],
        )

    def test_start_and_stop_share_lifecycle_ownership(self) -> None:
        start = self.sources["scripts/Start-AzurPilot.ps1"]
        stop = self.sources["scripts/Stop-AzurPilot.ps1"]
        lifecycle = self.sources["scripts/lib/AzurPilot.Lifecycle.psm1"]

        self.assertIn("Import-Module -Name $lifecycleModulePath", start)
        self.assertIn("Import-Module -Name $lifecycleModulePath", stop)
        self.assertNotIn("function Get-AzurPilotPortOwnershipState", start)
        self.assertNotIn("function Get-AzurPilotPortOwnershipState", stop)
        self.assertIn("function Get-AzurPilotPortOwnershipState", lifecycle)
        self.assertIn("function Get-AzurPilotRepositoryProcessEvidence", lifecycle)
        self.assertIn("AZURPILOT_LIFECYCLE_STOP_EVENT", start)
        self.assertIn("Console.CancelKeyPress += handler", start)
        self.assertIn("args.Cancel = true", start)
        self.assertIn("Send-AzurPilotStopRequest", stop)
        self.assertIn("Stop-AzurPilotOwnedProcessTree", stop)
        self.assertNotIn("Stop-Process", stop)
        self.assertNotIn("taskkill", stop.lower())
        self.assertIn("taskkill.exe", lifecycle)

    def test_start_path_entry_accepts_empty_path_segments(self) -> None:
        source = self.sources["scripts/Start-AzurPilot.ps1"]
        function_match = re.search(
            r"function Add-PathEntry\s*\{(?P<body>.*?)\n\}\n\nfunction Invoke-AzurPilotBackendStart",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(function_match)
        body = function_match.group("body")
        self.assertRegex(
            body,
            r"\[Parameter\(Mandatory\)\]\s*"
            r"\[AllowEmptyString\(\)\]\s*"
            r"\[string\]\$Entry",
        )
        self.assertIn("if ([string]::IsNullOrWhiteSpace($Entry))", body)

    @unittest.skipUnless(os.name == "nt", "требуется Windows")
    def test_start_executes_postgresql_prepare_through_real_wrapper(self) -> None:
        pwsh = shutil.which("pwsh")
        self.assertIsNotNone(pwsh, "PowerShell 7.6.x должен быть доступен в Windows gate")

        windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows"))
        csc_candidates = (
            shutil.which("csc.exe"),
            windows_directory / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
            windows_directory / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
        )
        csc = next(
            (
                Path(str(candidate))
                for candidate in csc_candidates
                if candidate is not None and Path(str(candidate)).is_file()
            ),
            None,
        )
        self.assertIsNotNone(csc, "Системный C# compiler нужен для disposable wsl.exe probe")

        start_source = self.sources["scripts/Start-AzurPilot.ps1"]
        self.assertIn("'dev_tools.postgresql_runtime'", start_source)
        self.assertIn("'prepare'", start_source)
        self.assertIn("-TimeoutMilliseconds 210000", start_source)
        self.assertIn(
            "Production PostgreSQL не прошёл подготовку marker, schema upgrade или app-health.",
            start_source,
        )
        self.assertIn("--property=ActiveState", start_source)
        self.assertIn("function Stop-OwnedPostgreSql", start_source)

        with tempfile.TemporaryDirectory(prefix="azurpilot-start-smoke-") as temporary:
            test_root = Path(temporary)
            shim_directory = test_root / "shim"
            shim_directory.mkdir()
            wsl_source = test_root / "wsl-probe.cs"
            wsl_executable = shim_directory / "wsl.exe"
            wsl_source.write_text(
                "using System;\n"
                "using System.IO;\n"
                "\n"
                "public static class WslProbe\n"
                "{\n"
                "    public static int Main(string[] args)\n"
                "    {\n"
                "        string logPath = Environment.GetEnvironmentVariable(\"AZURPILOT_START_WSL_LOG\");\n"
                "        string statePath = Environment.GetEnvironmentVariable(\"AZURPILOT_START_WSL_STATE\");\n"
                "        if (String.IsNullOrWhiteSpace(logPath) || String.IsNullOrWhiteSpace(statePath))\n"
                "        {\n"
                "            return 97;\n"
                "        }\n"
                "        File.AppendAllText(logPath, String.Join(\"\\u001f\", args) + Environment.NewLine);\n"
                "        int systemctl = Array.IndexOf(args, \"systemctl\");\n"
                "        if (systemctl >= 0 && Array.IndexOf(args, \"show\") >= 0)\n"
                "        {\n"
                "            Console.WriteLine(File.ReadAllText(statePath).Trim());\n"
                "            return 0;\n"
                "        }\n"
                "        if (systemctl >= 0 && Array.IndexOf(args, \"start\") >= 0)\n"
                "        {\n"
                "            File.WriteAllText(statePath, \"active\");\n"
                "            return 0;\n"
                "        }\n"
                "        if (systemctl >= 0 && Array.IndexOf(args, \"stop\") >= 0)\n"
                "        {\n"
                "            File.WriteAllText(statePath, \"inactive\");\n"
                "            return 0;\n"
                "        }\n"
                "        return 0;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            compile_result = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    f"/out:{wsl_executable}",
                    str(wsl_source),
                ],
                cwd=test_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
            self.assertEqual(
                0,
                compile_result.returncode,
                compile_result.stdout + compile_result.stderr,
            )
            self.assertTrue(wsl_executable.is_file())

            def run_start(mode: str, case_name: str, initial_service_state: str):
                repository = test_root / case_name / "Repository With Spaces"
                config_path = repository / "config" / "deploy.yaml"
                dev_tools_path = repository / "dev_tools"
                gui_path = repository / "gui.py"
                prepare_log = test_root / f"{case_name}-prepare.log"
                gui_log = test_root / f"{case_name}-gui.log"
                wsl_log = test_root / f"{case_name}-wsl.log"
                wsl_state = test_root / f"{case_name}-wsl-state.txt"

                config_path.parent.mkdir(parents=True)
                dev_tools_path.mkdir(parents=True)
                wsl_state.write_text(initial_service_state, encoding="utf-8")
                venv_result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "venv",
                        "--without-pip",
                        str(repository / ".venv"),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    0,
                    venv_result.returncode,
                    venv_result.stdout + venv_result.stderr,
                )
                self.assertTrue((repository / ".venv" / "Scripts" / "python.exe").is_file())

                (dev_tools_path / "__init__.py").write_text("", encoding="utf-8")
                (dev_tools_path / "postgresql_runtime.py").write_text(
                    "from __future__ import annotations\n"
                    "\n"
                    "import os\n"
                    "import sys\n"
                    "from pathlib import Path\n"
                    "\n"
                    "log_path = Path(os.environ['AZURPILOT_START_PROBE_LOG'])\n"
                    "with log_path.open('a', encoding='utf-8') as stream:\n"
                    "    stream.write('\\x1f'.join(sys.argv[1:]) + '\\n')\n"
                    "if os.environ.get('AZURPILOT_START_PROBE_MODE') == 'fail-prepare':\n"
                    "    print('Тестовая ошибка prepare', file=sys.stderr)\n"
                    "    raise SystemExit(43)\n",
                    encoding="utf-8",
                )
                gui_path.write_text(
                    "from pathlib import Path\n"
                    "import os\n"
                    "Path(os.environ['AZURPILOT_START_GUI_LOG']).write_text(\n"
                    "    'запущено\\n', encoding='utf-8'\n"
                    ")\n",
                    encoding="utf-8",
                )
                config_path.write_text(
                    "WebuiHost: 127.0.0.1\n"
                    f"WebuiPort: {self._disposable_port()}\n"
                    "EnableReload: false\n",
                    encoding="utf-8",
                )

                environment = os.environ.copy()
                environment["PATH"] = str(shim_directory) + os.pathsep + environment.get(
                    "PATH", ""
                )
                environment["AZURPILOT_START_PROBE_LOG"] = str(prepare_log)
                environment["AZURPILOT_START_PROBE_MODE"] = mode
                environment["AZURPILOT_START_GUI_LOG"] = str(gui_log)
                environment["AZURPILOT_START_WSL_LOG"] = str(wsl_log)
                environment["AZURPILOT_START_WSL_STATE"] = str(wsl_state)

                result = subprocess.run(
                    [
                        str(pwsh),
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(ROOT / "scripts" / "Start-AzurPilot.ps1"),
                        "-RepositoryPath",
                        str(repository),
                        "-NoBrowser",
                        "-StartupTimeoutSeconds",
                        "5",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                wsl_calls = tuple(
                    tuple(line.split("\x1f"))
                    for line in wsl_log.read_text(encoding="utf-8").splitlines()
                )
                return result, prepare_log, gui_log, wsl_calls, wsl_state

            success_result, success_prepare_log, success_gui_log, success_wsl_calls, success_wsl_state = (
                run_start("normal", "success", "inactive")
            )
            self.assertEqual(
                EXPECTED_EXIT_CODES["scripts/Start-AzurPilot.ps1"]["BackendFailure"],
                success_result.returncode,
                success_result.stdout + success_result.stderr,
            )
            self.assertEqual(
                (
                    (
                        "--distribution",
                        "Archlinux",
                        "--user",
                        "root",
                        "--exec",
                        "systemctl",
                        "show",
                        "--property=ActiveState",
                        "--value",
                        "postgresql",
                    ),
                    (
                        "--distribution",
                        "Archlinux",
                        "--user",
                        "root",
                        "--exec",
                        "systemctl",
                        "start",
                        "postgresql",
                    ),
                    (
                        "--distribution",
                        "Archlinux",
                        "--exec",
                        "pg_isready",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "5432",
                        "--timeout",
                        "5",
                    ),
                    (
                        "--distribution",
                        "Archlinux",
                        "--user",
                        "root",
                        "--exec",
                        "systemctl",
                        "stop",
                        "postgresql",
                    ),
                    (
                        "--distribution",
                        "Archlinux",
                        "--user",
                        "root",
                        "--exec",
                        "systemctl",
                        "show",
                        "--property=ActiveState",
                        "--value",
                        "postgresql",
                    ),
                ),
                success_wsl_calls,
            )
            self.assertEqual("inactive", success_wsl_state.read_text(encoding="utf-8"))
            self.assertEqual(
                "prepare\n",
                success_prepare_log.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "запущено\n",
                success_gui_log.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "PostgreSQL 18 запущен текущим Start и будет остановлен после завершения backend.",
                success_result.stdout + success_result.stderr,
            )
            self.assertIn(
                "PostgreSQL 18, запущенный текущим Start, остановлен.",
                success_result.stdout + success_result.stderr,
            )

            failed_result, failed_prepare_log, failed_gui_log, failed_wsl_calls, failed_wsl_state = (
                run_start("fail-prepare", "prepare-failure", "inactive")
            )
            self.assertEqual(
                EXPECTED_EXIT_CODES["scripts/Start-AzurPilot.ps1"]["EnvironmentFailure"],
                failed_result.returncode,
                failed_result.stdout + failed_result.stderr,
            )
            self.assertEqual(success_wsl_calls, failed_wsl_calls)
            self.assertEqual("inactive", failed_wsl_state.read_text(encoding="utf-8"))
            self.assertEqual(
                "prepare\n",
                failed_prepare_log.read_text(encoding="utf-8"),
            )
            self.assertFalse(failed_gui_log.exists())
            self.assertIn(
                "Production PostgreSQL не прошёл подготовку marker, schema upgrade или app-health.",
                failed_result.stdout + failed_result.stderr,
            )

            external_result, external_prepare_log, external_gui_log, external_wsl_calls, external_wsl_state = (
                run_start("normal", "external-active", "active")
            )
            self.assertEqual(
                EXPECTED_EXIT_CODES["scripts/Start-AzurPilot.ps1"]["BackendFailure"],
                external_result.returncode,
                external_result.stdout + external_result.stderr,
            )
            self.assertEqual(
                (
                    success_wsl_calls[0],
                    success_wsl_calls[2],
                ),
                external_wsl_calls,
            )
            self.assertEqual("active", external_wsl_state.read_text(encoding="utf-8"))
            self.assertEqual(
                "prepare\n",
                external_prepare_log.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "запущено\n",
                external_gui_log.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "PostgreSQL 18 уже работал до текущего Start; эта сессия не будет останавливать службу.",
                external_result.stdout + external_result.stderr,
            )

    def test_utf8_text_has_no_mojibake(self) -> None:
        mojibake = ("Р ", "РЎ", "Рџ", "СЂ", "вЂ", "пїЅ", "\ufffd")
        for relative_path, source in self.sources.items():
            for marker in mojibake:
                with self.subTest(path=relative_path, marker=marker):
                    self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
