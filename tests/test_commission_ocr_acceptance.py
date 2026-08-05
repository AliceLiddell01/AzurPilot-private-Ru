from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dev_tools.commission_ocr_acceptance import (
    _is_single_blank_scan,
    _scan_mode,
    evaluate_rows,
)


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

    def test_only_one_fully_blank_object_is_empty_tab_sentinel(self) -> None:
        blank = SimpleNamespace(
            valid=False,
            name="",
            genre="",
            duration=timedelta(0),
            suffix_hash="",
        )
        real_but_failed = SimpleNamespace(
            valid=False,
            name="",
            genre="",
            duration=timedelta(hours=1),
            suffix_hash="",
        )

        self.assertTrue(_is_single_blank_scan([blank]))
        self.assertFalse(_is_single_blank_scan([]))
        self.assertFalse(_is_single_blank_scan([blank, blank]))
        self.assertFalse(_is_single_blank_scan([real_but_failed]))

    @patch(
        "dev_tools.commission_ocr_acceptance.COMMISSION_SWITCH.get",
        return_value="daily",
    )
    def test_already_active_daily_tab_is_success(self, switch_get: Mock) -> None:
        runner = Mock()
        runner._commission_ensure_mode.return_value = False
        runner._commission_scan_list.return_value = []

        result = _scan_mode(runner, "daily")

        self.assertEqual(result, [])
        runner._commission_ensure_mode.assert_called_once_with("daily")
        runner._commission_swipe_to_top.assert_called_once_with()
        runner.device.screenshot.assert_not_called()
        switch_get.assert_called_once_with(main=runner)

    def test_live_runner_is_read_only_for_commission_state(self) -> None:
        source = Path("dev_tools/commission_ocr_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("runner.ui_ensure(page_commission)", source)
        self.assertIn("runner._commission_scan_list()", source)
        self.assertIn('_ensure_mode_active(runner, "urgent")', source)
        self.assertIn('_ensure_mode_active(runner, "daily")', source)
        self.assertNotIn("runner.commission_start(", source)
        self.assertNotIn("runner._commission_receive(", source)
        self.assertNotIn("runner._commission_choose(", source)

    def test_empty_urgent_and_rows_require_separate_manual_confirmation(self) -> None:
        source = Path("dev_tools/commission_ocr_acceptance.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("_confirm_empty_urgent(", source)
        self.assertIn("EMPTY URGENT", source)
        self.assertIn("_confirm_rows(rows, args)", source)
        self.assertIn("MATCH ALL", source)
        self.assertIn("user_confirmed_empty_modes", source)
        self.assertIn("user_confirmed_ids", source)


if __name__ == "__main__":
    unittest.main()
