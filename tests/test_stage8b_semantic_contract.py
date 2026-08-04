from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dev_tools.stage8b_model_scope import find_removed_runtime_model_references
from dev_tools.stage8b_ocr_log_audit import Stage8BOcrLogAudit
from dev_tools.stage8b_semantic_policy import (
    BLOCKING_METRICS,
    OCR_SCOPE_PATHS,
    OCR_SCOPE_RULES,
    ROOT,
)


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
