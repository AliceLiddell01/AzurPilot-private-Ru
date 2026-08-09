from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path

import imageio.v2 as imageio
import numpy as np

from module.exception import GamePageUnknownError, GameStuckError
from module.game_settings.assets import (
    GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
    GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
)
from module.game_settings.model import GameSettingsScanResult
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.traversal import (
    OPTIONS_VIEWPORT_AREA,
    OptionsTraversalMixin,
    OptionsViewportMotion,
    measure_options_viewport_motion,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"


def _fixture(name: str) -> np.ndarray:
    image = imageio.imread(FIXTURE_DIR / name)
    return image[:, :, :3] if image.ndim == 3 else image


@dataclass(frozen=True)
class _FakeFrame:
    position: int
    page_is_options: bool = True


class _FakeTraversalScanner(GameSettingsScanner):
    def __init__(
        self,
        *,
        start: int = 0,
        bottom: int = 2,
        down_steps: list[int] | None = None,
        bottom_anchor_enabled: bool = True,
        page_loss_after_down_swipe: int | None = None,
    ) -> None:
        self.position = start
        self.bottom = bottom
        self.down_steps = list(down_steps or [])
        self.bottom_anchor_enabled = bottom_anchor_enabled
        self.page_loss_after_down_swipe = page_loss_after_down_swipe
        self.page_is_options = True
        self.ensure_calls = 0
        self.up_swipes = 0
        self.down_swipes = 0

    def _scan_game_settings(self) -> GameSettingsScanResult:
        return GameSettingsScanResult()

    def ensure_options_page(self) -> bool:
        self.ensure_calls += 1
        self.page_is_options = True
        return False

    def _wait_options_stable(self) -> _FakeFrame:
        frame = _FakeFrame(self.position, self.page_is_options)
        self._confirm_options_page(frame)
        return frame

    def _confirm_options_page(self, frame: _FakeFrame) -> None:
        if not frame.page_is_options:
            raise GamePageUnknownError("Options lost in fake")

    def _options_anchor_matches(self, frame: _FakeFrame, anchor) -> bool:
        if anchor is GAME_SETTINGS_OPTIONS_TOP_ANCHOR:
            return frame.position == 0
        if anchor is GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR:
            return self.bottom_anchor_enabled and frame.position >= self.bottom
        raise AssertionError(anchor)

    def _swipe_options(self, *, down: bool) -> None:
        if down:
            self.down_swipes += 1
            step = self.down_steps.pop(0) if self.down_steps else 1
            self.position += step
            if self.page_loss_after_down_swipe == self.down_swipes:
                self.page_is_options = False
        else:
            self.up_swipes += 1
            self.position = max(0, self.position - 1)

    @staticmethod
    def _measure_options_motion(
        previous: _FakeFrame,
        current: _FakeFrame,
    ) -> OptionsViewportMotion:
        shift = float((current.position - previous.position) * 100)
        changed = current.position != previous.position
        return OptionsViewportMotion(
            vertical_shift=shift,
            horizontal_shift=0.0,
            response=1.0,
            edge_change=0.1 if changed else 0.0,
        )


@dataclass(frozen=True)
class _FakeStabilizationFrame:
    position: int
    page_is_options: bool = True


class _FakeStabilizationScanner(OptionsTraversalMixin):
    def __init__(self, frames: list[_FakeStabilizationFrame]) -> None:
        self.frames = list(frames)
        self.capture_count = 0

    def _capture_options_frame(self) -> _FakeStabilizationFrame:
        self.capture_count += 1
        return self.frames.pop(0)

    @staticmethod
    def _options_page_visible(frame: _FakeStabilizationFrame) -> bool:
        return frame.page_is_options

    @staticmethod
    def _measure_options_motion(
        previous: _FakeStabilizationFrame,
        current: _FakeStabilizationFrame,
    ) -> OptionsViewportMotion:
        changed = previous.position != current.position
        return OptionsViewportMotion(
            vertical_shift=10.0 if changed else 0.0,
            horizontal_shift=0.0,
            response=1.0,
            edge_change=0.1 if changed else 0.0,
        )


class OptionsTraversalContractTests(unittest.TestCase):
    def test_start_normalization_reaches_and_confirms_top(self) -> None:
        scanner = _FakeTraversalScanner(start=2, bottom=2)
        visited = []

        result = scanner.traverse_options(lambda viewport: visited.append(viewport))

        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.up_swipes, 2)
        self.assertEqual([item.scroll_offset for item in visited], [0.0, 100.0, 200.0])
        self.assertTrue(visited[0].is_top)
        self.assertTrue(result.reached_bottom)

    def test_already_top_does_not_issue_reset_swipe(self) -> None:
        scanner = _FakeTraversalScanner(start=0, bottom=1)

        scanner.traverse_options(lambda _viewport: None)

        self.assertEqual(scanner.up_swipes, 0)

    def test_normal_traversal_is_monotonic_and_visits_bottom_last(self) -> None:
        scanner = _FakeTraversalScanner(start=0, bottom=3)
        visited = []

        result = scanner.traverse_options(lambda viewport: visited.append(viewport))

        offsets = [item.scroll_offset for item in visited]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual([item.index for item in visited], [1, 2, 3, 4])
        self.assertFalse(any(item.is_bottom for item in visited[:-1]))
        self.assertTrue(visited[-1].is_bottom)
        self.assertEqual(result.visited_viewports, 4)
        self.assertFalse(result.stopped_early)

    def test_no_progress_retries_are_bounded_and_fail_closed(self) -> None:
        scanner = _FakeTraversalScanner(
            start=0,
            bottom=10,
            down_steps=[0, 0, 0],
        )
        visited = []

        with self.assertRaisesRegex(GameStuckError, "не прокручивается"):
            scanner.traverse_options(lambda viewport: visited.append(viewport))

        self.assertEqual(scanner.down_swipes, 2)
        self.assertEqual([item.index for item in visited], [1])

    def test_hard_safety_bound_is_not_used_as_bottom_detector(self) -> None:
        scanner = _FakeTraversalScanner(
            start=0,
            bottom=100,
            bottom_anchor_enabled=False,
        )
        scanner.options_max_viewports = 4

        with self.assertRaisesRegex(GameStuckError, "аварийный лимит"):
            scanner.traverse_options(lambda _viewport: None)

        self.assertEqual(scanner.down_swipes, 4)

    def test_early_stop_avoids_extra_swipe_and_keeps_options(self) -> None:
        scanner = _FakeTraversalScanner(start=0, bottom=5)
        visited = []

        result = scanner.traverse_options(
            lambda viewport: visited.append(viewport) or viewport.index == 2
        )

        self.assertEqual([item.index for item in visited], [1, 2])
        self.assertEqual(scanner.down_swipes, 1)
        self.assertTrue(scanner.page_is_options)
        self.assertTrue(result.stopped_early)
        self.assertFalse(result.reached_bottom)

    def test_backward_progress_fails_instead_of_becoming_monotonic(self) -> None:
        scanner = _FakeTraversalScanner(start=0, bottom=5, down_steps=[-1])

        with self.assertRaisesRegex(GameStuckError, "пошла назад"):
            scanner.traverse_options(lambda _viewport: None)

    def test_page_loss_during_traversal_fails_closed(self) -> None:
        scanner = _FakeTraversalScanner(
            start=0,
            bottom=3,
            page_loss_after_down_swipe=1,
        )

        with self.assertRaises(GamePageUnknownError):
            scanner.traverse_options(lambda _viewport: None)

    def test_stabilization_ignores_transient_quiet_pair(self) -> None:
        scanner = _FakeStabilizationScanner(
            [
                _FakeStabilizationFrame(0),
                _FakeStabilizationFrame(0),
                _FakeStabilizationFrame(1),
                _FakeStabilizationFrame(2),
                _FakeStabilizationFrame(2),
                _FakeStabilizationFrame(2),
                _FakeStabilizationFrame(2),
                _FakeStabilizationFrame(2),
            ]
        )

        frame = scanner._wait_options_stable()

        self.assertEqual(frame.position, 2)
        self.assertEqual(scanner.capture_count, 8)

    def test_stabilization_tolerates_bounded_selected_icon_animation(self) -> None:
        scanner = _FakeStabilizationScanner(
            [_FakeStabilizationFrame(0, False) for _ in range(5)]
            + [_FakeStabilizationFrame(0, True) for _ in range(5)]
        )

        frame = scanner._wait_options_stable()

        self.assertTrue(frame.page_is_options)

    def test_stabilization_fails_after_bounded_page_loss(self) -> None:
        scanner = _FakeStabilizationScanner(
            [_FakeStabilizationFrame(0, False) for _ in range(8)]
        )

        with self.assertRaises(GamePageUnknownError):
            scanner._wait_options_stable()


class OptionsTraversalVisualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.top = _fixture("options_traversal_top.png")
        cls.middle_previous = _fixture("options_traversal_middle_previous.png")
        cls.middle = _fixture("options_traversal_middle.png")
        cls.bottom = _fixture("options_traversal_bottom.png")
        cls.bottom_retry = _fixture("options_traversal_bottom_retry.png")

    def test_real_anchors_distinguish_top_middle_and_bottom(self) -> None:
        self.assertTrue(
            GAME_SETTINGS_OPTIONS_TOP_ANCHOR.match(
                self.top,
                offset=(3, 3),
                similarity=0.82,
            )
        )
        self.assertFalse(
            GAME_SETTINGS_OPTIONS_TOP_ANCHOR.match(
                self.middle,
                offset=(3, 3),
                similarity=0.82,
            )
        )
        self.assertFalse(
            GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR.match(
                self.middle,
                offset=(3, 3),
                similarity=0.82,
            )
        )
        self.assertTrue(
            GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR.match(
                self.bottom,
                offset=(3, 3),
                similarity=0.82,
            )
        )

    def test_real_downward_steps_are_monotonic_with_overlap(self) -> None:
        first = measure_options_viewport_motion(self.middle_previous, self.middle)
        second = measure_options_viewport_motion(self.middle, self.bottom)
        viewport_height = OPTIONS_VIEWPORT_AREA[3] - OPTIONS_VIEWPORT_AREA[1]

        for motion in (first, second):
            with self.subTest(motion=motion):
                self.assertGreaterEqual(motion.vertical_shift, 5.0)
                self.assertGreaterEqual(motion.response, 0.10)
                self.assertLess(abs(motion.horizontal_shift), 5.0)
                self.assertLess(motion.vertical_shift, viewport_height * 0.60)

    def test_real_bottom_anchor_survives_animated_background(self) -> None:
        for frame in (self.bottom, self.bottom_retry):
            with self.subTest():
                self.assertTrue(
                    GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR.match(
                        frame,
                        offset=(8, 110),
                        similarity=0.82,
                    )
                )

    def test_visual_fixtures_contain_only_options_content(self) -> None:
        x1, y1, x2, y2 = (160, 80, 1223, 690)
        for name in (
            "options_traversal_top.png",
            "options_traversal_middle_previous.png",
            "options_traversal_middle.png",
            "options_traversal_bottom.png",
            "options_traversal_bottom_retry.png",
        ):
            image = _fixture(name)
            outside = image.copy()
            outside[y1:y2, x1:x2] = 0
            with self.subTest(fixture=name):
                self.assertFalse(np.any(outside))

    def test_traversal_does_not_introduce_concrete_setting_registry(self) -> None:
        path = ROOT / "module" / "game_settings" / "traversal.py"
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        self.assertNotIn("Custom Ship Names", text)
        self.assertFalse(any(name.endswith(".switch") for name in imports))
        self.assertFalse(any(name.endswith(".setting") for name in imports))


if __name__ == "__main__":
    unittest.main()
