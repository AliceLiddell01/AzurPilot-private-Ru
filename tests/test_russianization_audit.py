from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dev_tools.russianization_audit import AuditEngine, RESULT_FILENAMES, is_excluded


class RussianizationAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "dev_tools/russianization/results"
        self._write("module/config/i18n/en-US.json", json.dumps({"Menu": {"Home": {"name": "Home", "help": "Open dashboard"}}}, ensure_ascii=False))
        self._write("module/config/i18n/ja-JP.json", json.dumps({"Menu": {"Home": {"name": "ホーム"}}}, ensure_ascii=False))
        self._write(
            "module/webui/app.py",
            "from module.logger import logger\n"
            "def render():\n"
            "    put_text('Known hardcoded UI')\n"
            "    logger.info('Known logger fixture')\n"
            "    logger.info('ADB')\n"
            "    raise RuntimeError('Visible failure')\n",
        )
        self._write("module/ocr/loader.py", "from pathlib import Path\nfor p in Path('assets/ocr').glob('*.png'):\n    print(p)\n")
        self._write("module/device/use_asset.py", "ASSET = 'assets/shared/direct.png'\n")
        self._write_bytes("assets/shared/direct.png", b"direct")
        self._write_bytes("assets/ocr/dynamic.png", b"dynamic")
        self._write_bytes("assets/jp/unreferenced_jp.png", b"jp")
        self._write_bytes("assets/unknown.bin", b"unknown")
        self._write("scripts/Start-AzurPilot.ps1", "Write-Information 'Запуск AzurPilot'\nthrow 'Start failed'\n")
        self._write("README.md", "Кириллица 日本語 English\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_bytes(self, relative: str, data: bytes) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _engine(self) -> AuditEngine:
        return AuditEngine(self.root, self.output)

    def test_check_mode_is_read_only(self) -> None:
        engine = self._engine()
        engine.write()
        before = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(engine.check(), [])
        after = {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_repeated_generation_is_deterministic(self) -> None:
        first = self._engine().build_outputs()
        second = self._engine().build_outputs()
        self.assertEqual(first, second)

    def test_ci_and_stage4_transport_are_excluded(self) -> None:
        self.assertTrue(is_excluded(".github/workflows/lint.yml"))
        self.assertTrue(is_excluded(".github/stage4_regenerate.py"))
        self.assertFalse(is_excluded("module/webui/app.py"))

    def test_all_locale_files_and_key_drift_are_detected(self) -> None:
        locales, missing, extra = self._engine().locale_inventory()
        self.assertEqual([item["locale"] for item in locales], ["en-US", "ja-JP"])
        self.assertIn("module/config/i18n/ja-JP.json", missing)
        self.assertIn("Menu.Home.help", missing["module/config/i18n/ja-JP.json"])
        self.assertIn("module/config/i18n/en-US.json", extra)

    def test_known_hardcoded_ui_fixture_is_detected(self) -> None:
        entries = self._engine().inventory_ui_strings()
        matches = [entry for entry in entries if entry["text"] == "Known hardcoded UI"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["classification"], "user_ui_text")
        self.assertTrue(matches[0]["translation_required"])

    def test_known_logger_fixture_is_detected_and_classified(self) -> None:
        entries = self._engine().inventory_logs()
        match = next(entry for entry in entries if entry["message_or_template"] == "Known logger fixture")
        self.assertEqual(match["first_party_or_external"], "first_party")
        self.assertTrue(match["translation_required"])

    def test_technical_identifier_is_not_required_translation(self) -> None:
        entries = self._engine().inventory_logs()
        match = next(entry for entry in entries if entry["message_or_template"] == "ADB")
        self.assertFalse(match["translation_required"])

    def test_direct_reference_prevents_delete_status(self) -> None:
        assets = {entry["path"]: entry for entry in self._engine().asset_manifest()}
        item = assets["assets/shared/direct.png"]
        self.assertTrue(item["static_references"])
        self.assertEqual(item["decision_status"], "confirmed_keep")
        self.assertFalse(item["deletable_candidate"])

    def test_glob_reference_is_dynamic_reference(self) -> None:
        assets = {entry["path"]: entry for entry in self._engine().asset_manifest()}
        item = assets["assets/ocr/dynamic.png"]
        self.assertTrue(item["dynamic_loader_references"])
        self.assertIn(item["decision_status"], {"confirmed_keep", "probable_keep"})
        self.assertFalse(item["deletable_candidate"])

    def test_unknown_asset_is_not_falsely_safe_to_delete(self) -> None:
        assets = {entry["path"]: entry for entry in self._engine().asset_manifest()}
        item = assets["assets/unknown.bin"]
        self.assertEqual(item["decision_status"], "needs_manual_review")
        self.assertTrue(item["manual_review_required"])
        self.assertFalse(item["deletable_candidate"])

    def test_server_marker_without_evidence_remains_manual_review(self) -> None:
        assets = {entry["path"]: entry for entry in self._engine().asset_manifest()}
        item = assets["assets/jp/unreferenced_jp.png"]
        self.assertEqual(item["decision_status"], "probable_delete_candidate")
        self.assertTrue(item["manual_review_required"])
        self.assertLess(item["confidence"], 0.8)

    def test_utf8_cjk_and_cyrillic_are_preserved(self) -> None:
        outputs = self._engine().build_outputs()
        for data in outputs.values():
            data.decode("utf-8")
        report = outputs["stage4_report.md"].decode("utf-8")
        self.assertIn("аудит русификации", report)

    def test_machine_outputs_have_required_structure(self) -> None:
        outputs = self._engine().build_outputs()
        for filename in RESULT_FILENAMES:
            self.assertIn(filename, outputs)
        for filename in (
            "summary.json", "ui_strings.json", "first_party_logs.json", "asset_manifest.json",
            "locale_dependency_map.json", "terminology.json", "technical_allowlist.json",
            "asset_decisions.json", "en_global_required.json",
        ):
            payload = json.loads(outputs[filename])
            self.assertEqual(payload["schema_version"], 1)
        ui_payload = json.loads(outputs["ui_strings.json"])
        log_payload = json.loads(outputs["first_party_logs.json"])
        asset_payload = json.loads(outputs["asset_manifest.json"])
        self.assertIsInstance(ui_payload["columns"], list)
        self.assertIsInstance(ui_payload["entries"], list)
        self.assertIsInstance(log_payload["columns"], list)
        self.assertIsInstance(log_payload["entries"], list)
        self.assertIsInstance(asset_payload["columns"], list)
        self.assertIsInstance(asset_payload["entries"], list)
        self.assertFalse(asset_payload["full_manifest_committed"])
        self.assertIn("full_manifest_sha256", asset_payload)


if __name__ == "__main__":
    unittest.main()
