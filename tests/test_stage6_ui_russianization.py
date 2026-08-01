from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dev_tools.stage6_ui_audit import (
    EXCEPTIONS_PATH,
    METRICS_PATH,
    REPORT_PATH,
    Stage6Audit,
    exception_category,
    format_signature,
    javascript_ui_candidates,
    python_translation_key_usage,
)
from module.webui.event_calculator import build_event_calculator_js


ROOT = Path(__file__).resolve().parents[1]


class Stage6UiRussianizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = Stage6Audit(ROOT)
        cls.outputs, cls.details = cls.audit.build()
        cls.metrics = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
        cls.exceptions = json.loads(EXCEPTIONS_PATH.read_text(encoding="utf-8"))["entries"]

    def test_definition_of_done_metrics_are_zero(self) -> None:
        self.assertEqual(self.metrics["active_runtime_locales"], ["ru-RU"])
        self.assertFalse(self.metrics["foreign_runtime_fallback"])
        self.assertFalse(self.metrics["ui_locale_linked_to_game_server"])
        for key in (
            "missing_translation_keys",
            "extra_translation_keys",
            "empty_replacements",
            "unresolved_active_ui",
            "unreviewed_English_active_ui",
            "unreviewed_CJK_active_ui",
            "placeholder_mismatches",
            "raw_translation_keys_rendered",
            "Gui.Missing_rendered",
        ):
            with self.subTest(metric=key):
                self.assertEqual(self.metrics[key], 0)

    def test_recomputed_outputs_match_committed_results(self) -> None:
        for name, expected in self.outputs.items():
            with self.subTest(result=name):
                self.assertEqual((EXCEPTIONS_PATH.parent / name).read_bytes(), expected)
        self.assertEqual(self.audit.check(), [])

    def test_check_is_read_only(self) -> None:
        paths = (EXCEPTIONS_PATH, METRICS_PATH, REPORT_PATH)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(self.audit.check(), [])
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(before, after)

    def test_exceptions_are_point_specific_and_complete(self) -> None:
        required = {
            "path", "key_or_line", "text", "category", "reason",
            "runtime_context", "stage", "evidence",
        }
        allowed_categories = {
            "technical_value", "proper_name", "original_metadata", "external_content"
        }
        identities = set()
        for entry in self.exceptions:
            with self.subTest(path=entry.get("path"), key=entry.get("key_or_line")):
                self.assertEqual(set(entry), required)
                self.assertEqual(entry["stage"], 6)
                self.assertIn(entry["category"], allowed_categories)
                self.assertNotIn("*", entry["path"])
                self.assertNotIn("*", str(entry["key_or_line"]))
                self.assertFalse(str(entry["path"]).endswith("/"))
                self.assertTrue(entry["reason"].strip())
                self.assertTrue(entry["runtime_context"].strip())
                self.assertTrue(entry["evidence"].strip())
                identity = (entry["path"], str(entry["key_or_line"]), entry["text"])
                self.assertNotIn(identity, identities)
                identities.add(identity)

    def test_ordinary_english_is_not_auto_excepted(self) -> None:
        self.assertIsNone(exception_category("Gui.Toast.ConfigSaved", "Settings saved"))
        self.assertIsNone(exception_category("Task.Example.help", "Select the server"))

    def test_direct_python_and_html_ui_has_no_foreign_literal(self) -> None:
        self.assertEqual(self.audit.direct_candidates(), [])

    def test_placeholder_and_markup_signature_covers_stage6_formats(self) -> None:
        sample = "{name} %(count)03d %s <b>x</b> [red]y[/red] ${value}\n"
        signature = format_signature(sample)
        self.assertEqual(signature["placeholders"], ["%(count)03d", "%s", "{name}", "{value}"])
        self.assertEqual(signature["html"], ["start:b", "end:b"])
        self.assertEqual(signature["rich"], ["red", "/red"])
        self.assertEqual(signature["js_interpolation"], ["${value}"])
        self.assertEqual(signature["control_characters"], ["\\n"])
        self.assertEqual(signature["literal_escapes"], [])
        self.assertNotEqual(format_signature("<b>x</b>"), format_signature("<b>x<b/>"))

    def test_javascript_and_runtime_key_regressions_are_detected(self) -> None:
        candidates = javascript_ui_candidates(
            "statusEl.textContent = err.message || 'unknown error';",
            "module/webui/fixture.py",
        )
        self.assertEqual([candidate.text for candidate in candidates], ["unknown error"])

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            dir=ROOT,
            encoding="utf-8",
            delete=False,
        ) as fixture:
            fixture.write(
                'from module.webui.lang import t\n'
                'put_text(t("Gui.Text.Save"))\n'
                'put_text("Gui.Missing")\n'
            )
            fixture_path = Path(fixture.name)
        try:
            translated, raw = python_translation_key_usage(fixture_path)
        finally:
            fixture_path.unlink(missing_ok=True)
        self.assertEqual([item["key"] for item in translated], ["Gui.Text.Save"])
        self.assertEqual([item["key"] for item in raw], ["Gui.Missing"])

    def test_external_wiki_names_are_escaped_before_table_rendering(self) -> None:
        data = {
            "event_name": "<img src=x onerror=alert(1)>",
            "shop_items": [{"name": "<script>alert(1)</script>", "quantity": 1}],
            "daily": [],
            "extra": [],
            "stages": [],
        }
        script = build_event_calculator_js("fixture", data, {})
        self.assertIn("function escapeHtml(value)", script)
        self.assertEqual(script.count("${escapeHtml(item.name)}"), 3)
        self.assertIn("[data-role=\"event-name\"]').textContent", script)
        self.assertNotIn("<td>${item.name", script)

    def test_server_package_and_event_sources_remain_independent(self) -> None:
        updater = (ROOT / "module/config/config_updater.py").read_text(encoding="utf-8")
        locale = (ROOT / "module/config/locale.py").read_text(encoding="utf-8")
        self.assertIn("EVENT_NAME_SOURCE", updater)
        self.assertIn("VALID_PACKAGE", updater)
        self.assertIn("VALID_SERVER_LIST", updater)
        self.assertNotIn("UI_LOCALE = to_server", locale)
        self.assertNotIn("to_server(UI_LOCALE", updater)


if __name__ == "__main__":
    unittest.main()
