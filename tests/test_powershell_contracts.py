
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
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

        probe_source = r"""
using System;
using System.IO;

public static class AzurPilotStartProbe
{
    public static int Main(string[] args)
    {
        string logPath = Environment.GetEnvironmentVariable("AZURPILOT_START_PROBE_LOG");
        if (String.IsNullOrWhiteSpace(logPath))
        {
            return 97;
        }

        string payload = String.Join("\u001f", args);
        string executable = Path.GetFileName(Environment.GetCommandLineArgs()[0]);
        File.AppendAllText(logPath, executable + "\t" + payload + Environment.NewLine);

        string mode = Environment.GetEnvironmentVariable("AZURPILOT_START_PROBE_MODE");
        if (
            String.Equals(mode, "fail-prepare", StringComparison.Ordinal) &&
            payload.Contains("dev_tools.postgresql_runtime\u001fprepare", StringComparison.Ordinal)
        )
        {
            return 43;
        }

        return 0;
    }
}
""".strip()
        compile_script = (
            "Set-StrictMode -Version Latest\n"
            "$ErrorActionPreference = 'Stop'\n"
            "$PSNativeCommandUseErrorActionPreference = $false\n"
            "$source = @'\n"
            + probe_source
            + "\n'@\n"
            "Add-Type -TypeDefinition $source -Language CSharp "
            "-OutputAssembly $env:AZURPILOT_PROBE_OUTPUT "
            "-OutputType ConsoleApplication -ErrorAction Stop\n"
        )

        with tempfile.TemporaryDirectory(prefix="azurpilot-start-smoke-") as temporary:
            test_root = Path(temporary)
            probe_executable = test_root / "probe.exe"
            compile_environment = os.environ.copy()
            compile_environment["AZURPILOT_PROBE_OUTPUT"] = str(probe_executable)
            compile_result = subprocess.run(
                [
                    str(pwsh),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    compile_script,
                ],
                cwd=ROOT,
                env=compile_environment,
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
            self.assertTrue(probe_executable.is_file())

            shim_directory = test_root / "shim"
            shim_directory.mkdir()
            shutil.copy2(probe_executable, shim_directory / "wsl.exe")

            def run_start(mode: str, case_name: str):
                repository = test_root / case_name / "Repository With Spaces"
                python_path = repository / ".venv" / "Scripts" / "python.exe"
                config_path = repository / "config" / "deploy.yaml"
                gui_path = repository / "gui.py"
                python_path.parent.mkdir(parents=True)
                config_path.parent.mkdir(parents=True)
                shutil.copy2(probe_executable, python_path)
                gui_path.write_text("# disposable Start smoke\n", encoding="utf-8")
                config_path.write_text(
                    "WebuiHost: 127.0.0.1\n"
                    f"WebuiPort: {self._disposable_port()}\n"
                    "EnableReload: false\n",
                    encoding="utf-8",
                )

                probe_log = test_root / f"{case_name}.log"
                environment = os.environ.copy()
                environment["PATH"] = str(shim_directory) + os.pathsep + environment.get(
                    "PATH", ""
                )
                environment["AZURPILOT_START_PROBE_LOG"] = str(probe_log)
                environment["AZURPILOT_START_PROBE_MODE"] = mode

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
                self.assertTrue(probe_log.is_file(), result.stdout + result.stderr)
                calls = []
                for line in probe_log.read_text(encoding="utf-8").splitlines():
                    executable, separator, payload = line.partition("\t")
                    self.assertEqual("\t", separator)
                    arguments = tuple(payload.split("\x1f")) if payload else ()
                    calls.append((executable.lower(), arguments))
                return result, calls, gui_path

            success_result, success_calls, success_gui = run_start("normal", "success")
            self.assertEqual(
                EXPECTED_EXIT_CODES["scripts/Start-AzurPilot.ps1"]["BackendFailure"],
                success_result.returncode,
                success_result.stdout + success_result.stderr,
            )

            health_index = next(
                index
                for index, (executable, arguments) in enumerate(success_calls)
                if executable == "python.exe" and arguments == ("-c", "raise SystemExit(0)")
            )
            systemctl_index = next(
                index
                for index, (executable, arguments) in enumerate(success_calls)
                if executable == "wsl.exe"
                and arguments[-4:] == ("systemctl", "start", "postgresql")[-4:]
            )
            pg_isready_index = next(
                index
                for index, (executable, arguments) in enumerate(success_calls)
                if executable == "wsl.exe" and "pg_isready" in arguments
            )
            prepare_index = next(
                index
                for index, (executable, arguments) in enumerate(success_calls)
                if executable == "python.exe"
                and arguments
                == ("-X", "utf8", "-m", "dev_tools.postgresql_runtime", "prepare")
            )
            gui_index = next(
                index
                for index, (executable, arguments) in enumerate(success_calls)
                if executable == "python.exe" and arguments == (str(success_gui),)
            )
            self.assertLess(health_index, systemctl_index)
            self.assertLess(systemctl_index, pg_isready_index)
            self.assertLess(pg_isready_index, prepare_index)
            self.assertLess(prepare_index, gui_index)

            failed_result, failed_calls, failed_gui = run_start(
                "fail-prepare", "prepare-failure"
            )
            self.assertEqual(
                EXPECTED_EXIT_CODES["scripts/Start-AzurPilot.ps1"]["EnvironmentFailure"],
                failed_result.returncode,
                failed_result.stdout + failed_result.stderr,
            )
            failed_output = failed_result.stdout + failed_result.stderr
            self.assertIn(
                "Production PostgreSQL не прошёл подготовку marker, schema upgrade или app-health.",
                failed_output,
            )
            self.assertTrue(
                any(
                    executable == "python.exe"
                    and arguments
                    == ("-X", "utf8", "-m", "dev_tools.postgresql_runtime", "prepare")
                    for executable, arguments in failed_calls
                )
            )
            self.assertFalse(
                any(
                    executable == "python.exe" and arguments == (str(failed_gui),)
                    for executable, arguments in failed_calls
                )
            )
            self.assertIn(
                "TimeoutMilliseconds = 210000",
                self.sources["scripts/Start-AzurPilot.ps1"],
            )

    def test_utf8_text_has_no_mojibake(self) -> None:
        mojibake = ("Р ", "РЎ", "Рџ", "СЂ", "вЂ", "пїЅ", "\ufffd")
        for relative_path, source in self.sources.items():
            for marker in mojibake:
                with self.subTest(path=relative_path, marker=marker):
                    self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()