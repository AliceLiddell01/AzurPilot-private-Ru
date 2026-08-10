from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

import imageio.v2 as imageio
import numpy as np

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.detector import (
    _CUSTOM_SHIP_NAMES_STATE_AREA,
)
from module.game_settings.model import (
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
)
from module.game_settings.preflight import GameSettingsPreflightScanner
from module.game_settings.registry import (
    GAME_SETTINGS_PREFLIGHT_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.traversal import (
    OptionsTraversalResult,
    OptionsViewport,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "game_settings"
_REFERENCE_ORIGIN = (226, 490)


def _fixture(name: str) -> np.ndarray:
    image = imageio.imread(FIXTURE_DIR / name)
    return image[:, :, :3] if image.ndim == 3 else image


def _frame_from_real_state_evidence(state: str) -> np.ndarray:
    row = _fixture("custom_ship_names_on.png").copy()
    if state == "off":
        x1, y1, x2, y2 = _CUSTOM_SHIP_NAMES_STATE_AREA
        row[y1:y2, x1:x2] = _fixture("custom_ship_names_off_state.png")
    elif state != "on":
        raise ValueError(f"Unsupported state fixture: {state}")

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y = _REFERENCE_ORIGIN
    height, width = row.shape[:2]
    frame[y : y + height, x : x + width] = row
    return frame


class _FakeDevice:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)


class _FakePreflightScanner(GameSettingsPreflightScanner):
    def __init__(
        self,
        viewports: int = 3,
        *,
        registry: tuple[GameSettingCheckSpec, ...] | None = None,
        frames: tuple[np.ndarray, ...] | None = None,
        ensure_error: Exception | None = None,
        traversal_error: Exception | None = None,
        return_error: Exception | None = None,
    ) -> None:
        self.device = _FakeDevice()
        self.viewports = viewports
        self.frames = frames
        self.check_registry = (
            GAME_SETTINGS_PREFLIGHT_REGISTRY if registry is None else registry
        )
        self.ensure_error = ensure_error
        self.traversal_error = traversal_error
        self.return_error = return_error
        self.visited = 0
        self.ensure_calls = 0
        self.traversal_calls = 0
        self.return_calls = 0

    def ensure_options_page(self) -> bool:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error
        return True

    def traverse_options(self, visitor):
        self.traversal_calls += 1
        self.ensure_options_page()
        if self.traversal_error is not None:
            raise self.traversal_error

        for index in range(1, self.viewports + 1):
            self.visited += 1
            if self.frames is not None:
                self.device.image = self.frames[index - 1]
            viewport = OptionsViewport(
                index=index,
                scroll_offset=float((index - 1) * 100),
                is_top=index == 1,
                is_bottom=False,
            )
            if bool(visitor(viewport)):
                return OptionsTraversalResult(
                    visited_viewports=index,
                    final_offset=viewport.scroll_offset,
                    reached_bottom=False,
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


def _definition(key: str) -> GameSettingDefinition:
    return GameSettingDefinition(key=key, location="options")


def _required(
    definition: GameSettingDefinition,
    state: GameSettingState,
) -> GameSettingRequirement:
    return GameSettingRequirement(definition=definition, expected_state=state)


class GameSettingsPreflightTests(unittest.TestCase):
    def test_preflight_scanner_is_concrete(self) -> None:
        self.assertFalse(inspect.isabstract(GameSettingsPreflightScanner))

    def test_production_definition_requires_off(self) -> None:
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

    def test_real_on_fixture_flows_through_production_registry(self) -> None:
        scanner = _FakePreflightScanner(
            viewports=1,
            frames=(_frame_from_real_state_evidence("on"),),
        )

        result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIsNotNone(check)
        self.assertIs(check.detected_state, GameSettingState.ON)
        self.assertIs(check.expected_state, GameSettingState.OFF)
        self.assertFalse(check.compatible)
        self.assertEqual(scanner.visited, 1)
        self.assertEqual(scanner.traversal_calls, 1)
        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_real_off_fixture_flows_through_production_registry(self) -> None:
        scanner = _FakePreflightScanner(
            viewports=1,
            frames=(_frame_from_real_state_evidence("off"),),
        )

        result = scanner.scan_game_settings()

        check = result.get("custom_ship_names")
        self.assertIs(check.detected_state, GameSettingState.OFF)
        self.assertTrue(check.compatible)
        self.assertTrue(result.all_required_compatible)
        self.assertEqual(scanner.visited, 1)
        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_two_entries_resolve_in_one_viewport_and_one_traversal(self) -> None:
        definition_a = _definition("setting_a")
        definition_b = _definition("setting_b")
        frame_ids: list[int] = []
        calls = {"a": 0, "b": 0}

        def detector_a(image: np.ndarray) -> GameSettingState | None:
            calls["a"] += 1
            frame_ids.append(id(image))
            return GameSettingState.OFF

        def detector_b(image: np.ndarray) -> GameSettingState | None:
            calls["b"] += 1
            frame_ids.append(id(image))
            return GameSettingState.ON

        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(
                    definition=definition_a,
                    detector=detector_a,
                    requirement=_required(definition_a, GameSettingState.OFF),
                ),
                GameSettingCheckSpec(
                    definition=definition_b,
                    detector=detector_b,
                    requirement=_required(definition_b, GameSettingState.ON),
                ),
            )
        )
        scanner = _FakePreflightScanner(viewports=4, registry=registry)

        result = scanner.scan_game_settings()

        self.assertEqual(tuple(check.key for check in result), ("setting_a", "setting_b"))
        self.assertEqual(calls, {"a": 1, "b": 1})
        self.assertEqual(len(set(frame_ids)), 1)
        self.assertEqual(scanner.visited, 1)
        self.assertEqual(scanner.traversal_calls, 1)
        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_resolved_entry_is_not_called_on_later_viewports(self) -> None:
        definition_a = _definition("setting_a")
        definition_b = _definition("setting_b")
        calls = {"a": 0, "b": 0}

        def detector_a(_image: np.ndarray) -> GameSettingState | None:
            calls["a"] += 1
            return GameSettingState.OFF

        def detector_b(_image: np.ndarray) -> GameSettingState | None:
            calls["b"] += 1
            if calls["b"] == 1:
                return None
            return GameSettingState.ON

        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(definition=definition_a, detector=detector_a),
                GameSettingCheckSpec(definition=definition_b, detector=detector_b),
            )
        )
        scanner = _FakePreflightScanner(viewports=4, registry=registry)

        scanner.scan_game_settings()

        self.assertEqual(calls, {"a": 1, "b": 2})
        self.assertEqual(scanner.visited, 2)

    def test_absent_entry_becomes_unknown_at_hard_bottom(self) -> None:
        definition_a = _definition("setting_a")
        definition_b = _definition("setting_b")

        def detector_a(_image: np.ndarray) -> GameSettingState | None:
            return GameSettingState.OFF

        def detector_b(_image: np.ndarray) -> GameSettingState | None:
            return None

        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(definition=definition_a, detector=detector_a),
                GameSettingCheckSpec(
                    definition=definition_b,
                    detector=detector_b,
                    requirement=_required(definition_b, GameSettingState.ON),
                ),
            )
        )
        scanner = _FakePreflightScanner(viewports=3, registry=registry)

        result = scanner.scan_game_settings()

        self.assertEqual(len(result), 2)
        self.assertIs(result.get("setting_a").detected_state, GameSettingState.OFF)
        self.assertIs(result.get("setting_b").detected_state, GameSettingState.UNKNOWN)
        self.assertFalse(result.get("setting_b").compatible)
        self.assertEqual(scanner.visited, 3)

    def test_row_present_unknown_is_resolved_while_other_entry_continues(self) -> None:
        definition_a = _definition("setting_a")
        definition_b = _definition("setting_b")
        calls = {"a": 0, "b": 0}

        def detector_a(_image: np.ndarray) -> GameSettingState | None:
            calls["a"] += 1
            return GameSettingState.UNKNOWN

        def detector_b(_image: np.ndarray) -> GameSettingState | None:
            calls["b"] += 1
            return None

        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(definition=definition_a, detector=detector_a),
                GameSettingCheckSpec(definition=definition_b, detector=detector_b),
            )
        )
        scanner = _FakePreflightScanner(viewports=3, registry=registry)

        result = scanner.scan_game_settings()

        self.assertIs(result.get("setting_a").detected_state, GameSettingState.UNKNOWN)
        self.assertIs(result.get("setting_b").detected_state, GameSettingState.UNKNOWN)
        self.assertEqual(calls, {"a": 1, "b": 3})
        self.assertEqual(scanner.visited, 3)

    def test_result_order_is_registry_order_not_detection_order(self) -> None:
        definition_a = _definition("setting_a")
        definition_b = _definition("setting_b")
        calls = {"a": 0}

        def detector_a(_image: np.ndarray) -> GameSettingState | None:
            calls["a"] += 1
            if calls["a"] == 1:
                return None
            return GameSettingState.OFF

        def detector_b(_image: np.ndarray) -> GameSettingState | None:
            return GameSettingState.ON

        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(definition=definition_a, detector=detector_a),
                GameSettingCheckSpec(definition=definition_b, detector=detector_b),
            )
        )
        scanner = _FakePreflightScanner(viewports=3, registry=registry)

        result = scanner.scan_game_settings()

        self.assertEqual(tuple(check.key for check in result), ("setting_a", "setting_b"))
        self.assertIs(result.results[0].detected_state, GameSettingState.OFF)
        self.assertIs(result.results[1].detected_state, GameSettingState.ON)
        self.assertEqual(scanner.visited, 2)

    def test_detector_exception_propagates_and_cleanup_runs(self) -> None:
        definition = _definition("setting_a")

        def detector(_image: np.ndarray) -> GameSettingState | None:
            raise ValueError("unsupported geometry")

        registry = build_game_settings_registry(
            (GameSettingCheckSpec(definition=definition, detector=detector),)
        )
        scanner = _FakePreflightScanner(registry=registry)

        with self.assertRaisesRegex(ValueError, "unsupported geometry"):
            scanner.scan_game_settings()

        self.assertEqual(scanner.ensure_calls, 1)
        self.assertEqual(scanner.return_calls, 1)

    def test_options_entry_failure_still_attempts_return_to_main(self) -> None:
        scanner = _FakePreflightScanner(
            ensure_error=RuntimeError("options entry failure")
        )

        with self.assertRaisesRegex(RuntimeError, "options entry failure"):
            scanner.scan_game_settings()

        self.assertEqual(scanner.ensure_calls, 1)
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
        definition = _definition("setting_a")

        def detector(_image: np.ndarray) -> GameSettingState | None:
            return GameSettingState.OFF

        registry = build_game_settings_registry(
            (GameSettingCheckSpec(definition=definition, detector=detector),)
        )
        scanner = _FakePreflightScanner(
            registry=registry,
            return_error=RuntimeError("cleanup failure"),
        )

        with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
            scanner.scan_game_settings()

        self.assertEqual(scanner.return_calls, 1)

    def test_empty_registry_is_noop_without_navigation_or_cleanup(self) -> None:
        scanner = _FakePreflightScanner(registry=())

        result = scanner.scan_game_settings()

        self.assertEqual(len(result), 0)
        self.assertEqual(scanner.traversal_calls, 0)
        self.assertEqual(scanner.ensure_calls, 0)
        self.assertEqual(scanner.return_calls, 0)

    def test_production_game_settings_code_has_no_setting_mutation_calls(self) -> None:
        forbidden_attributes = {
            "click",
            "appear_then_click",
            "set",
            "swipe",
        }
        forbidden_names = {
            "click",
            "appear_then_click",
            "swipe",
        }
        calls: set[str] = set()

        for relative in (
            "module/game_settings/definitions.py",
            "module/game_settings/detector.py",
            "module/game_settings/registry.py",
            "module/game_settings/preflight.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in forbidden_attributes:
                        calls.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    if node.func.id in forbidden_names:
                        calls.add(node.func.id)

        self.assertFalse(calls, calls)


if __name__ == "__main__":
    unittest.main()
