from __future__ import annotations

import unittest

from module.game_settings.enforcement import GameSettingsEnforcementScanner


class GameSettingsClickSafetyTests(unittest.TestCase):
    def test_live_bottom_toggle_target_is_shrunk_away_from_marker_edges(self) -> None:
        # Live Duplicate Ship Display failure: the detector's expanded marker
        # click area was centered at (1038, 648), but a stochastic device click
        # landed at y=663 near/below the diamond edge. Enforcement must only hand
        # the device a small central hitbox.
        expanded_marker = (1014, 624, 1062, 672)

        safe = GameSettingsEnforcementScanner._safe_click_bounds(expanded_marker)

        self.assertEqual(safe, (1030, 640, 1046, 656))
        self.assertGreater(safe[0], expanded_marker[0])
        self.assertGreater(safe[1], expanded_marker[1])
        self.assertLess(safe[2], expanded_marker[2])
        self.assertLess(safe[3], expanded_marker[3])
        self.assertLess(safe[3], 663)

    def test_safe_click_preserves_marker_center_for_choice_controls(self) -> None:
        expanded_marker = (714, 330, 762, 378)

        safe = GameSettingsEnforcementScanner._safe_click_bounds(expanded_marker)

        original_center = (
            (expanded_marker[0] + expanded_marker[2]) / 2.0,
            (expanded_marker[1] + expanded_marker[3]) / 2.0,
        )
        safe_center = (
            (safe[0] + safe[2]) / 2.0,
            (safe[1] + safe[3]) / 2.0,
        )
        self.assertEqual(safe_center, original_center)
        self.assertEqual(safe[2] - safe[0], 16)
        self.assertEqual(safe[3] - safe[1], 16)

    def test_narrow_compact_target_is_never_expanded(self) -> None:
        narrow_target = (1034, 642, 1042, 654)

        safe = GameSettingsEnforcementScanner._safe_click_bounds(narrow_target)

        self.assertEqual(safe, narrow_target)
        self.assertGreaterEqual(safe[0], narrow_target[0])
        self.assertGreaterEqual(safe[1], narrow_target[1])
        self.assertLessEqual(safe[2], narrow_target[2])
        self.assertLessEqual(safe[3], narrow_target[3])

    def test_invalid_compact_target_is_preserved_for_fail_closed_validation(self) -> None:
        invalid_targets = (
            (100, 100, 100, 120),
            (100, 100, 120, 100),
            (120, 100, 100, 120),
        )

        for target in invalid_targets:
            with self.subTest(target=target):
                safe = GameSettingsEnforcementScanner._safe_click_bounds(target)
                self.assertEqual(safe, target)


if __name__ == "__main__":
    unittest.main()
