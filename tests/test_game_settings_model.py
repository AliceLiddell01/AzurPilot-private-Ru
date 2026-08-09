from __future__ import annotations

import ast
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

from module.game_settings.model import (
    GameSettingCheckResult,
    GameSettingDefinition,
    GameSettingRequirement,
    GameSettingsScanResult,
    GameSettingState,
)


ROOT = Path(__file__).resolve().parents[1]


def _definition(key: str = "example_setting") -> GameSettingDefinition:
    return GameSettingDefinition(key=key, location="options")


def _required_result(
    key: str,
    detected_state: GameSettingState,
    expected_state: GameSettingState,
) -> GameSettingCheckResult:
    definition = _definition(key)
    return GameSettingCheckResult(
        definition=definition,
        detected_state=detected_state,
        requirement=GameSettingRequirement(definition, expected_state),
    )


class GameSettingStateTests(unittest.TestCase):
    def test_state_has_exact_tri_state_values(self) -> None:
        self.assertEqual(
            list(GameSettingState),
            [
                GameSettingState.ON,
                GameSettingState.OFF,
                GameSettingState.UNKNOWN,
            ],
        )
        self.assertEqual(
            [state.value for state in GameSettingState],
            ["on", "off", "unknown"],
        )

    def test_state_cannot_be_converted_to_bool_implicitly(self) -> None:
        for state in GameSettingState:
            with self.subTest(state=state), self.assertRaises(TypeError):
                bool(state)


class GameSettingDefinitionTests(unittest.TestCase):
    def test_definition_is_value_based_hashable_and_serialization_friendly(self) -> None:
        left = _definition()
        right = _definition()

        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))
        self.assertEqual(
            asdict(left),
            {"key": "example_setting", "location": "options"},
        )
        self.assertIn("example_setting", repr(left))

    def test_definition_rejects_invalid_identifiers(self) -> None:
        invalid_values = ("", "   ", "ExampleSetting", "example-setting", 1)

        for value in invalid_values:
            with self.subTest(key=value), self.assertRaises((TypeError, ValueError)):
                GameSettingDefinition(key=value, location="options")
            with self.subTest(location=value), self.assertRaises(
                (TypeError, ValueError)
            ):
                GameSettingDefinition(key="example_setting", location=value)

    def test_definition_is_immutable(self) -> None:
        definition = _definition()

        with self.assertRaises(FrozenInstanceError):
            definition.key = "other_setting"


class GameSettingRequirementTests(unittest.TestCase):
    def test_requirement_accepts_on_and_off(self) -> None:
        definition = _definition()

        on = GameSettingRequirement(definition, GameSettingState.ON)
        off = GameSettingRequirement(definition, GameSettingState.OFF)

        self.assertEqual(on, GameSettingRequirement(definition, GameSettingState.ON))
        self.assertNotEqual(on, off)
        self.assertEqual(len({on, off}), 2)

    def test_requirement_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            GameSettingRequirement(_definition(), GameSettingState.UNKNOWN)

    def test_requirement_rejects_untyped_values(self) -> None:
        with self.assertRaises(TypeError):
            GameSettingRequirement("example_setting", GameSettingState.ON)
        with self.assertRaises(TypeError):
            GameSettingRequirement(_definition(), "on")


class GameSettingCheckResultTests(unittest.TestCase):
    def test_compatibility_truth_table(self) -> None:
        cases = (
            (GameSettingState.ON, GameSettingState.ON, True),
            (GameSettingState.OFF, GameSettingState.OFF, True),
            (GameSettingState.ON, GameSettingState.OFF, False),
            (GameSettingState.OFF, GameSettingState.ON, False),
            (GameSettingState.UNKNOWN, GameSettingState.ON, False),
            (GameSettingState.UNKNOWN, GameSettingState.OFF, False),
        )

        for detected, expected, compatible in cases:
            with self.subTest(detected=detected, expected=expected):
                result = _required_result("example_setting", detected, expected)
                self.assertIs(result.compatible, compatible)

    def test_informational_result_has_no_compatibility(self) -> None:
        result = GameSettingCheckResult(_definition(), GameSettingState.UNKNOWN)

        self.assertFalse(result.is_required)
        self.assertIsNone(result.expected_state)
        self.assertIsNone(result.compatible)

    def test_result_rejects_requirement_for_other_definition(self) -> None:
        definition = _definition("first_setting")
        other = _definition("other_setting")

        with self.assertRaises(ValueError):
            GameSettingCheckResult(
                definition,
                GameSettingState.ON,
                GameSettingRequirement(other, GameSettingState.ON),
            )

    def test_result_rejects_untyped_state(self) -> None:
        with self.assertRaises(TypeError):
            GameSettingCheckResult(_definition(), True)


