from __future__ import annotations

import ast
import unittest

from dev_tools.stage8a_control_flow_policy import (
    APPROVED_METADATA_EXPRESSION_POLICY,
    _find_fstring,
    _normalized_ast,
    _validate_metadata_expression_change,
)
from dev_tools.stage8a_device_log_audit import RuntimeScanner


def _fstring(source: str) -> ast.JoinedStr:
    tree = ast.parse(source)
    return next(node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr))


class Stage8AControlFlowPolicyTests(unittest.TestCase):
    def test_policy_contains_only_expected_eleven_points(self):
        identifiers = [
            identifier
            for values in APPROVED_METADATA_EXPRESSION_POLICY.values()
            for identifier in values
        ]
        self.assertEqual(len(identifiers), 11)
        self.assertEqual(len(set(identifiers)), 11)
        self.assertEqual(
            set(APPROVED_METADATA_EXPRESSION_POLICY),
            {
                "module/device/method/adb.py",
                "module/device/method/ascreencap.py",
                "module/device/method/droidcast.py",
            },
        )

    def test_exact_raw_to_len_raw_change_is_allowed(self):
        before = _fstring("logger.warning(f'bad={data}')")
        after = _fstring("logger.warning(f'bytes={len(data)}')")
        self.assertIsNone(_validate_metadata_expression_change(before, after))

    def test_raw_repr_to_len_raw_change_is_allowed(self):
        before = _fstring("raise RuntimeError(f'bad={data!r}')")
        after = _fstring("raise RuntimeError(f'bytes={len(data)}')")
        self.assertIsNone(_validate_metadata_expression_change(before, after))

    def test_unrelated_expression_change_is_rejected(self):
        before = _fstring("logger.warning(f'bad={data}')")
        after = _fstring("logger.warning(f'bad={len(other)}')")
        self.assertIsNotNone(_validate_metadata_expression_change(before, after))

    def test_multiple_formatted_values_are_rejected(self):
        before = _fstring("logger.warning(f'bad={data}')")
        after = _fstring("logger.warning(f'bad={len(data)} type={type(data)}')")
        self.assertIsNotNone(_validate_metadata_expression_change(before, after))

    def test_format_specifier_is_rejected(self):
        before = _fstring("logger.warning(f'bad={data}')")
        after = _fstring("logger.warning(f'bad={len(data):04d}')")
        self.assertIsNotNone(_validate_metadata_expression_change(before, after))

    def test_normalizer_accepts_only_message_and_metadata_expression_delta(self):
        path = "module/device/sample.py"
        before = (
            "def run(data):\n"
            "    logger.warning(f'[设备] bad={data}')\n"
        )
        after = (
            "def run(data):\n"
            "    logger.warning(f'[Устройство] получено={len(data)} байт')\n"
        )
        base_scanner = RuntimeScanner(path, before)
        head_scanner = RuntimeScanner(path, after)
        base_rows = base_scanner.scan()
        head_rows = head_scanner.scan()
        self.assertEqual(len(base_rows), 1)
        self.assertEqual(len(head_rows), 1)
        identifier = base_rows[0]["stable_identifier"]
        self.assertEqual(identifier, head_rows[0]["stable_identifier"])
        base_key = base_scanner.node_keys[identifier]
        head_key = head_scanner.node_keys[identifier]
        self.assertIsNotNone(_find_fstring(base_scanner.tree, base_key))
        self.assertIsNotNone(_find_fstring(head_scanner.tree, head_key))
        self.assertEqual(
            _normalized_ast(before, {base_key}, {base_key}),
            _normalized_ast(after, {head_key}, {head_key}),
        )

    def test_normalizer_does_not_hide_other_control_flow_change(self):
        path = "module/device/sample.py"
        before = (
            "def run(data):\n"
            "    logger.warning(f'[设备] bad={data}')\n"
        )
        after = (
            "def run(data):\n"
            "    logger.warning(f'[Устройство] получено={len(data)} байт')\n"
            "    return data\n"
        )
        base_scanner = RuntimeScanner(path, before)
        head_scanner = RuntimeScanner(path, after)
        base_rows = base_scanner.scan()
        head_rows = head_scanner.scan()
        identifier = base_rows[0]["stable_identifier"]
        self.assertEqual(identifier, head_rows[0]["stable_identifier"])
        base_key = base_scanner.node_keys[identifier]
        head_key = head_scanner.node_keys[identifier]
        self.assertNotEqual(
            _normalized_ast(before, {base_key}, {base_key}),
            _normalized_ast(after, {head_key}, {head_key}),
        )


if __name__ == "__main__":
    unittest.main()
