from __future__ import annotations

import unittest
from unittest.mock import Mock

from module.ocr.global_english import GlobalEnglishOcr, should_use_general_english


class GlobalEnglishOcrRoutingTests(unittest.TestCase):
    def test_audited_commission_name_uses_general_english(self) -> None:
        self.assertTrue(should_use_general_english(None, name="COMMISSION"))

    def test_dock_ship_name_uses_general_english(self) -> None:
        self.assertTrue(should_use_general_english(None, name="DOCK_SHIP_NAME"))

    def test_dock_level_digit_proof_uses_general_english(self) -> None:
        self.assertTrue(
            should_use_general_english(
                "0123456789IDSB",
                name="DOCK_LEVEL_DIGIT_PROOF_96",
                recognizer_type="Digit",
            )
        )

    def test_dock_primary_level_name_keeps_compact_model(self) -> None:
        self.assertFalse(
            should_use_general_english(
                "0123456789IDSB",
                name="DOCK_LEVEL_OCR",
                recognizer_type="Digit",
            )
        )

    def test_audited_zone_name_uses_general_english(self) -> None:
        self.assertTrue(should_use_general_english(None, name="OCR_OS_MAP_NAME"))

    def test_unlisted_unconstrained_request_keeps_compact_model(self) -> None:
        self.assertFalse(should_use_general_english(None, name="UNLISTED_OCR"))

    def test_special_font_numeric_wrapper_uses_general_english(self) -> None:
        self.assertTrue(
            should_use_general_english(
                "0123456789:IDSB",
                name="OCR_TRANSPORT_TIME",
                recognizer_type="Duration",
            )
        )

    def test_default_numeric_wrappers_keep_compact_model(self) -> None:
        self.assertFalse(should_use_general_english("0123456789IDSB"))
        self.assertFalse(should_use_general_english("0123456789/IDSB"))
        self.assertFalse(should_use_general_english("0123456789:IDSB"))

    def test_router_selects_text_model_for_audited_request(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()

        selected = router.for_request(None, name="COMMISSION", recognizer_type="Ocr")

        self.assertIs(selected, router.text)

    def test_router_selects_text_model_for_dock_level_digit_proof(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()

        selected = router.for_request(
            "0123456789IDSB",
            name="DOCK_LEVEL_DIGIT_PROOF_128",
            recognizer_type="Digit",
        )

        self.assertIs(selected, router.text)

    def test_router_keeps_unlisted_direct_ocr_on_compact_model(self) -> None:
        router = GlobalEnglishOcr()
        router.compact = Mock()
        router.text = Mock()
        router.compact.ocr.return_value = "12345"

        result = router.ocr(object())

        self.assertEqual(result, "12345")
        router.compact.ocr.assert_called_once()
        router.text.ocr.assert_not_called()

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
