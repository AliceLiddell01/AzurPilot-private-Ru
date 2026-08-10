from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
    DUPLICATE_SHIP_DISPLAY,
    DUPLICATE_SHIP_DISPLAY_REQUIRED_OFF,
    ENABLE_IDLE_SCREEN,
    ENABLE_IDLE_SCREEN_REQUIRED_OFF,
    FRAME_RATE,
    FRAME_RATE_REQUIRED_60_FPS,
    OPSI_AUTO_USE_ITEMS,
    OPSI_AUTO_USE_ITEMS_REQUIRED_ON,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_REQUIRED_OFF,
    OPSI_REDUCE_TB_GUIDANCE,
    OPSI_REDUCE_TB_GUIDANCE_REQUIRED_ON,
    STORY_AUTOPLAY,
    STORY_AUTOPLAY_REQUIRED_ENABLED,
    TEXT_AUTO_SCROLL_SPEED,
    TEXT_AUTO_SCROLL_SPEED_REQUIRED_VERY_FAST,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    FrameRateValue,
    GameSettingChoiceCheckResult,
    GameSettingState,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
)
from module.game_settings.options_detector import (
    CUSTOM_SHIP_NAMES_ROW,
    GameSettingRowObservation,
)
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GAME_SETTINGS_PREFLIGHT_REGISTRY,
    GAME_SETTINGS_PRODUCTION_KEYS,
    OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW,
    build_game_settings_registry,
)


EXPECTED_PRODUCTION_KEYS = (
    "frame_rate",
    "opsi_reduce_tb_guidance",
    "opsi_auto_use_items",
    "opsi_default_auto_mode_threat_safe",
    "story_autoplay",
    "text_auto_scroll_speed",
    "enable_idle_screen",
    "duplicate_ship_display",
    "display_quick_switch_prompt",
    "display_battle_result_cutscene",
    "custom_ship_names",
)


