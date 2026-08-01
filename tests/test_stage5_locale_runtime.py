from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from module.config.deep import deep_iter
from module.config.locale import LEGACY_UI_LOCALES, UI_LOCALE
from module.webui import lang
from module.webui import deploy_settings

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(
    r"\{[^{}]*\}"
    r"|%\([^)]+\)[#0 +\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
    r"|%[#0+\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs%]"
)


def flatten_strings(data):
    return {".".join(path): value for path, value in deep_iter(data, depth=3) if isinstance(value, str)}


class Stage5LocaleRuntimeTests(unittest.TestCase):
    def test_single_explicit_runtime_locale(self) -> None:
        self.assertEqual(UI_LOCALE, "ru-RU")
        self.assertEqual(lang.LANG, UI_LOCALE)
        self.assertNotIn(UI_LOCALE, LEGACY_UI_LOCALES)
        self.assertEqual(set(LEGACY_UI_LOCALES), {"en-US", "ja-JP", "zh-CN", "zh-MIAO", "zh-TW"})

    def test_loader_reads_only_ru_ru_and_uses_flat_dictionary(self) -> None:
        calls = []

        def fake_read(path):
            calls.append(str(path).replace("\\", "/"))
            return {"Gui": {"Menu": {"Title": "Русский интерфейс"}}}

        with patch.object(lang, "list_mod_dir", return_value=[]), patch.object(lang, "read_file", side_effect=fake_read):
            lang.reload()

        self.assertEqual(lang.dic_lang, {"Gui.Menu.Title": "Русский интерфейс"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("module/config/i18n/ru-RU.json"))
        for legacy in LEGACY_UI_LOCALES:
            self.assertTrue(all(legacy not in path for path in calls))

    def test_browser_or_unknown_locale_cannot_switch_runtime(self) -> None:
        for value in (*LEGACY_UI_LOCALES, "de-DE", "unknown"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    lang.set_language(value)
        lang.set_language("RU-ru")
        self.assertEqual(lang.LANG, UI_LOCALE)
        with self.assertRaises(ValueError):
            lang.set_language(UI_LOCALE, refresh=True)

    def test_missing_key_never_uses_foreign_fallback(self) -> None:
        lang.dic_lang.clear()
        self.assertEqual(lang._t("Gui.Missing"), "Gui.Missing")
        for legacy in LEGACY_UI_LOCALES:
            with self.assertRaises(ValueError):
                lang._t("Gui.Missing", legacy)

    def test_language_selector_is_absent_from_ui_surfaces(self) -> None:
        deploy_keys = {field.key for _, fields in deploy_settings.DEPLOY_GROUPS for field in fields}
        self.assertNotIn("Language", deploy_keys)
        home = (ROOT / "module/webui/app_home.py").read_text(encoding="utf-8")
        oobe = (ROOT / "module/webui/oobe.py").read_text(encoding="utf-8")
        self.assertNotIn("Select your language", home)
        self.assertNotIn("简体中文", home)
        self.assertNotIn("_on_language_selected", oobe)
        self.assertNotIn('"welcome"', oobe.split("STEPS =", 1)[1].split("\n", 1)[0])

    def test_ru_catalog_is_complete_and_preserves_placeholders(self) -> None:
        ru_path = ROOT / "module/config/i18n/ru-RU.json"
        canonical_path = ROOT / "module/config/i18n/en-US.json"
        self.assertTrue(ru_path.is_file())
        ru_raw = ru_path.read_text(encoding="utf-8")
        ru = json.loads(ru_raw)
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        ru_flat = flatten_strings(ru)
        canonical_flat = flatten_strings(canonical)
        self.assertEqual(set(ru_flat), set(canonical_flat))
        self.assertEqual(len(ru_flat), len(canonical_flat))
        for key in sorted(ru_flat):
            with self.subTest(key=key):
                self.assertEqual(
                    sorted(PLACEHOLDER_RE.findall(ru_flat[key])),
                    sorted(PLACEHOLDER_RE.findall(canonical_flat[key])),
                )
        self.assertEqual(ru_raw, json.dumps(ru, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
