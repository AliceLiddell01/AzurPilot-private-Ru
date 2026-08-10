from __future__ import annotations

import sys
import types
import unittest

import numpy as np

from module.game_settings.model import (
    FrameRateValue,
    GameSettingState,
    TextAutoScrollSpeedValue,
)
from module.game_settings.options_detector import (
    GameSettingOptionSpec,
    GameSettingRowSpec,
    OcrTextBox,
    clear_game_settings_ocr_cache,
    detect_game_setting_row,
    observe_game_setting_row,
)


BACKGROUND = 96


def _frame() -> np.ndarray:
    return np.full((720, 1280, 3), BACKGROUND, dtype=np.uint8)


def _box(text: str, x1: int, y1: int, x2: int, y2: int) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=(x1, y1, x2, y2), score=0.99)


def _paint_marker(
    image: np.ndarray,
    option_bounds: tuple[int, int, int, int],
    *,
    selected: bool,
) -> None:
    _x1, y1, x2, y2 = option_bounds
    cy = int(round((y1 + y2) / 2.0))
    left = x2 + 2
    top = cy - 15
    right = left + 30
    bottom = cy + 15
    if selected:
        image[top + 5 : bottom - 5, left + 5 : right - 5] = 230
    else:
        image[top + 10 : bottom - 10, left + 10 : right - 10] = 150


def _toggle_fixture(y: int = 200, selected: GameSettingState = GameSettingState.OFF):
    image = _frame()
    label = _box("Example Setting", 250, y, 430, y + 20)
    off = _box("Off", 520, y, 550, y + 20)
    on = _box("On", 650, y, 675, y + 20)
    _paint_marker(image, off.bounds, selected=selected is GameSettingState.OFF)
    _paint_marker(image, on.bounds, selected=selected is GameSettingState.ON)
    detections = (label, off, on)
    spec = GameSettingRowSpec(
        label_aliases=("Example Setting",),
        options=(
            GameSettingOptionSpec(GameSettingState.OFF, ("Off",)),
            GameSettingOptionSpec(GameSettingState.ON, ("On",)),
        ),
    )
    return image, detections, spec


def _speed_spec() -> GameSettingRowSpec:
    return GameSettingRowSpec(
        label_aliases=("Text Auto-Scroll Speed",),
        options=(
            GameSettingOptionSpec(TextAutoScrollSpeedValue.SLOW, ("Slow",)),
            GameSettingOptionSpec(TextAutoScrollSpeedValue.NORMAL, ("Normal",)),
            GameSettingOptionSpec(TextAutoScrollSpeedValue.FAST, ("Fast",)),
            GameSettingOptionSpec(
                TextAutoScrollSpeedValue.VERY_FAST,
                ("Very Fast",),
            ),
        ),
    )


class GameSettingsOptionsDetectorTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_game_settings_ocr_cache()

    def test_toggle_required_and_mismatch_states_are_resolved_row_locally(self) -> None:
        for selected in (GameSettingState.OFF, GameSettingState.ON):
            with self.subTest(selected=selected):
                image, detections, spec = _toggle_fixture(selected=selected)
                observation = observe_game_setting_row(
                    image,
                    spec,
                    detections=detections,
                )
                self.assertIsNotNone(observation)
                self.assertIs(observation.value, selected)
                self.assertIsNotNone(observation.option_for(GameSettingState.OFF))
                self.assertIsNotNone(observation.option_for(GameSettingState.ON))

    def test_row_anchor_tracks_dynamic_y_without_absolute_click_y(self) -> None:
        top_image, top_detections, spec = _toggle_fixture(y=180)
        lower_image, lower_detections, _ = _toggle_fixture(y=420)
        top = observe_game_setting_row(top_image, spec, detections=top_detections)
        lower = observe_game_setting_row(lower_image, spec, detections=lower_detections)
        self.assertIsNotNone(top)
        self.assertIsNotNone(lower)
        top_target = top.option_for(GameSettingState.ON).click_bounds
        lower_target = lower.option_for(GameSettingState.ON).click_bounds
        self.assertGreater(lower_target[1] - top_target[1], 200)
        self.assertEqual(top_target[0], lower_target[0])

    def test_neighbor_row_does_not_match_requested_label(self) -> None:
        image, detections, _ = _toggle_fixture()
        different = GameSettingRowSpec(
            label_aliases=("Completely Different Setting",),
            options=(
                GameSettingOptionSpec(GameSettingState.OFF, ("Off",)),
                GameSettingOptionSpec(GameSettingState.ON, ("On",)),
            ),
        )
        self.assertIsNone(
            observe_game_setting_row(image, different, detections=detections)
        )

    def test_split_enable_idle_screen_label_cannot_be_reused_as_enable_option(self) -> None:
        image = _frame()
        y = 230
        detections = (
            _box("Enable", 250, y, 300, y + 20),
            _box("Idle", 307, y, 337, y + 20),
            _box("Screen", 344, y, 397, y + 20),
            _box("Off", 540, y, 570, y + 20),
            _box("On", 660, y, 685, y + 20),
        )
        _paint_marker(image, detections[3].bounds, selected=True)
        _paint_marker(image, detections[4].bounds, selected=False)
        spec = GameSettingRowSpec(
            label_aliases=("Enable Idle Screen",),
            options=(
                GameSettingOptionSpec(GameSettingState.OFF, ("Off", "Disabled")),
                GameSettingOptionSpec(GameSettingState.ON, ("On", "Enable")),
            ),
        )
        observation = observe_game_setting_row(image, spec, detections=detections)
        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.OFF)
        on_target = observation.option_for(GameSettingState.ON)
        self.assertIsNotNone(on_target)
        self.assertEqual(on_target.bounds, detections[4].bounds)

    def test_ambiguous_marker_activity_returns_unknown_not_guess(self) -> None:
        image, detections, spec = _toggle_fixture()
        off = detections[1]
        on = detections[2]
        image[:] = BACKGROUND
        _paint_marker(image, off.bounds, selected=True)
        _paint_marker(image, on.bounds, selected=True)
        observation = observe_game_setting_row(image, spec, detections=detections)
        self.assertIs(observation.value, GameSettingState.UNKNOWN)

    def test_multiword_choice_uses_precise_group_and_selected_marker(self) -> None:
        image = _frame()
        y = 300
        detections = (
            _box("Text Auto-Scroll Speed", 250, y, 450, y + 20),
            _box("Slow", 510, y, 545, y + 20),
            _box("Normal", 610, y, 665, y + 20),
            _box("Fast", 730, y, 765, y + 20),
            _box("Very", 850, y, 885, y + 20),
            _box("Fast", 892, y, 925, y + 20),
        )
        for box in detections[1:4]:
            _paint_marker(image, box.bounds, selected=False)
        very_fast_bounds = (850, y, 925, y + 20)
        _paint_marker(image, very_fast_bounds, selected=True)
        observation = observe_game_setting_row(
            image,
            _speed_spec(),
            detections=detections,
        )
        self.assertIs(observation.value, TextAutoScrollSpeedValue.VERY_FAST)

    def test_foreign_duplicate_fast_option_fails_closed_instead_of_guessing(self) -> None:
        image = _frame()
        y = 330
        detections = (
            _box("Text Auto-Scroll Speed", 250, y, 450, y + 20),
            _box("Slow", 510, y, 545, y + 20),
            _box("Normal", 610, y, 665, y + 20),
            _box("Fast", 730, y, 765, y + 20),
            _box("Very", 850, y, 885, y + 20),
            _box("Fast", 892, y, 925, y + 20),
            _box("Fast", 1010, y, 1045, y + 20),
        )
        for box in detections[1:]:
            _paint_marker(image, box.bounds, selected=False)
        _paint_marker(image, (850, y, 925, y + 20), selected=True)
        observation = observe_game_setting_row(
            image,
            _speed_spec(),
            detections=detections,
        )
        self.assertIsNotNone(observation)
        self.assertIs(observation.value, TextAutoScrollSpeedValue.UNKNOWN)
        self.assertEqual(observation.options, ())

    def test_choice_unknown_when_one_option_is_not_uniquely_located(self) -> None:
        image = _frame()
        y = 260
        detections = (
            _box("Frame Rate", 250, y, 380, y + 20),
            _box("60 FPS", 650, y, 705, y + 20),
        )
        _paint_marker(image, detections[1].bounds, selected=True)
        spec = GameSettingRowSpec(
            label_aliases=("Frame Rate",),
            options=(
                GameSettingOptionSpec(FrameRateValue.FPS_30, ("30 FPS",)),
                GameSettingOptionSpec(FrameRateValue.FPS_60, ("60 FPS",)),
            ),
        )
        observation = observe_game_setting_row(image, spec, detections=detections)
        self.assertIsNotNone(observation)
        self.assertIs(observation.value, FrameRateValue.UNKNOWN)
        self.assertEqual(observation.options, ())

    def test_wrong_resolution_fails_closed(self) -> None:
        image, detections, spec = _toggle_fixture()
        with self.assertRaisesRegex(ValueError, "1280 x 720"):
            observe_game_setting_row(
                image[:700],
                spec,
                detections=detections,
            )

    def test_one_frame_runs_one_ocr_pass_for_multiple_detectors(self) -> None:
        image, detections, spec = _toggle_fixture(selected=GameSettingState.ON)
        calls = {"det": 0}

        class FakeAlOcr:
            def __init__(self, **_kwargs) -> None:
                pass

            def det(self, _image: np.ndarray):
                calls["det"] += 1
                raw = []
                for item in detections:
                    x1, y1, x2, y2 = item.bounds
                    raw.append(
                        (
                            item.text,
                            [
                                [float(x1), float(y1)],
                                [float(x2), float(y1)],
                                [float(x2), float(y2)],
                                [float(x1), float(y2)],
                            ],
                            item.score,
                        )
                    )
                return raw

        fake_module = types.ModuleType("module.ocr.al_ocr")
        fake_module.AlOcr = FakeAlOcr
        original = sys.modules.get("module.ocr.al_ocr")
        sys.modules["module.ocr.al_ocr"] = fake_module
        clear_game_settings_ocr_cache()
        try:
            first = detect_game_setting_row(image, spec)
            second = detect_game_setting_row(image, spec)
            self.assertIs(first, GameSettingState.ON)
            self.assertIs(second, GameSettingState.ON)
            self.assertEqual(calls["det"], 1)
        finally:
            clear_game_settings_ocr_cache()
            if original is None:
                sys.modules.pop("module.ocr.al_ocr", None)
            else:
                sys.modules["module.ocr.al_ocr"] = original


if __name__ == "__main__":
    unittest.main()
