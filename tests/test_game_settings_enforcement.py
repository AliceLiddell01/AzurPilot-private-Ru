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
from module.game_settings.options_detector import (
    GameSettingOptionObservation,
    GameSettingRowObservation,
)
from module.game_settings.registry import (
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.traversal import OptionsTraversalResult, OptionsViewport


class _FakeDevice:
    def __init__(self, owner: "_FakeEnforcementScanner") -> None:
        self.owner = owner
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.clicks: list[tuple[str, tuple[int, int, int, int]]] = []

    def click(self, button) -> None:
        self.clicks.append((button.name, button.button))
        prefix = "GAME_SETTINGS_"
        suffix = "_TARGET"
        key = button.name[len(prefix) : -len(suffix)].lower()
        if key == self.owner.row_lost_key:
            self.owner.rows_present[key] = False
            return
        if key == self.owner.verify_fail_key:
            return
        self.owner.states[key] = self.owner.requirements[key]


class _FakeEnforcementScanner(GameSettingsEnforcementScanner):
    def __init__(
        self,
        initial: tuple[tuple[str, GameSettingState, GameSettingState], ...],
    ) -> None:
        self.states = {key: detected for key, detected, _required in initial}
        self.requirements = {key: required for key, _detected, required in initial}
        self.rows_present = {key: True for key, _detected, _required in initial}
        self.verify_fail_key: str | None = None
        self.row_lost_key: str | None = None
        self.cleanup_error: Exception | None = None
        self.device = _FakeDevice(self)
        self.scan_calls = 0
        self.traversal_calls = 0
        self.wait_calls = 0
        self.return_calls = 0

        entries: list[GameSettingCheckSpec] = []
        for index, (key, _detected, required) in enumerate(initial):
            definition = GameSettingDefinition(key=key, location="options")
            requirement = GameSettingRequirement(definition, required)

            def detector(_image: np.ndarray, *, _key=key):
                return self.states[_key]

            def observer(_image: np.ndarray, *, _key=key, _index=index):
                if not self.rows_present[_key]:
                    return None
                return self._observation(_key, _index)

            entries.append(
                GameSettingCheckSpec(
                    definition=definition,
                    detector=detector,
                    requirement=requirement,
                    observer=observer,
                )
            )
        self.check_registry = build_game_settings_registry(
            entries,
            require_enforce=True,
        )

    def _observation(self, key: str, index: int) -> GameSettingRowObservation:
        y = 170 + index * 90
        off_bounds = (500, y, 540, y + 20)
        on_bounds = (640, y, 675, y + 20)
        state = self.states[key]
        return GameSettingRowObservation(
            value=state,
            row_bounds=(250, y - 5, 700, y + 25),
            options=(
                GameSettingOptionObservation(
                    value=GameSettingState.OFF,
                    bounds=off_bounds,
                    click_bounds=(492, y - 8, 580, y + 28),
                    marker_activity=0.20 if state is GameSettingState.OFF else 0.05,
                ),
                GameSettingOptionObservation(
                    value=GameSettingState.ON,
                    bounds=on_bounds,
                    click_bounds=(632, y - 8, 715, y + 28),
                    marker_activity=0.20 if state is GameSettingState.ON else 0.05,
                ),
            ),
        )

    def scan_game_settings(self) -> GameSettingsScanResult:
        self.scan_calls += 1
        return GameSettingsScanResult(
            entry.make_result(self.states[entry.key])
            for entry in self.check_registry
        )

    def traverse_options(self, visitor):
        self.traversal_calls += 1
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
            reached_bottom=not stopped,
            stopped_early=stopped,
        )

    def _wait_options_stable(self) -> np.ndarray:
        self.wait_calls += 1
        return self.device.image

    def return_to_main(self) -> bool:
        self.return_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return True


