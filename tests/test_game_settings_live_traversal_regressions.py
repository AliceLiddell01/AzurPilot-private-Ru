from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.options_detector import OcrTextBox
from module.game_settings.options_landmarks import detect_options_semantic_landmark


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _box(text: str, bounds: tuple[int, int, int, int]) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=bounds, score=0.99)


class LiveOptionsTraversalRegressionTests(unittest.TestCase):
    def test_live_fixed_l2d_ocr_bridges_semantic_gap_before_terminal_bottom(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(
                _box("Fixed L2D Settingsor", (718, 623, 1018, 655)),
                _box("On", (1075, 626, 1110, 652)),
            ),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "fixed_l2d_region")
        self.assertEqual(observation.rank, 45)
        self.assertFalse(observation.terminal)

    def test_split_fixed_l2d_label_is_joined_without_losing_landmark(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(
                _box("Fixed", (720, 610, 785, 638)),
                _box("L2D", (790, 610, 835, 638)),
                _box("Settings", (840, 610, 945, 638)),
                _box("Off", (985, 610, 1025, 638)),
                _box("On", (1080, 610, 1115, 638)),
            ),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "fixed_l2d_region")
        self.assertEqual(observation.rank, 45)

    def test_fixed_l2d_row_wins_over_older_custom_ship_names_landmark(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(
                _box("Custom Ship Names", (230, 180, 475, 208)),
                _box("Off", (495, 180, 535, 208)),
                _box("On", (600, 180, 635, 208)),
                _box("Fixed L2D Settingsor", (718, 623, 1018, 655)),
                _box("On", (1075, 626, 1110, 652)),
            ),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "fixed_l2d_region")
        self.assertEqual(observation.rank, 45)


if __name__ == "__main__":
    unittest.main()
