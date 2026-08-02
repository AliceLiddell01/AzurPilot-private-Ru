import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGGER_METHODS = {
    "debug",
    "info",
    "warning",
    "error",
    "critical",
    "exception",
    "exception_context",
    "hr",
}
EXPECTED_SEQUENCES = {
    "module/webui/app_lifecycle.py": [
        "exception_context", "info", "info", "exception_context",
        "exception_context", "error", "info",
    ],
    "module/webui/app_developer_tools.py": [
        "exception_context", "info", "warning", "exception_context", "exception_context",
    ],
    "module/webui/app_helpers.py": ["warning", "exception"],
    "module/webui/fastapi.py": ["exception", "debug", "debug", "debug", "debug"],
    "module/webui/patch.py": ["info"],
    "module/webui/process_manager.py": [
        "info", "info", "warning", "warning", "error", "info", "error",
        "warning", "info", "warning", "error", "error", "info", "error",
        "warning", "warning", "error", "error", "error", "error", "error",
        "error", "error", "error", "info", "error", "exception_context",
        "info", "info", "info", "info", "info", "info", "critical", "critical",
        "info", "info", "exception", "hr", "info", "info",
    ],
    "module/webui/remote_access.py": [
        "warning", "warning", "debug", "warning", "critical", "info", "info",
        "debug", "error", "error", "error", "info", "info", "info", "debug",
        "info", "info", "error", "info", "info", "info", "warning", "warning",
        "info", "info", "warning", "warning", "warning", "info", "warning",
        "warning", "exception", "info", "info", "info", "warning", "warning",
        "debug", "info", "exception",
    ],
}


def _logger_calls(relative_path):
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
            continue
        if node.func.attr not in LOGGER_METHODS:
            continue
        message = ast.get_source_segment(text, node.args[0]) if node.args else ""
        calls.append((node.lineno, node.func.attr, message or ""))
    return sorted(calls)


class ProcessLifecycleLogsTest(unittest.TestCase):
    def test_logger_severity_and_call_order_contract(self):
        for path, expected in EXPECTED_SEQUENCES.items():
            with self.subTest(path=path):
                actual = [method for _, method, _ in _logger_calls(path)]
                self.assertEqual(actual, expected)

    def test_first_party_lifecycle_messages_have_no_cjk_or_old_english_phrases(self):
        cjk = re.compile(r"[\u3400-\u9fff]")
        forbidden_english = re.compile(
            r"\b(?:Failed to load|exited\. Reason|Stop request|Reason: Finish)\b"
        )
        for path in EXPECTED_SEQUENCES:
            for line, method, message in _logger_calls(path):
                with self.subTest(path=path, line=line, method=method):
                    self.assertIsNone(cjk.search(message), message)
                    self.assertIsNone(forbidden_english.search(message), message)

    def test_process_state_keeps_legacy_markers_and_adds_russian_markers(self):
        source = (ROOT / "module/webui/process_manager.py").read_text(encoding="utf-8")
        for marker in (
            "Reason: Stop request",
            "Reason: Finish",
            "Причина: запрос остановки",
            "Причина: выполнение окончено",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
