from __future__ import annotations

import sys
import types
import unittest

import numpy as np

import module.game_settings.options_detector as detector_module
from module.game_settings.model import (
    FrameRateValue,
    GameSettingState,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.options_detector import (
    CUSTOM_SHIP_NAMES_ROW,
    DISPLAY_BATTLE_RESULT_CUTSCENE_ROW,
    DISPLAY_QUICK_SWITCH_PROMPT_ROW,
    ENABLE_IDLE_SCREEN_ROW,
    FRAME_RATE_ROW,
    OPSI_AUTO_USE_ITEMS_ROW,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW,
    OPSI_REDUCE_TB_GUIDANCE_ROW,
    STORY_AUTOPLAY_ROW,
    TEXT_AUTO_SCROLL_SPEED_ROW,
    GameSettingOptionSpec,
    GameSettingRowSpec,
    OcrTextBox,
    _FRAME_OCR_CACHE,
    _choice_marker_bounds,
    _toggle_marker_bounds,
    clear_game_settings_ocr_cache,
    detect_game_setting_row,
    observe_game_setting_row,
)


BACKGROUND = 96


def _frame() -> np.ndarray:
    return np.full((720, 1280, 3), BACKGROUND, dtype=np.uint8)


def _box(text: str, x1: int, y1: int, x2: int, y2: int) -> OcrTextBox:
    return OcrTextBox(text=text, bounds=(x1, y1, x2, y2), score=0.99)


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


def _paint_toggle(
    image: np.ndarray,
    *,
    panel: str,
    center_y: float,
    selected: GameSettingState,
) -> None:
    for value in (GameSettingState.OFF, GameSettingState.ON):
        _paint_marker_bounds(
            image,
            _toggle_marker_bounds(value, panel=panel, center_y=center_y),
            selected=value is selected,
        )


def _paint_choice(
    image: np.ndarray,
    option_boxes: tuple[OcrTextBox, ...],
    values,
    selected,
) -> None:
    for box, value in zip(option_boxes, values, strict=True):
        _paint_marker_bounds(
            image,
            _choice_marker_bounds(box.bounds),
            selected=value is selected,
        )


class GameSettingsOptionsDetectorTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_game_settings_ocr_cache()

    def test_two_column_toggle_uses_label_panel_not_full_y_group(self) -> None:
        image = _frame()
        y = 320
        detections = (
            _box("Reduce TB Guidance Off", 230, y, 528, y + 30),
            _box("On", 586, y, 621, y + 30),
            _box("items during Auto Off", 716, y, 1018, y + 30),
            _box("On", 1076, y, 1111, y + 30),
        )
        _paint_toggle(image, panel="left", center_y=y + 15, selected=GameSettingState.ON)
        _paint_toggle(image, panel="right", center_y=y + 15, selected=GameSettingState.OFF)

        observation = observe_game_setting_row(
            image,
            OPSI_REDUCE_TB_GUIDANCE_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.ON)
        self.assertEqual(len(observation.options), 2)
        self.assertLess(observation.row_bounds[2], 700)

    def test_merged_label_and_off_text_does_not_hide_toggle_value(self) -> None:
        image = _frame()
        y = 260
        detections = (
            _box("Enable Idle Screen Off", 230, y, 527, y + 30),
            _box("On", 586, y, 621, y + 30),
            _box("Play Voice Lines on Idle Screen Off", 715, y, 1018, y + 30),
            _box("On", 1076, y, 1111, y + 30),
        )
        _paint_toggle(image, panel="left", center_y=y + 15, selected=GameSettingState.OFF)
        _paint_toggle(image, panel="right", center_y=y + 15, selected=GameSettingState.ON)

        observation = observe_game_setting_row(
            image,
            ENABLE_IDLE_SCREEN_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.OFF)

    def test_split_label_boxes_are_recombined_before_panel_detection(self) -> None:
        image = _frame()
        y = 230
        detections = (
            _box("Enable", 230, y, 300, y + 25),
            _box("Idle", 307, y, 350, y + 25),
            _box("Screen", 357, y, 430, y + 25),
            _box("Off", 490, y, 527, y + 25),
            _box("On", 586, y, 621, y + 25),
        )
        _paint_toggle(image, panel="left", center_y=y + 12.5, selected=GameSettingState.ON)

        observation = observe_game_setting_row(
            image,
            ENABLE_IDLE_SCREEN_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.ON)

    def test_live_marquee_fragments_anchor_required_toggle_rows(self) -> None:
        cases = (
            (OPSI_AUTO_USE_ITEMS_ROW, "ems during Auto Sear off", "right", GameSettingState.ON),
            (
                OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_ROW,
                "Auto Mode in secured off",
                "left",
                GameSettingState.OFF,
            ),
            (
                DISPLAY_QUICK_SWITCH_PROMPT_ROW,
                "Luick-Switch Prompt of",
                "right",
                GameSettingState.OFF,
            ),
            (
                DISPLAY_BATTLE_RESULT_CUTSCENE_ROW,
                "Ittle Result Cutscene Off",
                "left",
                GameSettingState.ON,
            ),
        )
        for spec, text, panel, selected in cases:
            with self.subTest(text=text):
                image = _frame()
                y = 360
                x1, x2 = (230, 528) if panel == "left" else (716, 1018)
                detections = (_box(text, x1, y, x2, y + 30),)
                _paint_toggle(
                    image,
                    panel=panel,
                    center_y=y + 15,
                    selected=selected,
                )
                observation = observe_game_setting_row(
                    image,
                    spec,
                    detections=detections,
                )
                self.assertIsNotNone(observation)
                self.assertIs(observation.value, selected)

    def test_live_oathed_ship_name_label_uses_custom_ship_name_row_geometry(self) -> None:
        image = _frame()
        y = 360
        detections = (_box("Change Oathed Ship Names", 716, y, 1018, y + 30),)
        _paint_toggle(
            image,
            panel="right",
            center_y=y + 15,
            selected=GameSettingState.ON,
        )

        observation = observe_game_setting_row(
            image,
            CUSTOM_SHIP_NAMES_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.ON)
        self.assertEqual(len(observation.options), 2)

    def test_frame_rate_choice_reads_marker_left_of_option_text(self) -> None:
        image = _frame()
        label = _box("Frame Rate Settings", 195, 114, 479, 155)
        options = (
            _box("30 FPS", 401, 200, 492, 231),
            _box("60 FPS", 890, 200, 980, 231),
        )
        _paint_choice(
            image,
            options,
            (FrameRateValue.FPS_30, FrameRateValue.FPS_60),
            FrameRateValue.FPS_60,
        )

        observation = observe_game_setting_row(
            image,
            FRAME_RATE_ROW,
            detections=(label, *options),
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, FrameRateValue.FPS_60)
        target = observation.option_for(FrameRateValue.FPS_60)
        self.assertIsNotNone(target)
        self.assertLess(target.click_bounds[0], options[1].bounds[0])

    def test_story_autoplay_choice_reads_selected_card(self) -> None:
        image = _frame()
        label = _box("Story Autoplay", 195, 292, 403, 331)
        options = (
            _box("Disabled", 388, 363, 504, 396),
            _box("Enabled", 883, 365, 989, 395),
        )
        _paint_choice(
            image,
            options,
            (StoryAutoplayValue.DISABLED, StoryAutoplayValue.ENABLED),
            StoryAutoplayValue.ENABLED,
        )

        observation = observe_game_setting_row(
            image,
            STORY_AUTOPLAY_ROW,
            detections=(label, *options),
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, StoryAutoplayValue.ENABLED)

    def test_four_choice_speed_uses_two_rows_and_two_columns(self) -> None:
        image = _frame()
        label = _box("Text Auto-Scroll Speed", 195, 474, 509, 514)
        options = (
            _box("Slow", 412, 553, 481, 585),
            _box("Normal", 886, 552, 986, 585),
            _box("Fast", 412, 626, 478, 661),
            _box("Very Fast", 874, 626, 998, 661),
        )
        values = (
            TextAutoScrollSpeedValue.SLOW,
            TextAutoScrollSpeedValue.NORMAL,
            TextAutoScrollSpeedValue.FAST,
            TextAutoScrollSpeedValue.VERY_FAST,
        )
        _paint_choice(
            image,
            options,
            values,
            TextAutoScrollSpeedValue.VERY_FAST,
        )

        observation = observe_game_setting_row(
            image,
            TEXT_AUTO_SCROLL_SPEED_ROW,
            detections=(label, *options),
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, TextAutoScrollSpeedValue.VERY_FAST)
        self.assertEqual(len(observation.options), 4)

    def test_split_very_fast_option_is_resolved_as_one_choice(self) -> None:
        image = _frame()
        label = _box("Text Auto-Scroll Speed", 195, 474, 509, 514)
        detections = (
            label,
            _box("Slow", 412, 553, 481, 585),
            _box("Normal", 886, 552, 986, 585),
            _box("Fast", 412, 626, 478, 661),
            _box("Very", 874, 626, 915, 661),
            _box("Fast", 922, 626, 998, 661),
        )
        marker_box = _box("Very Fast", 874, 626, 998, 661)
        values = (
            TextAutoScrollSpeedValue.SLOW,
            TextAutoScrollSpeedValue.NORMAL,
            TextAutoScrollSpeedValue.FAST,
            TextAutoScrollSpeedValue.VERY_FAST,
        )
        option_boxes = (
            detections[1],
            detections[2],
            detections[3],
            marker_box,
        )
        _paint_choice(
            image,
            option_boxes,
            values,
            TextAutoScrollSpeedValue.VERY_FAST,
        )

        observation = observe_game_setting_row(
            image,
            TEXT_AUTO_SCROLL_SPEED_ROW,
            detections=detections,
        )

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, TextAutoScrollSpeedValue.VERY_FAST)

    def test_equal_marker_activity_returns_unknown_not_guess(self) -> None:
        image = _frame()
        y = 300
        detections = (_box("Example Setting", 230, y, 450, y + 30),)
        spec = GameSettingRowSpec(
            label_aliases=("Example Setting",),
            options=(
                GameSettingOptionSpec(GameSettingState.OFF, ("Off",)),
                GameSettingOptionSpec(GameSettingState.ON, ("On",)),
            ),
        )
        for value in (GameSettingState.OFF, GameSettingState.ON):
            _paint_marker_bounds(
                image,
                _toggle_marker_bounds(value, panel="left", center_y=y + 15),
                selected=True,
            )

        observation = observe_game_setting_row(image, spec, detections=detections)

        self.assertIsNotNone(observation)
        self.assertIs(observation.value, GameSettingState.UNKNOWN)

    def test_unrelated_label_returns_none(self) -> None:
        image = _frame()
        y = 300
        detections = (_box("Completely Different Setting", 230, y, 500, y + 30),)
        _paint_toggle(image, panel="left", center_y=y + 15, selected=GameSettingState.ON)

        self.assertIsNone(
            observe_game_setting_row(
                image,
                OPSI_REDUCE_TB_GUIDANCE_ROW,
                detections=detections,
            )
        )

    def test_wrong_resolution_fails_closed(self) -> None:
        image = _frame()
        with self.assertRaisesRegex(ValueError, "1280 x 720"):
            observe_game_setting_row(
                image[:700],
                ENABLE_IDLE_SCREEN_ROW,
                detections=(),
            )

    def test_one_frame_runs_one_ocr_and_grouping_pass_for_multiple_detectors(self) -> None:
        image = _frame()
        y = 300
        detections = (_box("Enable Idle Screen", 230, y, 460, y + 30),)
        _paint_toggle(image, panel="left", center_y=y + 15, selected=GameSettingState.OFF)
        calls = {"det": 0, "groups": 0}

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
        original_module = sys.modules.get("module.ocr.al_ocr")
        original_engine = _FRAME_OCR_CACHE._ocr
        original_grouping = detector_module._same_line_groups

        def counting_grouping(items):
            calls["groups"] += 1
            return original_grouping(items)

        sys.modules["module.ocr.al_ocr"] = fake_module
        _FRAME_OCR_CACHE._ocr = None
        detector_module._same_line_groups = counting_grouping
        clear_game_settings_ocr_cache()
        try:
            first = detect_game_setting_row(image, ENABLE_IDLE_SCREEN_ROW)
            second = detect_game_setting_row(image, ENABLE_IDLE_SCREEN_ROW)
            self.assertIs(first, GameSettingState.OFF)
            self.assertIs(second, GameSettingState.OFF)
            self.assertEqual(calls["det"], 1)
            self.assertEqual(calls["groups"], 1)
        finally:
            clear_game_settings_ocr_cache()
            detector_module._same_line_groups = original_grouping
            _FRAME_OCR_CACHE._ocr = original_engine
            if original_module is None:
                sys.modules.pop("module.ocr.al_ocr", None)
            else:
                sys.modules["module.ocr.al_ocr"] = original_module


if __name__ == "__main__":
    unittest.main()
