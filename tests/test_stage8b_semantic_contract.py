from __future__ import annotations

import unittest

from dev_tools.stage8b_ocr_log_audit import Stage8BOcrLogAudit
from dev_tools.stage8b_semantic_policy import BLOCKING_METRICS, OCR_SCOPE_PATHS, ROOT


class Stage8BSemanticContractTests(unittest.TestCase):
    def test_stage8b_audit_has_no_blocking_language_findings(self) -> None:
        _outputs, metrics = Stage8BOcrLogAudit().build()
        language_metrics = (
            "stage8b_unresolved", "stage8b_cjk_first_party_remaining",
            "stage8b_english_first_party_remaining", "stage8b_placeholder_mismatches",
            "stage8b_mojibake_findings",
        )
        self.assertEqual(
            {key: metrics[key] for key in language_metrics},
            {key: 0 for key in language_metrics},
        )
        self.assertGreater(metrics["remaining_log_translation_count"], 0)

    def test_runtime_monkey_patch_is_absent(self) -> None:
        for relative in (
            "module/ocr/models.py",
            "module/daemon/ocr_benchmark.py",
            "dev_tools/stage8b_ocr_acceptance.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("install_stage8b_runtime_patches", source, relative)
        self.assertFalse((ROOT / "module/ocr/stage8b_runtime.py").exists())

    def test_stage8b_scope_does_not_claim_later_stage_owners(self) -> None:
        self.assertNotIn("module/campaign/campaign_ocr.py", OCR_SCOPE_PATHS)
        self.assertNotIn("module/os/sea_miles_ocr.py", OCR_SCOPE_PATHS)
        self.assertNotIn("module/device/device.py", OCR_SCOPE_PATHS)

    def test_blocking_metric_names_are_unique(self) -> None:
        self.assertEqual(len(BLOCKING_METRICS), len(set(BLOCKING_METRICS)))


if __name__ == "__main__":
    unittest.main()
