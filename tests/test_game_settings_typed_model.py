from __future__ import annotations

import unittest

from module.game_settings.model import (
    FrameRateValue,
    GameSettingAppliedChange,
    GameSettingCheckResult,
    GameSettingChoiceCheckResult,
    GameSettingChoiceRequirement,
    GameSettingDefinition,
    GameSettingKind,
    GameSettingState,
    GameSettingsEnforcementResult,
    GameSettingsScanResult,
    StoryAutoplayValue,
    TextAutoScrollSpeedValue,
    is_unknown_game_setting_value,
)


def _definition(key: str) -> GameSettingDefinition:
    return GameSettingDefinition(key=key, location="options")


class GameSettingTypedValueTests(unittest.TestCase):
    def test_discrete_families_have_explicit_unknown_and_no_fake_toggle_values(self) -> None:
        self.assertEqual(
            tuple(FrameRateValue),
            (FrameRateValue.FPS_30, FrameRateValue.FPS_60, FrameRateValue.UNKNOWN),
        )
        self.assertEqual(
            tuple(StoryAutoplayValue),
            (
                StoryAutoplayValue.DISABLED,
                StoryAutoplayValue.ENABLED,
                StoryAutoplayValue.UNKNOWN,
            ),
        )
        self.assertEqual(
            tuple(TextAutoScrollSpeedValue),
            (
                TextAutoScrollSpeedValue.SLOW,
                TextAutoScrollSpeedValue.NORMAL,
                TextAutoScrollSpeedValue.FAST,
                TextAutoScrollSpeedValue.VERY_FAST,
                TextAutoScrollSpeedValue.UNKNOWN,
            ),
        )
        self.assertNotIn("on", {value.value for value in FrameRateValue})
        self.assertNotIn("on", {value.value for value in TextAutoScrollSpeedValue})

    def test_all_unknown_values_are_fail_closed_and_not_boolean(self) -> None:
        for value in (
            GameSettingState.UNKNOWN,
            FrameRateValue.UNKNOWN,
            StoryAutoplayValue.UNKNOWN,
            TextAutoScrollSpeedValue.UNKNOWN,
        ):
            with self.subTest(value=value):
                self.assertTrue(is_unknown_game_setting_value(value))
                with self.assertRaises(TypeError):
                    bool(value)

    def test_choice_requirement_rejects_unknown(self) -> None:
        definition = _definition("frame_rate")
        with self.assertRaises(ValueError):
            GameSettingChoiceRequirement(definition, FrameRateValue.UNKNOWN)

    def test_choice_result_rejects_cross_family_requirement(self) -> None:
        definition = _definition("frame_rate")
        requirement = GameSettingChoiceRequirement(
            definition,
            FrameRateValue.FPS_60,
        )
        with self.assertRaises(TypeError):
            GameSettingChoiceCheckResult(
                definition=definition,
                detected_value=TextAutoScrollSpeedValue.VERY_FAST,
                requirement=requirement,
            )

    def test_choice_compatibility_distinguishes_known_mismatch_and_unknown(self) -> None:
        definition = _definition("frame_rate")
        requirement = GameSettingChoiceRequirement(
            definition,
            FrameRateValue.FPS_60,
        )
        compatible = GameSettingChoiceCheckResult(
            definition,
            FrameRateValue.FPS_60,
            requirement,
        )
        mismatch = GameSettingChoiceCheckResult(
            definition,
            FrameRateValue.FPS_30,
            requirement,
        )
        unknown = GameSettingChoiceCheckResult(
            definition,
            FrameRateValue.UNKNOWN,
            requirement,
        )
        self.assertIs(compatible.compatible, True)
        self.assertIs(mismatch.compatible, False)
        self.assertIs(unknown.compatible, False)
        self.assertIs(compatible.kind, GameSettingKind.CHOICE)

    def test_heterogeneous_aggregate_preserves_order_and_unknown_semantics(self) -> None:
        frame_definition = _definition("frame_rate")
        speed_definition = _definition("text_auto_scroll_speed")
        frame = GameSettingChoiceCheckResult(
            frame_definition,
            FrameRateValue.FPS_60,
            GameSettingChoiceRequirement(frame_definition, FrameRateValue.FPS_60),
        )
        speed = GameSettingChoiceCheckResult(
            speed_definition,
            TextAutoScrollSpeedValue.UNKNOWN,
            GameSettingChoiceRequirement(
                speed_definition,
                TextAutoScrollSpeedValue.VERY_FAST,
            ),
        )
        result = GameSettingsScanResult((frame, speed))
        self.assertEqual(
            tuple(item.key for item in result),
            ("frame_rate", "text_auto_scroll_speed"),
        )
        self.assertEqual(result.unknown, (speed,))
        self.assertEqual(result.incompatible, (speed,))
        self.assertIs(result.all_required_compatible, False)

    def test_aggregate_rejects_duplicate_keys(self) -> None:
        definition = _definition("duplicate_guard")
        result = GameSettingCheckResult(
            definition=definition,
            detected_state=GameSettingState.OFF,
        )
        with self.assertRaisesRegex(ValueError, "Повторяющийся ключ"):
            GameSettingsScanResult((result, result))


class GameSettingsEnforcementResultTests(unittest.TestCase):
    def test_change_is_typed_and_preserves_verified_before_after(self) -> None:
        change = GameSettingAppliedChange(
            key="frame_rate",
            before=FrameRateValue.FPS_30,
            after=FrameRateValue.FPS_60,
        )
        self.assertTrue(change.verified)
        with self.assertRaises(TypeError):
            GameSettingAppliedChange(
                key="frame_rate",
                before=FrameRateValue.FPS_30,
                after=GameSettingState.ON,
            )
        with self.assertRaises(TypeError):
            GameSettingAppliedChange(
                key="frame_rate",
                before=1,
                after=1,
            )
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            GameSettingAppliedChange(
                key="frame_rate",
                before=FrameRateValue.FPS_30,
                after=FrameRateValue.UNKNOWN,
                verified=True,
            )

    def test_enforcement_result_exposes_changed_keys_and_blocked_reason(self) -> None:
        before = GameSettingsScanResult()
        change = GameSettingAppliedChange(
            key="frame_rate",
            before=FrameRateValue.FPS_30,
            after=FrameRateValue.FPS_60,
        )
        applied = GameSettingsEnforcementResult(
            before=before,
            changes=(change,),
            success=True,
        )
        blocked = GameSettingsEnforcementResult(
            before=before,
            success=False,
            blocked_reason="unknown setting",
        )
        self.assertEqual(applied.changed_keys, ("frame_rate",))
        self.assertFalse(applied.blocked)
        self.assertTrue(blocked.blocked)

    def test_enforcement_result_rejects_conflicting_status_fields(self) -> None:
        before = GameSettingsScanResult()
        with self.assertRaisesRegex(ValueError, "success result"):
            GameSettingsEnforcementResult(
                before=before,
                success=True,
                failure_reason="unexpected",
            )
        with self.assertRaisesRegex(ValueError, "взаимоисключающие"):
            GameSettingsEnforcementResult(
                before=before,
                success=False,
                blocked_reason="blocked",
                failure_reason="failed",
            )
        with self.assertRaisesRegex(ValueError, "взаимоисключающие"):
            GameSettingsEnforcementResult(
                before=before,
                success=False,
                blocked_reason="blocked",
                failed_key="frame_rate",
            )
        with self.assertRaisesRegex(ValueError, "должен содержать причину"):
            GameSettingsEnforcementResult(
                before=before,
                success=False,
            )


if __name__ == "__main__":
    unittest.main()
