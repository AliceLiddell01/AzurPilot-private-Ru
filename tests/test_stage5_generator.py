from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from module.config.locale import LEGACY_UI_LOCALES, UI_LOCALE

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_TEMPLATES = (
    "deploy/template",
    "deploy/Windows/template.yaml",
    "config/deploy.template.yaml",
    "config/deploy.template-AidLux.yaml",
    "config/deploy.template-docker.yaml",
    "config/deploy.template-linux.yaml",
)
LEGACY_TEMPLATES = (
    "config/deploy.template-cn.yaml",
    "config/deploy.template-AidLux-cn.yaml",
    "config/deploy.template-docker-cn.yaml",
    "config/deploy.template-linux-cn.yaml",
)


class Stage5GeneratorTests(unittest.TestCase):
    def test_active_generation_has_one_locale_and_no_foreign_special_cases(self) -> None:
        source = (ROOT / "module/config/config_updater.py").read_text(encoding="utf-8")
        generate = source[source.index("    def generate(self):"):]
        start = source.index("    def generate_i18n(self):")
        end = source.index("    @cached_property\n    def menu", start)
        generate_i18n = source[start:end]
        self.assertIn("self.generate_i18n()", generate)
        self.assertNotIn("for lang", generate)
        self.assertIn("UI_LOCALE", generate_i18n)
        self.assertIn("EVENT_NAME_SOURCE", generate_i18n)
        self.assertNotIn("LANG_TO_SERVER", generate_i18n)
        self.assertNotIn("SERVER_TO_LANG", generate_i18n)
        self.assertNotIn("zh-TW", generate_i18n)

    def test_generator_does_not_regenerate_cn_ui_templates(self) -> None:
        full_source = (ROOT / "module/config/config_updater.py").read_text(encoding="utf-8")
        start = full_source.index("    def generate_deploy_template():")
        end = full_source.index("    def insert_package", start)
        source = full_source[start:end]
        self.assertNotIn("template-cn", source)
        self.assertNotIn("AidLux-cn", source)
        self.assertNotIn("docker-cn", source)
        self.assertNotIn("linux-cn", source)
        for relative in LEGACY_TEMPLATES:
            self.assertTrue((ROOT / relative).is_file())

    def test_active_templates_are_ru_ru_while_legacy_files_remain(self) -> None:
        for relative in ACTIVE_TEMPLATES:
            with self.subTest(relative=relative):
                data = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
                self.assertEqual(data["Deploy"]["Webui"]["Language"], UI_LOCALE)
        for relative in LEGACY_TEMPLATES:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_legacy_locale_files_remain_physical_but_inactive(self) -> None:
        for locale in LEGACY_UI_LOCALES:
            self.assertTrue((ROOT / f"module/config/i18n/{locale}.json").is_file())
        self.assertTrue((ROOT / f"module/config/i18n/{UI_LOCALE}.json").is_file())


if __name__ == "__main__":
    unittest.main()
