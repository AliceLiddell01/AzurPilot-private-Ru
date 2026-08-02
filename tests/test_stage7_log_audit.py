from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dev_tools.stage7_log_audit import (
    COLUMNS,
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
                self.assertRegex(entry["stable_identifier"], r"^log-call:\d{4}$")
                self.assertTrue(entry["runtime_owner"].strip())
                self.assertTrue(entry["evidence"].strip())
                identity = (entry["path"], entry["stable_identifier"])
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


if __name__ == "__main__":
    unittest.main()
