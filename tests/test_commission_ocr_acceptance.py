from __future__ import annotations

import unittest
from pathlib import Path

from dev_tools.commission_ocr_acceptance import evaluate_rows


class CommissionOcrAcceptanceTests(unittest.TestCase):
    def test_valid_rows_pass_automatic_evaluation(self) -> None:
        rows = [
            {
                "id": 1,
                "mode": "daily",
                "name": "DAILY RESOURCE EXTRACTION",
                "genre": "daily_resource",
                "valid": True,
                "duration_seconds": 7200,
                "suspicious_gibberish": False,
            },
            {
                "id": 2,
                "mode": "urgent",
                "name": "BIW URGENT COMMISSION",
                "genre": "urgent_cube",
                "valid": True,
                "duration_seconds": 28800,
                "suspicious_gibberish": False,
            },
        ]

        self.assertEqual(evaluate_rows(rows), [])

    def test_invalid_gibberish_rows_fail_closed(self) -> None:
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

    def test_live_runner_is_read_only_for_commission_state(self) -> None:
        source = Path("dev_tools/commission_ocr_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("runner.ui_ensure(page_commission)", source)
        self.assertIn("runner._commission_scan_list()", source)
        self.assertIn('runner._commission_ensure_mode("daily")', source)
        self.assertNotIn("runner.commission_start(", source)
        self.assertNotIn("runner._commission_receive(", source)
        self.assertNotIn("runner._commission_choose(", source)

    def test_manual_confirmation_is_required_for_success(self) -> None:
        source = Path("dev_tools/commission_ocr_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_confirm_rows(rows, args)", source)
        self.assertIn("MATCH ALL", source)
        self.assertIn("user_confirmed_ids", source)


if __name__ == "__main__":
    unittest.main()
