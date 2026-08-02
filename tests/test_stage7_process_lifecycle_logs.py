import ast
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from dev_tools.stage7_gui_contract import GUI_BLOCKING_METRICS, build_gui_contract


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


    def test_gui_supervisor_translation_only_contract(self):
        base_ref = os.environ.get("STAGE7_BASE_REF", "origin/personal/stable")
        base_sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", base_ref],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        outputs, metrics, errors = build_gui_contract(ROOT, base_sha)
        self.assertEqual(errors, [])
        for key in GUI_BLOCKING_METRICS:
            with self.subTest(metric=key):
                self.assertEqual(metrics[key], 0)

        inventory = json.loads(outputs["gui-inventory.json"])["entries"]
        cjk = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
        plain_english = re.compile(r"[A-Za-z]{2,}")
        for entry in inventory:
            template = entry["template"]
            with self.subTest(identifier=entry["semantic_identifier"]):
                if entry["classification"] == "stage7_first_party_message":
                    self.assertIsNone(cjk.search(template), template)
                    if not re.search(r"[А-Яа-яЁё]", template):
                        self.assertIsNone(plain_english.search(template), template)

    def test_gui_contract_keeps_exact_technical_tokens(self):
        base_ref = os.environ.get("STAGE7_BASE_REF", "origin/personal/stable")
        base_sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", base_ref],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        outputs, _, _ = build_gui_contract(ROOT, base_sha)
        inventory = json.loads(outputs["gui-inventory.json"])["entries"]
        technical = {
            entry["template"]
            for entry in inventory
            if entry["classification"] == "technical_identifier"
        }
        self.assertEqual(technical, {"SSL", "Electron"})

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
