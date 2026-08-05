from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from dev_tools.commission_ocr_acceptance import evaluate_rows
from dev_tools.stage8b_model_scope import find_removed_runtime_model_references
from dev_tools.stage8b_ocr_log_audit import Stage8BOcrLogAudit
from dev_tools.stage8b_semantic_policy import (
    BLOCKING_METRICS,
    OCR_SCOPE_PATHS,
    OCR_SCOPE_RULES,
    ROOT,
)
from module.ocr.global_english import GlobalEnglishOcr, should_use_general_english


class Stage8BSemanticContractTests(unittest.TestCase):
    def test_stage8b_audit_has_no_blocking_language_findings(self) -> None:
        _outputs, metrics = Stage8BOcrLogAudit().build()
        language_metrics = (
            "stage8b_unresolved",
            "stage8b_cjk_first_party_remaining",
            "stage8b_english_first_party_remaining",
            "stage8b_placeholder_mismatches",
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

    def test_external_scope_is_limited_to_ocr_owned_functions(self) -> None:
        expected_external = {
            "module/campaign/campaign_ocr.py",
            "module/os/sea_miles_ocr.py",
            "module/device/device.py",
        }
        self.assertTrue(expected_external.issubset(OCR_SCOPE_PATHS))
        self.assertEqual(set(OCR_SCOPE_RULES), expected_external)
        for path, owners in OCR_SCOPE_RULES.items():
            with self.subTest(path=path):
                self.assertIsNotNone(owners)
                self.assertTrue(owners)
                self.assertTrue(all("ocr" in owner.casefold() for owner in owners))

    def test_blocking_metric_names_are_unique(self) -> None:
        self.assertEqual(len(BLOCKING_METRICS), len(set(BLOCKING_METRICS)))

    def test_unconstrained_english_text_uses_general_ppocr(self) -> None:
        self.assertTrue(should_use_general_english(None))
        self.assertTrue(
            should_use_general_english("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        )
        self.assertFalse(should_use_general_english("0123456789:IDSB"))

    def test_global_router_keeps_numeric_ocr_compact(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()
        router.compact.atomic_ocr_for_single_lines.return_value = ["01:30:00"]

        result = router.atomic_ocr_for_single_lines(
            [object()],
            "0123456789:IDSB",
        )

        self.assertEqual(result, ["01:30:00"])
        router.compact.atomic_ocr_for_single_lines.assert_called_once()
        router.text.atomic_ocr_for_single_lines.assert_not_called()

    def test_global_router_uses_general_pipeline_for_detection(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()
        router.text.det.return_value = [("SIMULATION", [], 0.99)]

        result = router.det(object())

        self.assertEqual(result[0][0], "SIMULATION")
        router.text.det.assert_called_once()
        router.compact.det.assert_not_called()

    def test_commission_acceptance_rejects_observed_gibberish(self) -> None:
        rows = [
            {
                "id": 1,
                "mode": "daily",
                "name": "A1R::8XM861",
                "genre": "",
                "valid": False,
                "duration_seconds": 7200,
                "suspicious_gibberish": True,
            },
            {
                "id": 2,
                "mode": "urgent",
                "name": "MM",
                "genre": "",
                "valid": False,
                "duration_seconds": 0,
                "suspicious_gibberish": False,
            },
        ]

        findings = evaluate_rows(rows)

        self.assertTrue(any("Commission.valid=False" in item for item in findings))
        self.assertTrue(any("тип комиссии не классифицирован" in item for item in findings))
        self.assertTrue(any("OCR-мусор" in item for item in findings))
        self.assertTrue(any("длительность не распознана" in item for item in findings))

    def test_commission_acceptance_is_live_read_only_and_manual(self) -> None:
        source = (ROOT / "dev_tools/commission_ocr_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("runner.ui_ensure(page_commission)", source)
        self.assertIn("runner._commission_scan_list()", source)
        self.assertIn('runner._commission_ensure_mode("daily")', source)
        self.assertIn("_confirm_rows(rows, args)", source)
        self.assertIn("MATCH ALL", source)
        self.assertNotIn("runner.commission_start(", source)
        self.assertNotIn("runner._commission_receive(", source)
        self.assertNotIn("runner._commission_choose(", source)

    def test_removed_runtime_model_scan_covers_en_and_common_code(self) -> None:
        source = """
class Demo:
    @Config.when(SERVER="en")
    def active(self):
        return Ocr(None, lang="ppocr_v6")

    @Config.when(SERVER="jp")
    def foreign(self):
        return Ocr(None, lang="jp")

def common():
    return OCR_MODEL.tw
"""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            module_path = root / "module"
            module_path.mkdir()
            (module_path / "sample.py").write_text(source, encoding="utf-8")

            findings = find_removed_runtime_model_references(root)

        self.assertEqual(
            [(item["model"], item["owner"]) for item in findings],
            [("ppocr_v6", "Demo.active"), ("tw", "common")],
        )

    def test_removed_runtime_model_scan_ignores_foreign_only_handlers(self) -> None:
        source = """
class Demo:
    @Config.when(SERVER="jp")
    def japanese(self):
        return Ocr(None, lang="jp")

    @Config.when(SERVER="tw")
    def traditional_chinese(self):
        return Ocr(None, lang="tw")

    @Config.when(SERVER=None)
    def chinese(self):
        return Ocr(None, lang="cnocr")
"""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            module_path = root / "module"
            module_path.mkdir()
            (module_path / "sample.py").write_text(source, encoding="utf-8")

            findings = find_removed_runtime_model_references(root)

        self.assertEqual(findings, [])

    def test_removed_runtime_model_scan_understands_module_server_branches(self) -> None:
        source = """
import module.config.server as server

if server.server == "jp":
    JP_ONLY = Ocr(None, lang="jp")
else:
    ACTIVE = Ocr(None, lang="ppocr_v6")

if server.server != "jp":
    COMMON_EN = OCR_MODEL.tw
else:
    FOREIGN = Ocr(None, lang="cnocr")
"""
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            module_path = root / "module"
            module_path.mkdir()
            (module_path / "sample.py").write_text(source, encoding="utf-8")

            findings = find_removed_runtime_model_references(root)

        self.assertEqual(
            [(item["model"], item["line"]) for item in findings],
            [("ppocr_v6", 7), ("tw", 10)],
        )

    def test_en_runtime_has_no_removed_model_references(self) -> None:
        self.assertEqual(find_removed_runtime_model_references(), [])


if __name__ == "__main__":
    unittest.main()
