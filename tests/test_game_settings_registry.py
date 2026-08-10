from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from typing import cast

import numpy as np

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    FrameRateValue,
    GameSettingChoiceRequirement,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
)
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GAME_SETTINGS_PREFLIGHT_REGISTRY,
    GAME_SETTINGS_PRODUCTION_KEYS,
    GameSettingCheckSpec,
    GameSettingDetector,
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


def _detector(_image: np.ndarray) -> GameSettingState | None:
    return GameSettingState.OFF


class GameSettingsRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definition_a = GameSettingDefinition(key="setting_a", location="options")
        self.definition_b = GameSettingDefinition(key="setting_b", location="options")
        self.requirement_a = GameSettingRequirement(
            definition=self.definition_a,
            expected_state=GameSettingState.OFF,
        )
        self.requirement_b = GameSettingRequirement(
            definition=self.definition_b,
            expected_state=GameSettingState.ON,
        )

    def test_valid_single_entry(self) -> None:
        entry = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=_detector,
            requirement=self.requirement_a,
        )
        registry = build_game_settings_registry((entry,))
        self.assertEqual(registry, (entry,))
        self.assertEqual(entry.key, "setting_a")

    def test_valid_multiple_entries_preserve_deterministic_order(self) -> None:
        entry_a = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=_detector,
            requirement=self.requirement_a,
        )
        entry_b = GameSettingCheckSpec(
            definition=self.definition_b,
            detector=_detector,
            requirement=self.requirement_b,
        )
        registry = build_game_settings_registry((entry_b, entry_a))
        self.assertEqual(tuple(entry.key for entry in registry), ("setting_b", "setting_a"))

    def test_duplicate_definition_key_fails_fast(self) -> None:
        duplicate_definition = GameSettingDefinition(
            key=self.definition_a.key,
            location="options",
        )
        entry_a = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=_detector,
        )
        duplicate_entry = GameSettingCheckSpec(
            definition=duplicate_definition,
            detector=_detector,
        )
        with self.assertRaisesRegex(ValueError, "Повторяющийся ключ registry"):
            build_game_settings_registry((entry_a, duplicate_entry))

    def test_requirement_definition_mismatch_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "другой настройке"):
            GameSettingCheckSpec(
                definition=self.definition_a,
                detector=_detector,
                requirement=self.requirement_b,
            )

    def test_requirement_value_family_mismatch_fails_fast(self) -> None:
        choice_requirement = GameSettingChoiceRequirement(
            definition=self.definition_a,
            expected_value=FrameRateValue.FPS_60,
        )
        with self.assertRaisesRegex(TypeError, "value family"):
            GameSettingCheckSpec(
                definition=self.definition_a,
                detector=cast(GameSettingDetector, lambda _image: FrameRateValue.FPS_60),
                requirement=choice_requirement,
            )

    def test_wrong_detector_family_is_rejected_when_result_is_built(self) -> None:
        entry = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=cast(GameSettingDetector, lambda _image: FrameRateValue.FPS_60),
            requirement=self.requirement_a,
        )
        with self.assertRaisesRegex(TypeError, "другой value family"):
            entry.make_result(FrameRateValue.FPS_60)

    def test_required_entry_without_observer_fails_enforce_registry_validation(self) -> None:
        entry = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=_detector,
            requirement=self.requirement_a,
        )
        with self.assertRaisesRegex(ValueError, "mutator observer"):
            build_game_settings_registry((entry,), require_enforce=True)

    def test_entry_is_immutable(self) -> None:
        entry = GameSettingCheckSpec(
            definition=self.definition_a,
            detector=_detector,
            requirement=self.requirement_a,
        )
        with self.assertRaises(FrozenInstanceError):
            entry.requirement = None

    def test_non_callable_detector_fails_fast(self) -> None:
        invalid_detector = cast(GameSettingDetector, None)
        with self.assertRaisesRegex(TypeError, "detector должен быть callable"):
            GameSettingCheckSpec(
                definition=self.definition_a,
                detector=invalid_detector,
            )

    def test_empty_registry_is_valid_deterministic_tuple(self) -> None:
        registry = build_game_settings_registry()
        self.assertEqual(registry, ())
        self.assertIsInstance(registry, tuple)

    def test_legacy_compat_registry_keeps_custom_ship_names_contract(self) -> None:
        self.assertEqual(len(GAME_SETTINGS_PREFLIGHT_REGISTRY), 1)
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

    def test_custom_ship_names_production_entry_uses_generic_row_state_path(self) -> None:
        entry = GAME_SETTINGS_OPTIONS_REGISTRY[-1]
        self.assertIs(entry.definition, CUSTOM_SHIP_NAMES)
        self.assertIs(entry.requirement, CUSTOM_SHIP_NAMES_REQUIRED_OFF)
        self.assertIsNot(entry.detector, detect_custom_ship_names)
        self.assertTrue(callable(entry.detector))
        self.assertTrue(callable(entry.observer))
        self.assertEqual(entry.key, "custom_ship_names")


if __name__ == "__main__":
    unittest.main()
