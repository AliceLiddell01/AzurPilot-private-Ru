from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.model import GameSettingState
from module.game_settings.preflight import GameSettingsPreflightScanner
from module.game_settings.traversal import (
    OptionsTraversalResult,
    OptionsViewport,
)


ROOT = Path(__file__).resolve().parents[1]


class _FakeDevice:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)


class _FakePreflightScanner(GameSettingsPreflightScanner):
    def __init__(
        self,
        viewports: int = 3,
        *,
        traversal_error: Exception | None = None,
        return_error: Exception | None = None,
    ) -> None:
        self.device = _FakeDevice()
        self.viewports = viewports
        self.traversal_error = traversal_error
        self.return_error = return_error
        self.visited = 0
        self.ensure_calls = 0
        self.return_calls = 0

    def ensure_options_page(self) -> bool:
        self.ensure_calls += 1
        return True

    def traverse_options(self, visitor):
        if self.traversal_error is not None:
            raise self.traversal_error

        for index in range(1, self.viewports + 1):
            self.visited += 1
            viewport = OptionsViewport(
                index=index,
                scroll_offset=float((index - 1) * 100),
                is_top=index == 1,
                is_bottom=index == self.viewports,
            )
            if bool(visitor(viewport)):
                return OptionsTraversalResult(
                    visited_viewports=index,
                    final_offset=viewport.scroll_offset,
                    reached_bottom=viewport.is_bottom,
                    stopped_early=True,
                )

        return OptionsTraversalResult(
            visited_viewports=self.viewports,
            final_offset=float(max(0, self.viewports - 1) * 100),
            reached_bottom=True,
            stopped_early=False,
        )

    def return_to_main(self) -> bool:
        self.return_calls += 1
        if self.return_error is not None:
            raise self.return_error
        return True


class GameSettingsPreflightTests(unittest.TestCase):
    def test_preflight_scanner_is_concrete(self) -> None:
        self.assertFalse(inspect.isabstract(GameSettingsPreflightScanner))

    def test_custom_ship_names_definition_requires_off(self) -> None:
        self.assertEqual(CUSTOM_SHIP_NAMES.key, "custom_ship_names")
        self.assertEqual(CUSTOM_SHIP_NAMES.location, "options")
        self.assertEqual(
            CUSTOM_SHIP_NAMES_REQUIRED_OFF.definition,
            CUSTOM_SHIP_NAMES,
        )
        self.assertIs(
            CUSTOM_SHIP_NAMES_REQUIRED_OFF.expected_state,
            GameSettingState.OFF,
        )

    def test_on_is_reported_as_required_mismatch_and_stops_early(self) -> None:
        scanner = _FakePreflightScanner(viewports=4)

        with patch(
            "module.game_settings.preflight.detect_custom_ship_names",
            side_effect=[None, GameSettingState.ON],
        ):
            result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIsNotNone(check)
        self.assertIs(check.detected_state, GameSettingState.ON)
        self.assertIs(check.expected_state, GameSettingState.OFF)
        self.assertFalse(check.compatible)
        self.assertFalse(result.all_required_compatible)
        self.assertEqual(len(result), 1)
        self.assertEqual(scanner.visited, 2)
        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_off_is_reported_as_compatible(self) -> None:
        scanner = _FakePreflightScanner(viewports=3)

        with patch(
            "module.game_settings.preflight.detect_custom_ship_names",
            return_value=GameSettingState.OFF,
        ):
            result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIs(check.detected_state, GameSettingState.OFF)
        self.assertTrue(check.compatible)
        self.assertTrue(result.all_required_compatible)
        self.assertEqual(scanner.visited, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_row_present_unknown_stops_early_and_fails_closed(self) -> None:
        scanner = _FakePreflightScanner(viewports=4)

        with patch(
            "module.game_settings.preflight.detect_custom_ship_names",
            side_effect=[None, GameSettingState.UNKNOWN, GameSettingState.OFF],
        ):
            result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIs(check.detected_state, GameSettingState.UNKNOWN)
        self.assertFalse(check.compatible)
        self.assertFalse(result.all_required_compatible)
        self.assertEqual(scanner.visited, 2)
        self.assertEqual(scanner.return_calls, 1)

    def test_absent_setting_reaches_bottom_as_unknown(self) -> None:
        scanner = _FakePreflightScanner(viewports=3)

        with patch(
            "module.game_settings.preflight.detect_custom_ship_names",
            return_value=None,
        ):
            result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIs(check.detected_state, GameSettingState.UNKNOWN)
        self.assertFalse(check.compatible)
        self.assertFalse(result.all_required_compatible)
        self.assertEqual(scanner.visited, 3)
        self.assertEqual(scanner.return_calls, 1)

    def test_traversal_failure_still_returns_to_main(self) -> None:
        scanner = _FakePreflightScanner(
            traversal_error=RuntimeError("primary traversal failure")
        )

        with self.assertRaisesRegex(RuntimeError, "primary traversal failure"):
            scanner.scan_game_settings()

        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_cleanup_failure_does_not_mask_primary_failure(self) -> None:
        scanner = _FakePreflightScanner(
            traversal_error=RuntimeError("primary traversal failure"),
            return_error=RuntimeError("cleanup failure"),
        )

        with self.assertRaisesRegex(RuntimeError, "primary traversal failure"):
            scanner.scan_game_settings()

        self.assertEqual(scanner.return_calls, 1)

    def test_cleanup_failure_propagates_after_successful_scan(self) -> None:
        scanner = _FakePreflightScanner(
            return_error=RuntimeError("cleanup failure"),
        )

        with patch(
            "module.game_settings.preflight.detect_custom_ship_names",
            return_value=GameSettingState.ON,
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
                scanner.scan_game_settings()

        self.assertEqual(scanner.return_calls, 1)

    def test_stage5_production_code_has_no_setting_mutation_calls(self) -> None:
        forbidden = {
            "click",
            "appear_then_click",
            "set",
            "swipe",
        }
        calls = set()

        for relative in (
            "module/game_settings/definitions.py",
            "module/game_settings/detector.py",
            "module/game_settings/preflight.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        calls.add(node.func.attr)
                    elif isinstance(node.func, ast.Name):
                        calls.add(node.func.id)

        self.assertTrue(forbidden.isdisjoint(calls), calls)


if __name__ == "__main__":
    unittest.main()
