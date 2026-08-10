from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.model import GameSettingRequirement, GameSettingState
from module.game_settings.options_detector import (
    ROW_LAYOUT_CHOICE_CARDS,
    ROW_SPECS_BY_KEY,
    GameSettingOptionObservation,
    GameSettingRowObservation,
    OcrTextBox,
    _choice_marker_bounds,
    _toggle_marker_bounds,
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


def _paint_marker_bounds(
    image: np.ndarray,
    bounds: tuple[int, int, int, int],
    *,
    selected: bool,
) -> None:
    x1, y1, x2, y2 = bounds
    if selected:
        image[y1 + 5 : y2 - 5, x1 + 5 : x2 - 5] = 230
    else:
        image[y1 + 10 : y2 - 10, x1 + 10 : x2 - 10] = 150


def _render_toggle_row(key: str, selected_value):
    spec = ROW_SPECS_BY_KEY[key]
    image = np.full((720, 1280, 3), _BACKGROUND, dtype=np.uint8)
    y = 300
    detections = (
        OcrTextBox(
            text=spec.label_aliases[0],
            bounds=(230, y, 470, y + 30),
            score=0.99,
        ),
    )
    for option in spec.options:
        marker_bounds = _toggle_marker_bounds(
            option.value,
            panel="left",
            center_y=y + 15,
        )
        _paint_marker_bounds(
            image,
            marker_bounds,
            selected=option.value is selected_value,
        )
    return image, detections, spec


def _render_choice_row(key: str, selected_value):
    spec = ROW_SPECS_BY_KEY[key]
    image = np.full((720, 1280, 3), _BACKGROUND, dtype=np.uint8)
    label_y = 220
    detections: list[OcrTextBox] = [
        OcrTextBox(
            text=spec.label_aliases[0],
            bounds=(195, label_y, 500, label_y + 35),
            score=0.99,
        )
    ]
    for index, option in enumerate(spec.options):
        column = index % 2
        row = index // 2
        x1 = 400 if column == 0 else 880
        y1 = 300 + row * 75
        text = option.aliases[0]
        width = max(60, len(text) * 10)
        box = OcrTextBox(
            text=text,
            bounds=(x1, y1, x1 + width, y1 + 32),
            score=0.99,
        )
        detections.append(box)
        _paint_marker_bounds(
            image,
            _choice_marker_bounds(box.bounds),
            selected=option.value is selected_value,
        )
    return image, tuple(detections), spec


def _render_row(key: str, selected_value):
    spec = ROW_SPECS_BY_KEY[key]
    if spec.layout == ROW_LAYOUT_CHOICE_CARDS:
        return _render_choice_row(key, selected_value)
    return _render_toggle_row(key, selected_value)


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
            reached_bottom=True,
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