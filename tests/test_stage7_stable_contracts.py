from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from dev_tools.stage7_gui_contract import GUI_BLOCKING_METRICS, build_gui_contract


ROOT = Path(__file__).resolve().parents[1]
SEA_MILES_OCR_PATH = "module/os/sea_miles_ocr.py"
SEA_MILES_BASE_WARNING = (
    'logger.warning(f"[大世界-里程] 异常的海域里程: {result}")'
)
SEA_MILES_HEAD_WARNING = (
    'logger.warning(f"[Operation Siren — OCR] Недопустимое значение Sea Miles: {result}")'
)
SEA_MILES_WARNING_MARKER = 'logger.warning(f"<STAGE8B_OCR_MESSAGE>: {result}")'
OPSI_OCR_NAMESPACE_DELTAS = {
    "module/os/map_operation.py": (
        (
            "ocr = Ocr(MAP_NAME, lang='ppocr_v6', letter=(206, 223, 247), "
            "threshold=96, name='OCR_OS_MAP_NAME')",
            "ocr = Ocr(MAP_NAME, lang='azur_lane', letter=(206, 223, 247), "
            "threshold=96, name='OCR_OS_MAP_NAME')",
        ),
    ),
    "module/os_handler/action_point.py": (
        (
            "] , letter=(231, 235, 239), lang=\"cnocr\", "
            "name='OCR_OS_ADAPTABILITY')",
            "] , letter=(231, 235, 239), lang=\"azur_lane\", "
            "name='OCR_OS_ADAPTABILITY')",
        ),
        (
            "ACTION_POINT_BUY_REMAIN, letter=(148, 247, 99), lang='cnocr', "
            "name='OCR_ACTION_POINT_BUY_REMAIN')",
            "ACTION_POINT_BUY_REMAIN, letter=(148, 247, 99), lang='azur_lane', "
            "name='OCR_ACTION_POINT_BUY_REMAIN')",
        ),
        (
            "ACTION_POINT_BUY_REMAIN, letter=(255, 255, 255), lang='cnocr', "
            "name='OCR_ACTION_POINT_BUY_REMAIN')",
            "ACTION_POINT_BUY_REMAIN, letter=(255, 255, 255), lang='azur_lane', "
            "name='OCR_ACTION_POINT_BUY_REMAIN')",
        ),
    ),
}


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

    def test_post_divergence_webui_supervisor_allows_translation_only_delta(self) -> None:
        self.assertIn("gui.py", self.changed)
        base_sha = _git("rev-parse", "--verify", self.base_ref).strip()
        _, metrics, errors = build_gui_contract(ROOT, base_sha)
        self.assertEqual(errors, [])
        for key in GUI_BLOCKING_METRICS:
            with self.subTest(metric=key):
                self.assertEqual(metrics[key], 0)

        source = (ROOT / "gui.py").read_text(encoding="utf-8")
        self.assertIn("recover_orphaned_workers", source)
        self.assertIn("EnableReload", source)
        self.assertIn("DEPENDENCY_SYNC_RESPONSE_TIMEOUT", source)
        self.assertIn("worker_registry.process_matches", source)

    def test_broad_gui_stable_policy_is_not_tracked(self) -> None:
        tracked = set(_git("ls-files").splitlines())
        self.assertNotIn("dev_tools/stage7_gui_stable_policy.py", tracked)

    def test_operation_siren_data_logger_implementation_is_unchanged(self) -> None:
        forbidden_prefixes = (
            "module/config/opsi_data_logger.py",
            "module/os",
            "module/os_handler",
            "module/os_shop",
            "module/os_tasks",
            "tests/test_opsi_data_logger",
        )
        allowed_ocr_paths = set(OPSI_OCR_NAMESPACE_DELTAS)
        changed_contract_files = sorted(
            path
            for path in self.changed
            if path != SEA_MILES_OCR_PATH
            and path not in allowed_ocr_paths
            and (
                path == forbidden_prefixes[0]
                or any(path.startswith(prefix) for prefix in forbidden_prefixes[1:])
            )
        )
        self.assertEqual(changed_contract_files, [])

        for path in sorted(self.changed & allowed_ocr_paths):
            base_source = _git("show", f"{self.base_ref}:{path}")
            head_source = (ROOT / path).read_text(encoding="utf-8")
            for index, (base_fragment, head_fragment) in enumerate(
                OPSI_OCR_NAMESPACE_DELTAS[path]
            ):
                marker = f"<STAGE8B_OCR_NAMESPACE_{index}>"
                self.assertEqual(base_source.count(base_fragment), 1, path)
                self.assertEqual(head_source.count(head_fragment), 1, path)
                base_source = base_source.replace(base_fragment, marker)
                head_source = head_source.replace(head_fragment, marker)
            self.assertEqual(base_source, head_source, path)

        if SEA_MILES_OCR_PATH in self.changed:
            base_source = _git("show", f"{self.base_ref}:{SEA_MILES_OCR_PATH}")
            head_source = (ROOT / SEA_MILES_OCR_PATH).read_text(encoding="utf-8")
            self.assertEqual(base_source.count(SEA_MILES_BASE_WARNING), 1)
            self.assertEqual(head_source.count(SEA_MILES_HEAD_WARNING), 1)
            self.assertEqual(
                base_source.replace(SEA_MILES_BASE_WARNING, SEA_MILES_WARNING_MARKER),
                head_source.replace(SEA_MILES_HEAD_WARNING, SEA_MILES_WARNING_MARKER),
            )

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
        self.assertIn(
            "STAGE7_BASE_REF: ${{ github.event.pull_request.base.sha || 'origin/personal/stable' }}",
            workflow,
        )
        self.assertIn('--base-ref "$STAGE7_BASE_REF"', workflow)
        self.assertNotIn("continue-on-error: true\n        run: uv run python -m dev_tools.verify_stage7", workflow)


if __name__ == "__main__":
    unittest.main()
