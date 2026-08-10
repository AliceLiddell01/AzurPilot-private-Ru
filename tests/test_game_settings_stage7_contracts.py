from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.model import GameSettingRequirement, GameSettingState
from module.game_settings.options_detector import (
    ROW_SPECS_BY_KEY,
    GameSettingOptionObservation,
    GameSettingRowObservation,
    OcrTextBox,
    _MARKER_HALF_HEIGHT,
    _MARKER_WIDTH,
    _MARKER_X_GAP,
    observe_game_setting_row,
)
from module.game_settings.preflight import GameSettingsPreflightScanner
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.traversal import OptionsTraversalResult, OptionsViewport


_BACKGROUND = 96


def _paint_marker(
    image: np.ndarray,
    bounds: tuple[int, int, int, int],
    *,
    selected: bool,
) -> None:
    _x1, y1, x2, y2 = bounds
    center_y = int(round((y1 + y2) / 2.0))
    left = x2 + _MARKER_X_GAP
    top = center_y - _MARKER_HALF_HEIGHT
    right = left + _MARKER_WIDTH
    bottom = center_y + _MARKER_HALF_HEIGHT
    if selected:
        image[top + 5 : bottom - 5, left + 5 : right - 5] = 230
    else:
        image[top + 10 : bottom - 10, left + 10 : right - 10] = 150


def _render_row(key: str, selected_value):
    spec = ROW_SPECS_BY_KEY[key]
    image = np.full((720, 1280, 3), _BACKGROUND, dtype=np.uint8)
    y = 260
    detections: list[OcrTextBox] = [
        OcrTextBox(
            text=spec.label_aliases[0],
            bounds=(220, y, 470, y + 20),
            score=0.99,
        )
    ]
    x = 520
    for option in spec.options:
        text = option.aliases[0]
        width = max(34, 9 * len(text))
        bounds = (x, y, x + width, y + 20)
        detections.append(OcrTextBox(text=text, bounds=bounds, score=0.99))
        _paint_marker(image, bounds, selected=option.value is selected_value)
        x += width + 78
    return image, tuple(detections), spec


class ProductionRowContractTests(unittest.TestCase):
    def test_every_production_requirement_detects_required_value(self) -> None:
        self.assertEqual(
            set(ROW_SPECS_BY_KEY),
            {entry.key for entry in GAME_SETTINGS_OPTIONS_REGISTRY},
        )
        for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
            with self.subTest(key=entry.key):
                required = entry.requirement.expected_value
                image, detections, spec = _render_row(entry.key, required)
                observation = observe_game_setting_row(
                    image,
                    spec,
                    detections=detections,
                )
                self.assertIsNotNone(observation)
                self.assertIs(observation.value, required)
                target = observation.option_for(required)
                self.assertIsNotNone(target)

    def test_every_production_requirement_detects_known_mismatch(self) -> None:
        for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
            with self.subTest(key=entry.key):
                required = entry.requirement.expected_value
                spec = ROW_SPECS_BY_KEY[entry.key]
                mismatch = next(
                    option.value
                    for option in spec.options
                    if option.value is not required
                )
                image, detections, spec = _render_row(entry.key, mismatch)
                observation = observe_game_setting_row(
                    image,
                    spec,
                    detections=detections,
                )
                self.assertIsNotNone(observation)
                self.assertIs(observation.value, mismatch)
                self.assertIsNot(observation.value, required)

    def test_every_production_requirement_rejects_unrelated_neighbor_label(self) -> None:
        for entry in GAME_SETTINGS_OPTIONS_REGISTRY:
            with self.subTest(key=entry.key):
                required = entry.requirement.expected_value
                image, detections, spec = _render_row(entry.key, required)
                unrelated = (
                    OcrTextBox(
                        text="Completely Unrelated Setting",
                        bounds=detections[0].bounds,
                        score=0.99,
                    ),
                    *detections[1:],
                )
                observation = observe_game_setting_row(
                    image,
                    spec,
                    detections=unrelated,
                )
                self.assertIsNone(observation)


class _ReadOnlyDevice:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)


class _ReadOnlyScanner(GameSettingsPreflightScanner):
    def __init__(self, registry: tuple[GameSettingCheckSpec, ...]) -> None:
        self.device = _ReadOnlyDevice()
        self.check_registry = registry
        self.return_calls = 0

    def traverse_options(self, visitor):
        viewport = OptionsViewport(
            index=1,
            scroll_offset=0.0,
            is_top=True,
            is_bottom=False,
        )
        stopped = bool(visitor(viewport))
        return OptionsTraversalResult(
            visited_viewports=1,
            final_offset=0.0,
            reached_bottom=False,
            stopped_early=stopped,
        )

    def return_to_main(self) -> bool:
        self.return_calls += 1
        return True


class ReadOnlyAuditContractTests(unittest.TestCase):
    def test_scan_never_calls_registered_enforcement_observer(self) -> None:
        observer_calls = 0

        def detector(_image: np.ndarray):
            return GameSettingState.OFF

        def observer(_image: np.ndarray):
            nonlocal observer_calls
            observer_calls += 1
            return GameSettingRowObservation(
                value=GameSettingState.OFF,
                row_bounds=(200, 200, 700, 240),
                options=(
                    GameSettingOptionObservation(
                        value=GameSettingState.OFF,
                        bounds=(500, 205, 530, 225),
                        click_bounds=(490, 195, 570, 235),
                        marker_activity=0.2,
                    ),
                ),
            )

        definition = GAME_SETTINGS_OPTIONS_REGISTRY[0].definition
        toggle_definition = type(definition)(key="read_only_guard", location="options")
        requirement = GameSettingRequirement(
            toggle_definition,
            GameSettingState.OFF,
        )
        registry = build_game_settings_registry(
            (
                GameSettingCheckSpec(
                    definition=toggle_definition,
                    detector=detector,
                    requirement=requirement,
                    observer=observer,
                ),
            )
        )
        scanner = _ReadOnlyScanner(registry)

        result = scanner.scan_game_settings()

        self.assertEqual(observer_calls, 0)
        self.assertFalse(hasattr(result, "changed_keys"))
        self.assertIs(result.get("read_only_guard").detected_state, GameSettingState.OFF)
        self.assertEqual(scanner.return_calls, 1)


if __name__ == "__main__":
    unittest.main()
