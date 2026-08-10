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
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
)
from module.game_settings.registry import (
    GAME_SETTINGS_PREFLIGHT_REGISTRY,
    GameSettingCheckSpec,
    GameSettingDetector,
    build_game_settings_registry,
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

    def test_production_registry_contains_only_custom_ship_names(self) -> None:
        self.assertEqual(len(GAME_SETTINGS_PREFLIGHT_REGISTRY), 1)
        entry = GAME_SETTINGS_PREFLIGHT_REGISTRY[0]
        self.assertIs(entry.definition, CUSTOM_SHIP_NAMES)
        self.assertIs(entry.requirement, CUSTOM_SHIP_NAMES_REQUIRED_OFF)
        self.assertIs(entry.detector, detect_custom_ship_names)
        self.assertEqual(entry.key, "custom_ship_names")


if __name__ == "__main__":
    unittest.main()
