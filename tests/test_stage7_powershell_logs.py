from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATHS = (
    Path("scripts/Start-AzurPilot.ps1"),
    Path("scripts/Update-AzurPilot.ps1"),
    Path("scripts/Repair-AzurPilot.ps1"),
    Path("scripts/Build-AzurPilot.ps1"),
    Path("scripts/lib/AzurPilot.Shortcut.psm1"),
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


class Stage7PowerShellLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = {
            path.as_posix(): (ROOT / path).read_text(encoding="utf-8-sig")
            for path in SCRIPT_PATHS
        }

    def test_operational_messages_are_russian(self) -> None:
        forbidden_fragments = (
            "abandoned Repair mutex",
            "abandoned Start mutex",
            "Backend PID",
            "Bootstrap cache",
            "Dependency journal",
            "dependency transaction",
            "Project Python",
            "Project ADB",
            "Repair transaction",
            "Shortcut migration",
            "Start supervisor",
            "health check",
            "partial .venv",
            "shortcut diagnostic",
            "version check",
        )
        combined = "\n".join(self.sources.values())
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, combined)

        required_fragments = (
            "Мьютекс Start",
            "Журнал транзакции зависимостей",
            "Транзакция Repair завершена",
            "Окружение проекта исправно",
            "Перенос ярлыка завершился ошибкой",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, combined)

    def test_exit_code_contract_is_unchanged(self) -> None:
        pattern = re.compile(r"^\$script:ExitCode([A-Za-z]+)\s*=\s*(\d+)\s*$", re.MULTILINE)
        for relative_path, expected in EXPECTED_EXIT_CODES.items():
            with self.subTest(path=relative_path):
                actual = {name: int(value) for name, value in pattern.findall(self.sources[relative_path])}
                self.assertEqual(expected, actual)

    def test_log_format_and_raw_external_output_contract_are_preserved(self) -> None:
        for relative_path in (
            "scripts/Start-AzurPilot.ps1",
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
                self.assertTrue(any(log_format in source for log_format in supported_formats))

        self.assertIn("Write-UpdateLog -Level $Level -Message $text", self.sources["scripts/Update-AzurPilot.ps1"])
        self.assertIn("Write-RepairLog -Level $Level -Message $message", self.sources["scripts/Repair-AzurPilot.ps1"])
        self.assertIn("Write-BuildLog -Level $Level -Message $message", self.sources["scripts/Build-AzurPilot.ps1"])
        self.assertIn("$message = '[gui {0}] {1}' -f $StreamName, $Line", self.sources["scripts/Start-AzurPilot.ps1"])

    def test_utf8_text_has_no_mojibake(self) -> None:
        mojibake = ("Р ", "РЎ", "Рџ", "СЂ", "вЂ", "пїЅ", "\ufffd")
        for relative_path, source in self.sources.items():
            for marker in mojibake:
                with self.subTest(path=relative_path, marker=marker):
                    self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
