from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from module.exception import GameStuckError
from module.game_settings.assets import (
    GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
    GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
)
from module.game_settings.options_detector import OcrTextBox
from module.game_settings.options_landmarks import detect_options_semantic_landmark
from module.game_settings.traversal import (
    OPTIONS_CONTROL_NAME,
    OptionsTraversalMixin,
    OptionsViewportMotion,
)


ROOT = Path(__file__).resolve().parents[1]


def _frame() -> np.ndarray:
    return np.zeros((720, 1280, 3), dtype=np.uint8)


def _box(text: str, y: int) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=(250, y, 650, y + 24), score=0.99)


class OptionsSemanticLandmarkTests(unittest.TestCase):
    def test_terminal_landmark_accepts_truncated_ocr_text(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(_box("Rendering Compatibil", 420),),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "rendering_compatibility_terminal")
        self.assertTrue(observation.terminal)

    def test_deepest_visible_landmark_wins_in_overlapping_viewport(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(
                _box("Change Oathed Ship Names Off On", 250),
                _box("Rendering Compatibility Off On", 420),
            ),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "rendering_compatibility_terminal")
        self.assertEqual(observation.rank, 50)

    def test_current_oathed_ship_label_marks_lower_region(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(_box("Change Oathed Ship Names Off On", 330),),
        )

        self.assertIsNotNone(observation)
        self.assertEqual(observation.key, "custom_ship_names_region")
        self.assertEqual(observation.rank, 40)
        self.assertFalse(observation.terminal)

    def test_unrelated_rows_do_not_create_semantic_position(self) -> None:
        observation = detect_options_semantic_landmark(
            _frame(),
            detections=(
                _box("Allow Dorm Visitors Off On", 200),
                _box("Sleep Mode On Main Off On", 400),
            ),
        )

        self.assertIsNone(observation)

    def test_landmark_module_does_not_depend_on_production_registry(self) -> None:
        path = ROOT / "module" / "game_settings" / "options_landmarks.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        self.assertFalse(any(name.endswith(".registry") for name in imports))


@dataclass(frozen=True)
class _FakeFrame:
    position: int


class _SemanticTraversalScanner(OptionsTraversalMixin):
    def __init__(
        self,
        *,
        reverse_at: int | None = None,
        lower_position: int = 1,
        terminal_position: int = 3,
        semantic_positions: dict[int, tuple[str, int, bool]] | None = None,
    ) -> None:
        self.device = self
        self.position = 0
        self.reverse_at = reverse_at
        self.lower_position = lower_position
        self.terminal_position = terminal_position
        self.semantic_positions = semantic_positions or {
            0: ("frame_rate_region", 10, False),
            1: ("idle_screen_region", 30, False),
            2: ("custom_ship_names_region", 40, False),
            3: ("rendering_compatibility_terminal", 50, True),
        }
        self.down_swipes = 0
        self.removed_control_records: list[str] = []

    def ensure_options_page(self) -> bool:
        return False

    def _wait_options_stable(self) -> _FakeFrame:
        return _FakeFrame(self.position)

    def _confirm_options_page(self, _frame: _FakeFrame) -> None:
        return None

    def _options_anchor_matches(self, frame: _FakeFrame, anchor, *, offset) -> bool:
        del offset
        if anchor is GAME_SETTINGS_OPTIONS_TOP_ANCHOR:
            return frame.position == 0
        if anchor is GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR:
            return frame.position == self.lower_position
        raise AssertionError(anchor)

    def _swipe_options(self, *, down: bool) -> None:
        if not down:
            self.position = max(0, self.position - 1)
            return
        self.down_swipes += 1
        self.position = min(self.terminal_position, self.position + 1)

    def _measure_options_motion(
        self,
        previous: _FakeFrame,
        current: _FakeFrame,
    ) -> OptionsViewportMotion:
        if current.position == previous.position:
            return OptionsViewportMotion(0.0, 0.0, 1.0, 0.0)
        vertical = 100.0
        if self.reverse_at is not None and current.position == self.reverse_at:
            vertical = -80.0
        return OptionsViewportMotion(
            vertical_shift=vertical,
            horizontal_shift=0.0,
            response=0.8,
            edge_change=0.20,
        )

    def _detect_options_semantic_landmark(self, frame: _FakeFrame):
        item = self.semantic_positions.get(frame.position)
        if item is None:
            return None
        key, rank, terminal = item
        return SimpleNamespace(
            key=key,
            rank=rank,
            terminal=terminal,
            score=0.99,
        )

    def click_record_remove(self, name: str) -> int:
        self.removed_control_records.append(name)
        return 1


class OptionsSemanticTraversalTests(unittest.TestCase):
    def test_terminal_semantic_landmark_overrides_false_reverse_phase_sign(self) -> None:
        scanner = _SemanticTraversalScanner(reverse_at=3)
        visited: list[int] = []

        result = scanner.traverse_options(
            lambda _viewport: visited.append(scanner.position)
        )

        self.assertEqual(visited, [0, 1, 2, 3])
        self.assertEqual(scanner.down_swipes, 3)
        self.assertTrue(result.reached_bottom)
        self.assertFalse(result.stopped_early)
        self.assertEqual(
            scanner.removed_control_records,
            [OPTIONS_CONTROL_NAME, OPTIONS_CONTROL_NAME, OPTIONS_CONTROL_NAME],
        )

    def test_forward_semantic_landmark_can_override_bad_phase_before_lower_area(self) -> None:
        scanner = _SemanticTraversalScanner(
            reverse_at=1,
            lower_position=2,
        )
        visited: list[int] = []

        result = scanner.traverse_options(
            lambda _viewport: visited.append(scanner.position)
        )

        self.assertEqual(visited, [0, 1, 2, 3])
        self.assertTrue(result.reached_bottom)

    def test_reverse_motion_without_semantic_progress_still_fails_closed(self) -> None:
        scanner = _SemanticTraversalScanner(
            reverse_at=1,
            lower_position=2,
            semantic_positions={0: ("frame_rate_region", 10, False)},
        )

        with self.assertRaisesRegex(GameStuckError, "пошла назад"):
            scanner.traverse_options(lambda _viewport: None)

    def test_semantic_detector_receives_the_exact_stable_frame(self) -> None:
        scanner = _SemanticTraversalScanner(reverse_at=1, lower_position=2)
        seen_positions: list[int] = []
        original = scanner._detect_options_semantic_landmark

        def detector(frame: _FakeFrame):
            seen_positions.append(frame.position)
            return original(frame)

        scanner._detect_options_semantic_landmark = detector
        result = scanner.traverse_options(lambda _viewport: None)

        self.assertTrue(result.reached_bottom)
        self.assertEqual(seen_positions, [0, 1, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
