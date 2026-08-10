from __future__ import annotations

import unittest

import numpy as np

from module.game_settings.enforcement import GameSettingsEnforcementScanner
from module.game_settings.model import (
    FrameRateValue,
    GameSettingChoiceRequirement,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingState,
    GameSettingValue,
    GameSettingsScanResult,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
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
        initial: tuple[tuple[str, GameSettingValue, GameSettingValue], ...],
    ) -> None:
        self.states = {key: detected for key, detected, _required in initial}
        self.requirements = {key: required for key, _detected, required in initial}
        self.rows_present = {key: True for key, _detected, _required in initial}
        self.verify_fail_key: str | None = None
        self.row_lost_key: str | None = None
        self.family_change_key: str | None = None
        self.escaped_target_key: str | None = None
        self.apply_missing_key: str | None = None
        self.drift_after_initial: dict[str, GameSettingValue] = {}
        self.unknown_verification_remaining: dict[str, int] = {}
        self.cleanup_error: Exception | None = None
        self.reached_bottom = True
        self.device = _FakeDevice(self)
        self.scan_calls = 0
        self.traversal_calls = 0
        self.wait_calls = 0
        self.return_calls = 0

        entries: list[GameSettingCheckSpec] = []
        for index, (key, _detected, required) in enumerate(initial):
            definition = GameSettingDefinition(key=key, location="options")
            if isinstance(required, GameSettingState):
                requirement = GameSettingRequirement(definition, required)
            else:
                requirement = GameSettingChoiceRequirement(definition, required)

            def detector(_image: np.ndarray, *, _key=key):
                return self.states[_key]

            def observer(_image: np.ndarray, *, _key=key, _index=index):
                if _key == self.apply_missing_key or not self.rows_present[_key]:
                    return None
                if _key == self.family_change_key:
                    return self._family_changed_observation(_index)
                remaining = self.unknown_verification_remaining.get(_key, 0)
                if remaining > 0 and self.wait_calls > 0:
                    self.unknown_verification_remaining[_key] = remaining - 1
                    return self._observation(
                        _key,
                        _index,
                        forced_value=type(self.states[_key]).UNKNOWN,
                    )
                return self._observation(_key, _index)

            entries.append(
                GameSettingCheckSpec(
                    definition=definition,
                    detector=detector,
                    requirement=requirement,
                    value_type=type(required),
                    observer=observer,
                )
            )
        self.check_registry = build_game_settings_registry(
            entries,
            require_enforce=True,
        )

    @staticmethod
    def _family_values(value: GameSettingValue) -> tuple[GameSettingValue, ...]:
        if isinstance(value, GameSettingState):
            return (GameSettingState.OFF, GameSettingState.ON)
        if isinstance(value, FrameRateValue):
            return (FrameRateValue.FPS_30, FrameRateValue.FPS_60)
        if isinstance(value, StoryAutoplayValue):
            return (StoryAutoplayValue.DISABLED, StoryAutoplayValue.ENABLED)
        if isinstance(value, TextAutoScrollSpeedValue):
            return (
                TextAutoScrollSpeedValue.SLOW,
                TextAutoScrollSpeedValue.NORMAL,
                TextAutoScrollSpeedValue.FAST,
                TextAutoScrollSpeedValue.VERY_FAST,
            )
        raise TypeError("Неподдерживаемая fake value family")

    def _observation(
        self,
        key: str,
        index: int,
        *,
        forced_value: GameSettingValue | None = None,
    ) -> GameSettingRowObservation:
        y = 170 + index * 90
        state = self.states[key] if forced_value is None else forced_value
        values = self._family_values(self.states[key])
        options: list[GameSettingOptionObservation] = []
        x = 430
        for option_index, value in enumerate(values):
            width = 56 if len(values) <= 2 else 68
            bounds = (x, y, x + width, y + 20)
            click_bounds = (x - 8, y - 8, x + width + 34, y + 28)
            if key == self.escaped_target_key and value is self.requirements[key]:
                click_bounds = (900, y - 8, 980, y + 28)
            options.append(
                GameSettingOptionObservation(
                    value=value,
                    bounds=bounds,
                    click_bounds=click_bounds,
                    marker_activity=0.20 if state is value else 0.05,
                )
            )
            x += width + 44

        return GameSettingRowObservation(
            value=state,
            row_bounds=(250, y - 5, 820, y + 25),
            options=tuple(options),
        )

    @staticmethod
    def _family_changed_observation(index: int) -> GameSettingRowObservation:
        y = 170 + index * 90
        return GameSettingRowObservation(
            value=FrameRateValue.FPS_30,
            row_bounds=(250, y - 5, 720, y + 25),
            options=(
                GameSettingOptionObservation(
                    value=FrameRateValue.FPS_30,
                    bounds=(500, y, 555, y + 20),
                    click_bounds=(492, y - 8, 590, y + 28),
                    marker_activity=0.20,
                ),
                GameSettingOptionObservation(
                    value=FrameRateValue.FPS_60,
                    bounds=(640, y, 695, y + 20),
                    click_bounds=(632, y - 8, 730, y + 28),
                    marker_activity=0.05,
                ),
            ),
        )

    def scan_game_settings(self) -> GameSettingsScanResult:
        self.scan_calls += 1
        result = GameSettingsScanResult(
            entry.make_result(self.states[entry.key])
            for entry in self.check_registry
        )
        if self.scan_calls == 1 and self.drift_after_initial:
            self.states.update(self.drift_after_initial)
        return result

    def traverse_options(self, visitor):
        self.traversal_calls += 1
        viewport = OptionsViewport(
            index=1,
            scroll_offset=0.0,
            is_top=True,
            is_bottom=self.reached_bottom,
        )
        stopped = bool(visitor(viewport))
        return OptionsTraversalResult(
            visited_viewports=1,
            final_offset=0.0,
            reached_bottom=self.reached_bottom,
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

    def test_choice_mismatch_clicks_exact_very_fast_target(self) -> None:
        scanner = _FakeEnforcementScanner(
            (
                (
                    "text_speed",
                    TextAutoScrollSpeedValue.NORMAL,
                    TextAutoScrollSpeedValue.VERY_FAST,
                ),
            )
        )
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertEqual(result.changed_keys, ("text_speed",))
        self.assertIs(
            scanner.states["text_speed"],
            TextAutoScrollSpeedValue.VERY_FAST,
        )
        self.assertEqual(len(scanner.device.clicks), 1)
        clicked_bounds = scanner.device.clicks[0][1]
        expected_target = scanner._observation("text_speed", 0).option_for(
            TextAutoScrollSpeedValue.VERY_FAST
        )
        self.assertIsNotNone(expected_target)
        self.assertEqual(clicked_bounds, expected_target.click_bounds)

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

    def test_value_family_change_before_click_fails_without_mutation(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.family_change_key = "setting_a"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("типизированная группа", result.failure_reason)
        self.assertEqual(scanner.device.clicks, [])

    def test_value_drift_after_initial_audit_fails_without_mutation(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.drift_after_initial = {"setting_a": GameSettingState.ON}
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("изменилось после начального аудита", result.failure_reason)
        self.assertEqual(scanner.device.clicks, [])

    def test_target_outside_observed_row_fails_without_click(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.escaped_target_key = "setting_a"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("вышла за границы", result.failure_reason)
        self.assertEqual(scanner.device.clicks, [])

    def test_required_row_missing_during_apply_fails_at_hard_bottom(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.apply_missing_key = "setting_a"
        scanner.reached_bottom = True
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("не найдена", result.failure_reason)
        self.assertEqual(scanner.device.clicks, [])

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
        self.assertNotIn(
            "GAME_SETTINGS_SETTING_C_TARGET",
            [name for name, _ in scanner.device.clicks],
        )

    def test_row_loss_after_click_gets_one_bounded_retry_then_fails(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.row_lost_key = "setting_a"
        result = scanner.enforce_required_game_settings()
        self.assertFalse(result.success)
        self.assertEqual(result.failed_key, "setting_a")
        self.assertIn("исчезла", result.failure_reason)
        self.assertEqual(scanner.wait_calls, 2)
        self.assertEqual(result.changed_keys, ())

    def test_transient_unknown_after_click_retries_once_then_succeeds(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.OFF, GameSettingState.ON),)
        )
        scanner.unknown_verification_remaining["setting_a"] = 1
        result = scanner.enforce_required_game_settings()
        self.assertTrue(result.success)
        self.assertEqual(result.changed_keys, ("setting_a",))
        self.assertEqual(scanner.wait_calls, 2)

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

    def test_noop_can_request_fresh_reaudit_for_smoke_idempotency_proof(self) -> None:
        scanner = _FakeEnforcementScanner(
            (("setting_a", GameSettingState.ON, GameSettingState.ON),)
        )
        result = scanner.enforce_required_game_settings(reaudit_on_noop=True)
        self.assertTrue(result.success)
        self.assertEqual(result.changed_keys, ())
        self.assertEqual(scanner.scan_calls, 2)
        self.assertEqual(scanner.traversal_calls, 0)
        self.assertEqual(scanner.device.clicks, [])

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