class GameSettingsRegistryTests(unittest.TestCase):
    def test_legacy_registry_preserves_custom_ship_names_detector_contract(self) -> None:
        self.assertEqual(len(GAME_SETTINGS_PREFLIGHT_REGISTRY), 1)
        entry = GAME_SETTINGS_PREFLIGHT_REGISTRY[0]
        self.assertIs(entry.definition, CUSTOM_SHIP_NAMES)
        self.assertIs(entry.requirement, CUSTOM_SHIP_NAMES_REQUIRED_OFF)
        self.assertIs(entry.detector, detect_custom_ship_names)

    def test_full_registry_contains_expected_required_definitions(self) -> None:
        by_key = {entry.key: entry for entry in GAME_SETTINGS_OPTIONS_REGISTRY}
        self.assertIs(by_key["frame_rate"].definition, FRAME_RATE)
        self.assertIs(by_key["frame_rate"].requirement, FRAME_RATE_REQUIRED_60_FPS)
        self.assertIs(by_key["opsi_reduce_tb_guidance"].definition, OPSI_REDUCE_TB_GUIDANCE)
        self.assertIs(
            by_key["opsi_reduce_tb_guidance"].requirement,
            OPSI_REDUCE_TB_GUIDANCE_REQUIRED_ON,
        )
        self.assertIs(by_key["opsi_auto_use_items"].definition, OPSI_AUTO_USE_ITEMS)
        self.assertIs(
            by_key["opsi_auto_use_items"].requirement,
            OPSI_AUTO_USE_ITEMS_REQUIRED_ON,
        )
        self.assertIs(
            by_key["opsi_default_auto_mode_threat_safe"].definition,
            OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE,
        )
        self.assertIs(
            by_key["opsi_default_auto_mode_threat_safe"].requirement,
            OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_REQUIRED_OFF,
        )
        self.assertIs(by_key["story_autoplay"].definition, STORY_AUTOPLAY)
        self.assertIs(
            by_key["story_autoplay"].requirement,
            STORY_AUTOPLAY_REQUIRED_ENABLED,
        )
        self.assertIs(by_key["text_auto_scroll_speed"].definition, TEXT_AUTO_SCROLL_SPEED)
        self.assertIs(
            by_key["text_auto_scroll_speed"].requirement,
            TEXT_AUTO_SCROLL_SPEED_REQUIRED_VERY_FAST,
        )
        self.assertIs(by_key["enable_idle_screen"].definition, ENABLE_IDLE_SCREEN)
        self.assertIs(
            by_key["enable_idle_screen"].requirement,
            ENABLE_IDLE_SCREEN_REQUIRED_OFF,
        )
        self.assertIs(by_key["duplicate_ship_display"].definition, DUPLICATE_SHIP_DISPLAY)
        self.assertIs(
            by_key["duplicate_ship_display"].requirement,
            DUPLICATE_SHIP_DISPLAY_REQUIRED_OFF,
        )
        self.assertIs(by_key["custom_ship_names"].definition, CUSTOM_SHIP_NAMES)
        self.assertIs(
            by_key["custom_ship_names"].requirement,
            CUSTOM_SHIP_NAMES_REQUIRED_OFF,
        )

    def test_choice_entries_build_choice_result_family(self) -> None:
        by_key = {entry.key: entry for entry in GAME_SETTINGS_OPTIONS_REGISTRY}
        result = by_key["frame_rate"].make_result(FrameRateValue.FPS_60)
        self.assertIsInstance(result, GameSettingChoiceCheckResult)
        self.assertIs(result.detected_value, FrameRateValue.FPS_60)
        self.assertIs(result.required_value, FrameRateValue.FPS_60)

        story = by_key["story_autoplay"].make_result(StoryAutoplayValue.ENABLED)
        self.assertIsInstance(story, GameSettingChoiceCheckResult)
        speed = by_key["text_auto_scroll_speed"].make_result(
            TextAutoScrollSpeedValue.VERY_FAST
        )
        self.assertIsInstance(speed, GameSettingChoiceCheckResult)

    def test_registry_rejects_duplicate_keys(self) -> None:
        entry = GAME_SETTINGS_OPTIONS_REGISTRY[0]
        with self.assertRaisesRegex(ValueError, "Повторяющийся ключ"):
            build_game_settings_registry((entry, entry))

    def test_registry_rejects_required_entry_without_observer_for_enforce(self) -> None:
        legacy = GAME_SETTINGS_PREFLIGHT_REGISTRY[0]
        with self.assertRaisesRegex(ValueError, "не имеет mutator observer"):
            build_game_settings_registry((legacy,), require_enforce=True)

    def test_full_registry_has_observer_and_row_spec_for_every_required_entry(self) -> None:
        self.assertTrue(
            all(entry.enforce_supported for entry in GAME_SETTINGS_OPTIONS_REGISTRY)
        )
        self.assertTrue(
            all(entry.row_spec is not None for entry in GAME_SETTINGS_OPTIONS_REGISTRY)
        )

    def test_legacy_registry_stays_single_setting_contract(self) -> None:
        self.assertEqual(
            tuple(entry.key for entry in GAME_SETTINGS_PREFLIGHT_REGISTRY),
            ("custom_ship_names",),
        )
        entry = GAME_SETTINGS_PREFLIGHT_REGISTRY[0]
        self.assertIs(entry.definition, CUSTOM_SHIP_NAMES)
        self.assertIs(entry.requirement, CUSTOM_SHIP_NAMES_REQUIRED_OFF)
        self.assertIs(entry.detector, detect_custom_ship_names)

    def test_production_registry_has_exact_authoritative_key_set(self) -> None:
        self.assertEqual(GAME_SETTINGS_PRODUCTION_KEYS, EXPECTED_PRODUCTION_KEYS)
        self.assertEqual(
            tuple(entry.key for entry in GAME_SETTINGS_OPTIONS_REGISTRY),
            EXPECTED_PRODUCTION_KEYS,
        )
        self.assertNotIn("no_sleep_mode_on_main_menu", GAME_SETTINGS_PRODUCTION_KEYS)
        self.assertIn("enable_idle_screen", GAME_SETTINGS_PRODUCTION_KEYS)
        self.assertTrue(all(entry.enforce_supported for entry in GAME_SETTINGS_OPTIONS_REGISTRY))

    def test_live_secured_marquee_fragment_is_used_by_production_detector(self) -> None:
        entry = GAME_SETTINGS_OPTIONS_REGISTRY[3]
        self.assertEqual(entry.key, "opsi_default_auto_mode_threat_safe")
        self.assertIs(entry.row_spec, OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW)
        self.assertIn(
            "Mode in secured",
            OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW.label_aliases,
        )
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        observation = GameSettingRowObservation(
            value=GameSettingState.OFF,
            row_bounds=(214, 280, 679, 340),
            options=(),
        )
        with patch(
            "module.game_settings.registry.observe_game_setting_row_with_control_assets",
            return_value=observation,
        ) as observer:
            self.assertIs(entry.detector(image), GameSettingState.OFF)
        observer.assert_called_once_with(
            image,
            OPSI_DEFAULT_AUTO_MODE_THREAT_SAFE_PRODUCTION_ROW,
        )

    def test_custom_ship_names_row_does_not_alias_distinct_oath_control(self) -> None:
        self.assertEqual(
            CUSTOM_SHIP_NAMES_ROW.label_aliases,
            ("Custom Ship Names",),
        )
        self.assertNotIn(
            "Change Oathed Ship Names",
            CUSTOM_SHIP_NAMES_ROW.label_aliases,
        )

    def test_custom_ship_names_production_entry_uses_one_authoritative_row_spec(self) -> None:
        entry = GAME_SETTINGS_OPTIONS_REGISTRY[-1]
        self.assertIs(entry.definition, CUSTOM_SHIP_NAMES)
        self.assertIs(entry.requirement, CUSTOM_SHIP_NAMES_REQUIRED_OFF)
        self.assertIs(entry.row_spec, CUSTOM_SHIP_NAMES_ROW)
        self.assertIsNot(entry.detector, detect_custom_ship_names)


if __name__ == "__main__":
    unittest.main()