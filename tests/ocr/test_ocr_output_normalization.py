
from __future__ import annotations

import unittest

from module.ocr.ocr import normalize_ocr_text


class OcrOutputNormalizationTests(unittest.TestCase):
    def test_compact_spacing_is_normalized_for_global_ocr(self) -> None:
        cases = {
            "MAX: 96056": "MAX:96056",
            "MAX : 96056": "MAX:96056",
            "14 / 15": "14/15",
            "01: 30: 00": "01:30:00",
            "7 - 2": "7-2",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ocr_text("azur_lane", raw), expected)

    def test_normalization_preserves_words_and_unrelated_models(self) -> None:
        self.assertEqual(
            normalize_ocr_text("azur_lane", "New Jersey"),
            "New Jersey",
        )
        self.assertEqual(
            normalize_ocr_text("azur_lane", "LEVEL: New Jersey 120"),
            "LEVEL: New Jersey 120",
        )
        self.assertEqual(
            normalize_ocr_text("cn", "MAX: 96056"),
            "MAX: 96056",
        )


if __name__ == "__main__":
    unittest.main()
