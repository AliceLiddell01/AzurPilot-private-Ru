from __future__ import annotations

import unittest

from dev_tools.stage8a_device_log_audit import RuntimeScanner, normalized_ast
from dev_tools.stage8a_semantic_policy import classify_message, has_ordinary_english


class Stage8ADeviceLogAuditTests(unittest.TestCase):
    def test_cjk_sentence_requires_translation_even_with_adb(self):
        classification, owner, required, _ = classify_message(
            path="module/device/connection.py",
            function_owner="Connection.connect",
            call_kind="logger.error",
            arg_role="message",
            message="[设备] ADB 连接失败",
        )
        self.assertEqual(classification, "stage8a_first_party_message")
        self.assertEqual(owner, "stage8a")
        self.assertTrue(required)

    def test_ordinary_english_requires_translation(self):
        classification, _, required, _ = classify_message(
            path="module/device/connection.py",
            function_owner="Connection.connect",
            call_kind="logger.error",
            arg_role="message",
            message="ADB connection failed",
        )
        self.assertEqual(classification, "stage8a_first_party_message")
        self.assertTrue(required)

    def test_russian_with_technical_tokens_is_complete(self):
        message = "[Устройство — ADB] serial сохранён в Alas.Emulator.Serial"
        self.assertFalse(has_ordinary_english(message))
        _, _, required, _ = classify_message(
            path="module/device/connection.py",
            function_owner="Connection.connect",
            call_kind="logger.info",
            arg_role="message",
            message=message,
        )
        self.assertFalse(required)

    def test_scanner_records_logger_exception_and_raw_expression(self):
        source = """
def run(value):
    logger.info('[设备] 启动')
    logger.error(value)
    raise RuntimeError(f'Failed: {value}')
"""
        rows = RuntimeScanner("module/device/sample.py", source).scan()
        self.assertEqual(len(rows), 3)
        classifications = {row["classification"] for row in rows}
        self.assertIn("raw_external_payload", classifications)
        self.assertEqual(
            sum(row["translation_required"] for row in rows),
            2,
        )

    def test_normalized_ast_allows_only_message_literal_changes(self):
        before = "def run(x):\n    logger.info(f'[设备] 值: {x}')\n"
        after = "def run(x):\n    logger.info(f'[Устройство] Значение: {x}')\n"
        base_scanner = RuntimeScanner("module/device/sample.py", before)
        head_scanner = RuntimeScanner("module/device/sample.py", after)
        base_rows = base_scanner.scan()
        head_rows = head_scanner.scan()
        base_keys = {base_scanner.node_keys[base_rows[0]["stable_identifier"]]}
        head_keys = {head_scanner.node_keys[head_rows[0]["stable_identifier"]]}
        self.assertEqual(
            normalized_ast(before, base_keys),
            normalized_ast(after, head_keys),
        )

    def test_normalized_ast_rejects_control_flow_change(self):
        before = "def run(x):\n    logger.info('Start')\n    return x\n"
        after = "def run(x):\n    logger.info('Запуск')\n    if x:\n        return x\n"
        base_scanner = RuntimeScanner("module/device/sample.py", before)
        head_scanner = RuntimeScanner("module/device/sample.py", after)
        base_rows = base_scanner.scan()
        head_rows = head_scanner.scan()
        base_keys = {base_scanner.node_keys[base_rows[0]["stable_identifier"]]}
        head_keys = {head_scanner.node_keys[head_rows[0]["stable_identifier"]]}
        self.assertNotEqual(
            normalized_ast(before, base_keys),
            normalized_ast(after, head_keys),
        )


if __name__ == "__main__":
    unittest.main()
