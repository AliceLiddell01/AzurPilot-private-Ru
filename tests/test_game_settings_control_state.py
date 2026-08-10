from __future__ import annotations

import unittest

import cv2
import numpy as np

from module.game_settings.assets import (
    TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED,
    TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED,
)
from module.game_settings.control_state import (
    control_selection_confidence,
    observe_game_setting_row_with_control_assets,
)
from module.game_settings.model import FrameRateValue, GameSettingState
from module.game_settings.options_detector import (
    CUSTOM_SHIP_NAMES_ROW,
    ENABLE_IDLE_SCREEN_ROW,
    FRAME_RATE_ROW,
    OcrTextBox,
    _choice_marker_bounds,
    _toggle_marker_bounds,
)


BACKGROUND = 96


def _frame() -> np.ndarray:
    return np.full((720, 1280, 3), BACKGROUND, dtype=np.uint8)


def _box(text: str, x1: int, y1: int, x2: int, y2: int) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=(x1, y1, x2, y2), score=0.99)


def _load_rgb(path: str) -> np.ndarray:
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise AssertionError(f"Missing test asset: {path}")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)


def _paste_control(
    image: np.ndarray,
    marker_bounds: tuple[int, int, int, int],
    *,
    selected: bool,
) -> None:
    asset = _load_rgb(
        TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED.file
        if selected
        else TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED.file
    )
    x1, y1, x2, y2 = marker_bounds
    center_x = int(round((x1 + x2) / 2.0))
    center_y = int(round((y1 + y2) / 2.0))
    height, width = asset.shape[:2]
    left = center_x - width // 2
    top = center_y - height // 2
    image[top : top + height, left : left + width] = asset


class GameSettingsControlStateTests(unittest.TestCase):
    def test_control_assets_are_small_real_state_templates(self) -> None:
        selected = _load_rgb(TEMPLATE_GAME_SETTINGS_CONTROL_SELECTED.file)
        unselected = _load_rgb(TEMPLATE_GAME_SETTINGS_CONTROL_UNSELECTED.file)

        self.assertEqual(selected.shape[:2], (28, 28))
        self.assertEqual(unselected.shape[:2], (28, 28))
        self.assertFalse(np.array_equal(selected, unselected))

    def test_toggle_state_is_selected_by_assets_not_marker_activity(self) -> None:
        image = _frame()
        y = 260
        detections = (_box("Enable Idle Screen", 230, y, 460, y + 30),)
        off_bounds = _toggle_marker_bounds(
            GameSettingState.OFF,
            panel="left",
            center_y=y + 15,
        )
        on_bounds = _toggle_marker_bounds(
            GameSettingState.ON,
            panel="left",
            center_y=y + 15,
        )
        _paste_control(image, off_bounds, selected=True)
        _paste_control(image, on_bounds, selected=False)

        observation = observe_game_setting_row_with_control_assets(
            image,
            ENABLE_IDLE_SCREEN_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.OFF)
        off = observation.option_for(GameSettingState.OFF)
        on = observation.option_for(GameSettingState.ON)
        self.assertIsNotNone(off)
        self.assertIsNotNone(on)
        self.assertGreater(off.marker_activity, 0.0)
        self.assertLess(on.marker_activity, 0.0)

    def test_choice_cards_use_the_same_selected_unselected_assets(self) -> None:
        image = _frame()
        label = _box("Frame Rate Settings", 195, 114, 479, 155)
        fps_30 = _box("30 FPS", 401, 200, 492, 231)
        fps_60 = _box("60 FPS", 890, 200, 980, 231)
        _paste_control(image, _choice_marker_bounds(fps_30.bounds), selected=False)
        _paste_control(image, _choice_marker_bounds(fps_60.bounds), selected=True)

        observation = observe_game_setting_row_with_control_assets(
            image,
            FRAME_RATE_ROW,
            detections=(label, fps_30, fps_60),
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, FrameRateValue.FPS_60)

    def test_custom_ship_names_uses_generic_row_assets(self) -> None:
        image = _frame()
        y = 570
        detections = (_box("Custom Ship Names", 230, y, 475, y + 30),)
        _paste_control(
            image,
            _toggle_marker_bounds(
                GameSettingState.OFF,
                panel="left",
                center_y=y + 15,
            ),
            selected=True,
        )
        _paste_control(
            image,
            _toggle_marker_bounds(
                GameSettingState.ON,
                panel="left",
                center_y=y + 15,
            ),
            selected=False,
        )

        observation = observe_game_setting_row_with_control_assets(
            image,
            CUSTOM_SHIP_NAMES_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.OFF)

    def test_two_selected_controls_fail_closed_as_unknown(self) -> None:
        image = _frame()
        y = 300
        detections = (_box("Enable Idle Screen", 230, y, 460, y + 30),)
        for value in (GameSettingState.OFF, GameSettingState.ON):
            _paste_control(
                image,
                _toggle_marker_bounds(value, panel="left", center_y=y + 15),
                selected=True,
            )

        observation = observe_game_setting_row_with_control_assets(
            image,
            ENABLE_IDLE_SCREEN_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.UNKNOWN)

    def test_blank_marker_area_has_no_confident_asset_state(self) -> None:
        image = _frame()
        confidence = control_selection_confidence(
            image,
            _toggle_marker_bounds(
                GameSettingState.OFF,
                panel="left",
                center_y=300,
            ),
        )
        self.assertIsNone(confidence)


if __name__ == "__main__":
    unittest.main()
