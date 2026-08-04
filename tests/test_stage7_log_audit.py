from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dev_tools.stage7_gui_contract import analyze_gui_contract, extract_gui_inventory
from dev_tools.stage7_log_audit import (
    COLUMNS,
    STAGE8B_OCR_GUARD_IDENTIFIER,
    Stage7LogAudit,
    _classify,
)


class Stage7LogAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = Stage7LogAudit()
        cls.outputs, cls.metrics = cls.audit.build()
        payload = json.loads(cls.outputs["scope.json"])
        cls.scope = [
            dict(zip(payload["columns"], row, strict=True))
            for row in payload["entries"]
        ]

    def test_outputs_are_generated_diagnostics(self) -> None:
        self.assertEqual(set(self.outputs), {"scope.json", "metrics.json", "report.md"})
        self.assertNotIn("dev_tools/russianization/results", str(self.audit.write.__defaults__))
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            written_metrics = self.audit.write(output_dir)
            self.assertEqual(written_metrics, self.metrics)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {"scope.json", "metrics.json", "report.md"},
            )

    def test_scope_has_point_specific_identifiers_and_evidence(self) -> None:
        identities = set()
        for entry in self.scope:
            with self.subTest(path=entry["path"], identifier=entry["stable_identifier"]):
                self.assertEqual(set(entry), set(COLUMNS))
                self.assertNotIn("*", entry["path"])
                self.assertFalse(entry["path"].endswith("/"))
                identifier = entry["stable_identifier"]
                if identifier == STAGE8B_OCR_GUARD_IDENTIFIER:
                    self.assertEqual(entry["path"], "module/config/config.py")
                    self.assertEqual(entry["stage_owner"], "stage8b")
                else:
                    self.assertRegex(identifier, r"^log-call:\d{4}$")
                self.assertTrue(entry["runtime_owner"].strip())
                self.assertTrue(entry["evidence"].strip())
                identity = (entry["path"], identifier)
                self.assertNotIn(identity, identities)
                identities.add(identity)

    def test_ordinary_english_stage7_message_is_unresolved(self) -> None:
        row = {
            "path": "module/webui/fixture.py",
            "message_or_template": "Failed to start worker. Please check settings.",
            "first_party_or_external": "first_party",
            "source_kind": "python_call",
            "translation_required": True,
        }
        classification, required, _ = _classify(row, "stage7")
        self.assertEqual(classification, "stage7_first_party_message")
        self.assertTrue(required)

    def test_raw_external_payload_is_not_translated(self) -> None:
        row = {
            "path": "module/webui/fixture.py",
            "message_or_template": "result.returncode",
            "first_party_or_external": "external_raw",
            "source_kind": "python_call",
            "translation_required": False,
        }
        classification, required, _ = _classify(row, "stage7")
        self.assertEqual(classification, "raw_external_payload")
        self.assertFalse(required)

    def test_stage8_transfers_are_specific(self) -> None:
        transfers = [
            entry for entry in self.scope
            if str(entry["stage_owner"]).startswith("stage8")
        ]
        self.assertTrue(transfers)
        for entry in transfers:
            with self.subTest(path=entry["path"], identifier=entry["stable_identifier"]):
                self.assertTrue(entry["runtime_owner"].startswith("Stage 8"))
                self.assertTrue(entry["evidence"].strip())


    def test_cjk_sentence_with_pid_is_unresolved(self) -> None:
        row = {
            "path": "gui.py",
            "message_or_template": "[GUI] 正在停止服务进程 (PID: {pid})...",
            "first_party_or_external": "first_party",
            "source_kind": "python_call",
            "translation_required": True,
        }
        classification, required, _ = _classify(row, "stage7")
        self.assertEqual(classification, "stage7_first_party_message")
        self.assertTrue(required)

    def test_mixed_cjk_network_sentence_is_unresolved(self) -> None:
        row = {
            "path": "gui.py",
            "message_or_template": "[GUI] WebUI 同时监听 IPv4 {v4} 与 IPv6 {v6}",
            "first_party_or_external": "first_party",
            "source_kind": "python_call",
            "translation_required": True,
        }
        classification, required, _ = _classify(row, "stage7")
        self.assertEqual(classification, "stage7_first_party_message")
        self.assertTrue(required)

    def test_plain_technical_identifiers_remain_technical(self) -> None:
        for text in ("SSL", "PID"):
            with self.subTest(text=text):
                row = {
                    "path": "gui.py",
                    "message_or_template": text,
                    "first_party_or_external": "first_party",
                    "source_kind": "python_call",
                    "translation_required": False,
                }
                classification, required, _ = _classify(row, "stage7")
                self.assertEqual(classification, "technical_identifier")
                self.assertFalse(required)

    def test_gui_contract_detects_old_untranslated_message(self) -> None:
        source = """
def run(pid):
    logger.info(f"[GUI] 正在停止服务进程 (PID: {pid})...")
"""
        result = analyze_gui_contract(source, source, base_sha="0" * 40)
        self.assertEqual(result["metrics"]["stage7_gui_unresolved"], 1)
        self.assertEqual(
            result["metrics"]["stage7_gui_cjk_first_party_remaining"], 1
        )

    def test_gui_identifiers_ignore_unrelated_statement_insertion(self) -> None:
        before = """
def run(pid):
    logger.info(f"Запуск (PID: {pid})")
    logger.warning("Повтор")
"""
        after = """
def run(pid):
    unrelated = 1
    logger.info(f"Запуск (PID: {pid})")
    logger.warning("Повтор")
"""
        before_rows, _ = extract_gui_inventory(before)
        after_rows, _ = extract_gui_inventory(after)
        self.assertEqual(
            [row.semantic_identifier for row in before_rows],
            [row.semantic_identifier for row in after_rows],
        )

    def test_gui_contract_allows_only_translated_literals(self) -> None:
        base = """
def run(pid):
    logger.warning(f"Stopping service (PID: {pid})")
"""
        head = """
def run(pid):
    logger.warning(f"Остановка службы (PID: {pid})")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["metrics"]["stage7_gui_translated"], 1)

    def test_gui_contract_rejects_control_flow_change(self) -> None:
        base = """
