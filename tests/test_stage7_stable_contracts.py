from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


class Stage7StableContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_ref = os.environ.get("STAGE7_BASE_REF", "origin/personal/stable")
        cls.changed = set(
            filter(None, _git("diff", "--name-only", f"{cls.base_ref}..HEAD").splitlines())
        )

    def test_post_divergence_webui_supervisor_is_not_replaced(self) -> None:
        self.assertNotIn("gui.py", self.changed)
        source = (ROOT / "gui.py").read_text(encoding="utf-8")
        self.assertIn("recover_orphaned_workers", source)
        self.assertIn("EnableReload", source)

    def test_operation_siren_data_logger_implementation_is_unchanged(self) -> None:
        forbidden_prefixes = (
            "module/config/opsi_data_logger.py",
            "module/os",
            "module/os_handler",
            "module/os_shop",
            "module/os_tasks",
            "tests/test_opsi_data_logger",
        )
        changed_contract_files = sorted(
            path
            for path in self.changed
            if path == forbidden_prefixes[0]
            or any(path.startswith(prefix) for prefix in forbidden_prefixes[1:])
        )
        self.assertEqual(changed_contract_files, [])

        implementation = (ROOT / "module/config/opsi_data_logger.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DATA_LOGGER_NAME = "Operation Siren Data Logger"', implementation)
        self.assertIn("DataLoggerPurchaseEvidence", implementation)
        self.assertIn("DATA_LOGGER_MAX_FAILURES_PER_CYCLE = 5", implementation)
        self.assertIn("DATA_LOGGER_EVIDENCE_CYCLE_KEY", implementation)

    def test_config_keeps_confirmed_activation_contract(self) -> None:
        source = (ROOT / "module/config/config.py").read_text(encoding="utf-8")
        self.assertIn(
            "from module.config.opsi_data_logger import data_logger_is_active_from_data",
            source,
        )
        self.assertIn("return data_logger_is_active_from_data(self.data)", source)

    def test_stage7_does_not_restore_tracked_generated_snapshots(self) -> None:
        tracked = set(_git("ls-files").splitlines())
        rejected = {
            "dev_tools/russianization/results/stage7_log_scope.json",
            "dev_tools/russianization/results/stage7_metrics.json",
            "dev_tools/russianization/results/stage7_report.md",
            "tests/fixtures/stage7_webui_traceback/fingerprint.json",
            "tests/fixtures/stage7_webui_traceback/1366x768-dark.png",
            "tests/fixtures/stage7_webui_traceback/1366x768-light.png",
        }
        self.assertTrue(rejected.isdisjoint(tracked))

    def test_ci_retains_independent_security_and_generator_gates(self) -> None:
        workflow = (ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8")
        for job in (
            "functional-tests:",
            "browser-validation:",
            "generated-checks:",
            "repository-audit:",
            "powershell-validation:",
        ):
            with self.subTest(job=job):
                self.assertIn(job, workflow)
        self.assertIn("Run specialized Gitleaks scan", workflow)
        self.assertIn("Verify deterministic and idempotent generators", workflow)
        self.assertIn("Run Stage 7 semantic verifier", workflow)
        self.assertNotIn("continue-on-error: true\n        run: uv run python -m dev_tools.verify_stage7", workflow)


if __name__ == "__main__":
    unittest.main()