class GameSettingsScanResultTests(unittest.TestCase):
    def test_aggregate_preserves_order_and_lookup(self) -> None:
        first = _required_result(
            "first_setting", GameSettingState.OFF, GameSettingState.OFF
        )
        second = GameSettingCheckResult(
            _definition("second_setting"), GameSettingState.UNKNOWN
        )
        scan = GameSettingsScanResult([first, second])

        self.assertEqual(tuple(scan), (first, second))
        self.assertEqual(len(scan), 2)
        self.assertIs(scan.get("first_setting"), first)
        self.assertIs(scan.get("second_setting"), second)
        self.assertIsNone(scan.get("missing_setting"))

    def test_aggregate_rejects_duplicate_keys(self) -> None:
        first = GameSettingCheckResult(
            _definition("duplicate_setting"), GameSettingState.ON
        )
        duplicate = GameSettingCheckResult(
            GameSettingDefinition("duplicate_setting", "other_page"),
            GameSettingState.OFF,
        )

        with self.assertRaisesRegex(ValueError, "duplicate_setting"):
            GameSettingsScanResult((first, duplicate))

    def test_aggregate_queries_and_required_compatibility(self) -> None:
        compatible = _required_result(
            "compatible_setting", GameSettingState.OFF, GameSettingState.OFF
        )
        unknown_required = _required_result(
            "unknown_setting", GameSettingState.UNKNOWN, GameSettingState.ON
        )
        informational_unknown = GameSettingCheckResult(
            _definition("informational_setting"), GameSettingState.UNKNOWN
        )
        scan = GameSettingsScanResult(
            (compatible, unknown_required, informational_unknown)
        )

        self.assertEqual(scan.required, (compatible, unknown_required))
        self.assertEqual(scan.unknown, (unknown_required, informational_unknown))
        self.assertEqual(scan.incompatible, (unknown_required,))
        self.assertFalse(scan.all_required_compatible)

    def test_aggregate_without_requirements_has_no_compatibility_result(self) -> None:
        scan = GameSettingsScanResult(
            (GameSettingCheckResult(_definition(), GameSettingState.ON),)
        )

        self.assertEqual(scan.required, ())
        self.assertEqual(scan.incompatible, ())
        self.assertIsNone(scan.all_required_compatible)

    def test_aggregate_is_immutable_and_hashable(self) -> None:
        result = GameSettingCheckResult(_definition(), GameSettingState.ON)
        scan = GameSettingsScanResult([result])

        self.assertEqual(hash(scan), hash(GameSettingsScanResult((result,))))
        with self.assertRaises(FrozenInstanceError):
            scan.results = ()

    def test_aggregate_rejects_untyped_items(self) -> None:
        with self.assertRaises(TypeError):
            GameSettingsScanResult(["example_setting"])


class GameSettingsModelBoundaryTests(unittest.TestCase):
    def test_model_has_only_standard_library_dependencies(self) -> None:
        model_path = ROOT / "module" / "game_settings" / "model.py"
        tree = ast.parse(model_path.read_text(encoding="utf-8"))
        imported_modules = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)

        self.assertEqual(
            sorted(imported_modules),
            ["__future__", "collections.abc", "dataclasses", "enum", "re"],
        )


if __name__ == "__main__":
    unittest.main()
