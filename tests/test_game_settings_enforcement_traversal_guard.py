from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.enforcement import GameSettingsEnforcementScanner
from module.game_settings.model import (
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
    GameSettingsScanResult,
)
from module.game_settings.registry import GameSettingCheckSpec, build_game_settings_registry
from module.game_settings.traversal import OptionsTraversalResult


class _IncompleteApplyScanner(GameSettingsEnforcementScanner):
    def __init__(self) -> None:
        definition = GameSettingDefinition(key="setting_a", location="options")
        requirement = GameSettingRequirement(definition, GameSettingState.ON)

        def detector(_image: np.ndarray):
            return GameSettingState.OFF

        def observer(_image: np.ndarray):
            return None

        self.entry = GameSettingCheckSpec(
            definition=definition,
            detector=detector,
            requirement=requirement,
            observer=observer,
        )
        self.check_registry = build_game_settings_registry(
            (self.entry,),
            require_enforce=True,
        )
        self.device = type(
            "Device",
            (),
            {"image": np.zeros((720, 1280, 3), dtype=np.uint8)},
        )()
        self.return_calls = 0

    def scan_game_settings(self) -> GameSettingsScanResult:
        return GameSettingsScanResult(
            (self.entry.make_result(GameSettingState.OFF),)
        )

    def traverse_options(self, _visitor) -> OptionsTraversalResult:
        return OptionsTraversalResult(
            visited_viewports=1,
            final_offset=0.0,
            reached_bottom=False,
            stopped_early=False,
        )

    def return_to_main(self) -> bool:
        self.return_calls += 1
        return True


class GameSettingsEnforcementTraversalGuardTests(unittest.TestCase):
    def test_incomplete_apply_traversal_reports_distinct_fail_closed_reason(self) -> None:
        scanner = _IncompleteApplyScanner()

        result = scanner.enforce_required_game_settings()

        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("завершился до поиска всех строк", result.failure_reason)
        self.assertEqual(result.changed_keys, ())
        self.assertEqual(scanner.return_calls, 1)


if __name__ == "__main__":
    unittest.main()