class GameSettingsEnforcementTests(unittest.TestCase):
    def test_known_mismatch_changes_only_required_target_and_final_audit_passes(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                ("setting_a", GameSettingState.OFF, GameSettingState.ON),
                ("setting_b", GameSettingState.OFF, GameSettingState.OFF),
            )
        )
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertEqual(result.changed_keys, ("setting_a",))
        self.assertIs(scanner.states["setting_a"], GameSettingState.ON)
        self.assertEqual(len(scanner.device.clicks), 1)
        self.assertEqual(scanner.traversal_calls, 1)
        self.assertEqual(scanner.scan_calls, 2)
        self.assertEqual(scanner.return_calls, 1)

    def test_unknown_initial_audit_blocks_all_mutation(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.UNKNOWN, GameSettingState.ON),)
        )
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertTrue(result.blocked)
        self.assertEqual(scanner.device.clicks, [])
        self.assertEqual(scanner.traversal_calls, 0)
        self.assertEqual(scanner.scan_calls, 1)

    def test_multiple_changes_use_one_apply_traversal_in_registry_order(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                ("setting_a", GameSettingState.OFF, GameSettingState.ON),
                ("setting_b", GameSettingState.ON, GameSettingState.OFF),
                ("setting_c", GameSettingState.OFF, GameSettingState.ON),
            )
        )
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertEqual(result.changed_keys, ("setting_a", "setting_b", "setting_c"))
        self.assertEqual(scanner.traversal_calls, 1)
        names = tuple(item[0] for item in scanner.device.clicks)
        self.assertEqual(
            names,
            (
                "GAME_SETTINGS_SETTING_A_TARGET",
                "GAME_SETTINGS_SETTING_B_TARGET",
                "GAME_SETTINGS_SETTING_C_TARGET",
            ),
        )

    def test_click_geometry_is_row_local_and_never_uses_adjacent_row(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                ("setting_a", GameSettingState.OFF, GameSettingState.ON),
                ("setting_b", GameSettingState.OFF, GameSettingState.ON),
            )
        )
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        first_bounds = scanner.device.clicks[0][1]
        second_bounds = scanner.device.clicks[1][1]
        self.assertLess(first_bounds[3], second_bounds[1])
        self.assertGreater(second_bounds[1] - first_bounds[1], 50)

    def test_partial_verify_failure_stops_remaining_changes_without_rollback(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                ("setting_a", GameSettingState.OFF, GameSettingState.ON),
                ("setting_b", GameSettingState.OFF, GameSettingState.ON),
                ("setting_c", GameSettingState.OFF, GameSettingState.ON),
            )
        )
        scanner.verify_fail_key = "setting_b"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.changed_keys, ("setting_a",))
        self.assertEqual(result.failed_key, "setting_b")
        self.assertIs(scanner.states["setting_a"], GameSettingState.ON)
        self.assertIs(scanner.states["setting_b"], GameSettingState.OFF)
        self.assertIs(scanner.states["setting_c"], GameSettingState.OFF)
        self.assertEqual(len(scanner.device.clicks), 2)
        self.assertNotIn("GAME_SETTINGS_SETTING_C_TARGET", [name for name, _ in scanner.device.clicks])

    def test_row_loss_after_click_gets_one_bounded_retry_then_fails(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.row_lost_key = "setting_a"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("disappeared", result.failure_reason)
        self.assertEqual(scanner.wait_calls, 2)
        self.assertEqual(result.changed_keys, ())

    def test_second_enforce_after_success_is_zero_change_noop(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        first = scanner.enforce_required_game_settings()
        clicks_after_first = len(scanner.device.clicks)
        traversal_after_first = scanner.traversal_calls
        second = scanner.enforce_required_game_settings()
        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(second.changed_keys, ())
        self.assertEqual(len(scanner.device.clicks), clicks_after_first)
        self.assertEqual(scanner.traversal_calls, traversal_after_first)

    def test_cleanup_failure_after_successful_apply_is_operational_error(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.cleanup_error = RuntimeError("cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            scanner.enforce_required_game_settings()

    def test_cleanup_failure_does_not_mask_typed_apply_failure(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                ("setting_a", GameSettingState.OFF, GameSettingState.ON),
                ("setting_b", GameSettingState.OFF, GameSettingState.ON),
            )
        )
        scanner.verify_fail_key = "setting_b"
        scanner.cleanup_error = RuntimeError("cleanup failed")
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_b")
        self.assertEqual(result.changed_keys, ("setting_a",))


if __name__ == "__main__":
    unittest.main()
