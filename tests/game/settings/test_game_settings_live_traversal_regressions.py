from __future__ import annotations

import unittest
from dataclasses import dataclass

import numpy as np

from module.game_settings.assets import (
    GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
    GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
)
from module.game_settings.options_detector import OcrTextBox
from module.game_settings.options_landmarks import detect_options_semantic_landmark
from module.game_settings.traversal import (
    OptionsTraversalMixin,
    OptionsViewportMotion,
)


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _box(text: str, bounds: tuple[int, int, int, int]) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=bounds, score=0.99)


@dataclass(frozen=True)
class _TraversalFrame:
    position: int


@dataclass(frozen=True)
class _Semantic:
    key: str
    rank: int
    terminal: bool = False
    score: float = 1.0


class _LiveReversePhaseScanner(OptionsTraversalMixin):
    def __init__(self) -> None:
        self.position = 0
        self.down_swipes = 0

    def ensure_options_page(self) -> bool:
        return False

    def _wait_options_stable(self) -> _TraversalFrame:
        return _TraversalFrame(self.position)

    @staticmethod
    def _confirm_options_page(_frame: _TraversalFrame) -> None:
        return None

    @staticmethod
    def _options_anchor_matches(frame: _TraversalFrame, anchor, *, offset) -> bool:
        del offset
        if anchor is GAME_SETTINGS_OPTIONS_TOP_ANCHOR:
            return frame.position == 0
        if anchor is GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR:
            return frame.position == 6
        raise AssertionError(anchor)

    @staticmethod
    def _detect_options_semantic_landmark(frame: _TraversalFrame):
        semantics = {
            0: _Semantic("frame_rate_region", 10),
            4: _Semantic("story_autoplay_region", 20),
            6: _Semantic("idle_screen_region", 30),
            7: _Semantic("custom_ship_names_region", 40),
            8: _Semantic("custom_ship_names_region", 40),
            9: _Semantic("fixed_l2d_region", 45),
            10: _Semantic("rendering_compatibility_terminal", 50, terminal=True),
        }
        return semantics.get(frame.position)

    def _swipe_options(self, *, down: bool) -> None:
        if not down:
            raise AssertionError("test starts at the confirmed top")
        self.down_swipes += 1
        self.position = min(10, self.position + 1)

    @staticmethod
    def _measure_options_motion(
        previous: _TraversalFrame,
        current: _TraversalFrame,
    ) -> OptionsViewportMotion:
        if current.position == 9 and previous.position == 8:
            return OptionsViewportMotion(
                vertical_shift=-58.6,
                horizontal_shift=1.2,
                response=0.85,
                edge_change=0.12,
            )
        changed = current.position != previous.position
        return OptionsViewportMotion(
            vertical_shift=260.0 if changed else 0.0,
            horizontal_shift=0.0,
            response=1.0,
            edge_change=0.12 if changed else 0.0,
        )

    @staticmethod
    def _clear_options_control_record() -> None:
        return None


class _RepeatedLowerSemanticScanner(_LiveReversePhaseScanner):
    @staticmethod
    def _detect_options_semantic_landmark(frame: _TraversalFrame):
        semantics = {
            0: _Semantic("frame_rate_region", 10),
            4: _Semantic("story_autoplay_region", 20),
            6: _Semantic("idle_screen_region", 30),
            7: _Semantic("custom_ship_names_region", 40),
            8: _Semantic("custom_ship_names_region", 40),
            9: _Semantic("fixed_l2d_region", 45),
            10: _Semantic("fixed_l2d_region", 45),
            11: _Semantic("fixed_l2d_region", 45),
        }
        return semantics.get(frame.position)

    def _swipe_options(self, *, down: bool) -> None:
        if not down:
            raise AssertionError("test starts at the confirmed top")
        self.down_swipes += 1
        self.position = min(11, self.position + 1)

    @staticmethod
    def _measure_options_motion(
        previous: _TraversalFrame,
        current: _TraversalFrame,
    ) -> OptionsViewportMotion:
        if previous.position >= 9 and current.position > previous.position:
            return OptionsViewportMotion(
                vertical_shift=-60.0,
                horizontal_shift=0.0,
                response=0.80,
                edge_change=0.12,
            )
        changed = current.position != previous.position
        return OptionsViewportMotion(
            vertical_shift=260.0 if changed else 0.0,
            horizontal_shift=0.0,
            response=1.0,
            edge_change=0.12 if changed else 0.0,
        )


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

    def test_live_reverse_phase_at_fixed_l2d_is_overridden_by_semantic_progress(self) -> None:
        scanner = _LiveReversePhaseScanner()
        visited_positions: list[int] = []

        result = scanner.traverse_options(
            lambda _viewport: visited_positions.append(scanner.position)
        )

        self.assertTrue(result.reached_bottom)
        self.assertFalse(result.stopped_early)
        self.assertEqual(visited_positions, list(range(11)))
        self.assertEqual(scanner.down_swipes, 10)

    def test_repeated_equal_lower_semantic_rank_is_bounded_as_no_progress(self) -> None:
        scanner = _RepeatedLowerSemanticScanner()
        visited_positions: list[int] = []

        result = scanner.traverse_options(
            lambda _viewport: visited_positions.append(scanner.position)
        )

        self.assertTrue(result.reached_bottom)
        self.assertFalse(result.stopped_early)
        self.assertEqual(visited_positions[-1], 10)
        self.assertEqual(scanner.down_swipes, 11)


if __name__ == "__main__":
    unittest.main()
