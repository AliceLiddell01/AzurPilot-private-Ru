from __future__ import annotations

import unittest
from unittest.mock import Mock

from module.ocr.global_english import GlobalEnglishOcr, should_use_general_english


class GlobalEnglishOcrRoutingTests(unittest.TestCase):
    def test_unconstrained_text_uses_general_english(self) -> None:
        self.assertTrue(should_use_general_english(None))

    def test_real_letters_use_general_english(self) -> None:
        self.assertTrue(should_use_general_english("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

    def test_numeric_wrappers_keep_compact_model(self) -> None:
        self.assertFalse(should_use_general_english("0123456789IDSB"))
        self.assertFalse(should_use_general_english("0123456789/IDSB"))
        self.assertFalse(should_use_general_english("0123456789:IDSB"))

    def test_router_delegates_unconstrained_ocr_to_text_model(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()
        router.text.ocr.return_value = "DAILY RESOURCE EXTRACTION"

        result = router.ocr(object())

        self.assertEqual(result, "DAILY RESOURCE EXTRACTION")
        router.text.ocr.assert_called_once()
        router.compact.ocr.assert_not_called()

    def test_router_keeps_numeric_candidate_alphabet_on_compact_model(self) -> None:
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

    def test_detection_always_uses_general_text_pipeline(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()
        router.text.det.return_value = [("SIMULATION", [], 0.99)]

        result = router.det(object())

        self.assertEqual(result[0][0], "SIMULATION")
        router.text.det.assert_called_once()
        router.compact.det.assert_not_called()


if __name__ == "__main__":
    unittest.main()