def run(pid):
    logger.info(f"Запуск (PID: {pid})")
"""
        head = """
def run(pid):
    if pid:
        logger.info(f"Запуск (PID: {pid})")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertGreater(
            result["metrics"]["stage7_gui_control_flow_mismatches"], 0
        )

    def test_gui_contract_rejects_message_replaced_by_technical_token(self) -> None:
        base = """
def run(pid):
    logger.warning(f"Stopping service (PID: {pid})")
"""
        head = """
def run(pid):
    logger.warning(f"SSL (PID: {pid})")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertGreater(result["metrics"]["stage7_gui_unresolved"], 0)

    def test_gui_contract_rejects_deleted_first_party_text(self) -> None:
        base = """
def run():
    logger.warning("Dependency sync service is not running")
"""
        head = """
def run():
    logger.warning("")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertGreater(result["metrics"]["stage7_gui_unresolved"], 0)

    def test_gui_contract_rejects_mixed_ordinary_english(self) -> None:
        base = """
def run():
    logger.warning("Dependency sync service is not running")
"""
        head = """
def run():
    logger.warning("Служба dependency sync service не запущена")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertEqual(
            result["metrics"]["stage7_gui_english_first_party_remaining"], 1
        )

    def test_gui_contract_allows_russian_with_technical_tokens(self) -> None:
        base = """
def run(pid):
    logger.warning(f"[GUI] WebUI worker failed (PID: {pid})")
"""
        head = """
def run(pid):
    logger.warning(f"[GUI] Не удалось завершить worker WebUI (PID: {pid})")
"""
        result = analyze_gui_contract(base, head, base_sha="0" * 40)
        self.assertEqual(result["errors"], [])

    def test_broad_gui_stable_policy_is_removed(self) -> None:
        policy = Path(__file__).resolve().parents[1] / "dev_tools/stage7_gui_stable_policy.py"
        self.assertFalse(policy.exists())

    def test_metrics_include_semantic_invariants(self) -> None:
        expected = {
            "stage7_unresolved",
            "stage7_placeholder_mismatches",
            "stage7_severity_mismatches",
            "stage7_sequence_mismatches",
            "stage7_raw_payload_violations",
            "stage7_unknown_classifications",
            "stage7_invalid_stage8_transfers",
            "stage7_mojibake_findings",
        }
        self.assertTrue(expected.issubset(self.metrics))
        self.assertEqual(len(self.metrics["base_sha"]), 40)


class Stage7DeveloperFixtureRegressionTests(unittest.TestCase):
    def test_escaped_path_example_is_developer_output(self) -> None:
        from dev_tools.stage7_log_audit import _owner

        self.assertEqual(
            _owner(
                "module/logger.py",
                r"E:/path\\to/alas/alas.exe, /root/alas/, ./relative/path/log.txt",
            ),
            "developer",
        )


if __name__ == "__main__":
    unittest.main()
