from __future__ import annotations

import unittest

from dev_tools.stage8b_ocr_log_audit import Stage8BOcrLogAudit
from dev_tools.stage8b_semantic_policy import BLOCKING_METRICS


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

    def test_runtime_patch_is_installed_for_direct_and_lazy_ocr_imports(self) -> None:
        from dev_tools.stage8b_semantic_policy import ROOT
        for relative in ("module/ocr/models.py", "module/daemon/ocr_benchmark.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("install_stage8b_runtime_patches", source, relative)

    def test_blocking_metric_names_are_unique(self) -> None:
        self.assertEqual(len(BLOCKING_METRICS), len(set(BLOCKING_METRICS)))


if __name__ == "__main__":
    unittest.main()
