from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from module.config.locale import UI_LOCALE

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_TEMPLATES = (
    "deploy/template",
    "deploy/Windows/template.yaml",
    "config/deploy.template.yaml",
    "config/deploy.template-AidLux.yaml",
    "config/deploy.template-docker.yaml",
    "config/deploy.template-linux.yaml",
)


class ConfigGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.argument_source = yaml.safe_load(
            (ROOT / "module/config/argument/argument.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.generated_args = json.loads(
            (ROOT / "module/config/argument/args.json").read_text(
                encoding="utf-8"
            )
        )

    def test_generation_uses_one_ui_locale_and_explicit_event_source(self) -> None:
        source = (ROOT / "module/config/config_updater.py").read_text(encoding="utf-8")
        generate = source[source.index("    def generate(self):") :]
        start = source.index("    def generate_i18n(self):")
        end = source.index("    @cached_property\n    def menu", start)
        generate_i18n = source[start:end]

        self.assertIn("self.generate_i18n()", generate)
        self.assertNotIn("for lang", generate)
        self.assertIn("UI_LOCALE", generate_i18n)
        self.assertIn("EVENT_NAME_SOURCE", generate_i18n)
        self.assertNotIn("LANG_TO_SERVER", generate_i18n)
        self.assertNotIn("SERVER_TO_LANG", generate_i18n)

    def test_generator_does_not_regenerate_inactive_ui_templates(self) -> None:
        source = (ROOT / "module/config/config_updater.py").read_text(encoding="utf-8")
        start = source.index("    def generate_deploy_template():")
        end = source.index("    def insert_package", start)
        generator = source[start:end]

        self.assertNotIn("template-cn", generator)
        self.assertNotIn("AidLux-cn", generator)
        self.assertNotIn("docker-cn", generator)
        self.assertNotIn("linux-cn", generator)

    def test_active_templates_use_runtime_locale(self) -> None:
        for relative in ACTIVE_TEMPLATES:
            with self.subTest(relative=relative):
                data = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
                self.assertEqual(data["Deploy"]["Webui"]["Language"], UI_LOCALE)

    def test_smoke_override_capability_is_carried_into_generated_args(self) -> None:
        self.assertTrue(
            self.argument_source["Reward"]["CollectMission"]["smoke_override"]
        )
        self.assertTrue(
            self.generated_args["Reward"]["Reward"]["CollectMission"][
                "smoke_override"
            ]
        )

    def test_llm_api_base_remains_editable_in_generated_metadata(self) -> None:
        self.assertNotIn("sensitive", self.argument_source["Error"]["LlmApiBase"])
        self.assertNotIn(
            "sensitive",
            self.generated_args["Alas"]["Error"]["LlmApiBase"],
        )
        for name, source_data, generated_data in (
            (
                "Error.OnePushConfig",
                self.argument_source["Error"]["OnePushConfig"],
                self.generated_args["Alas"]["Error"]["OnePushConfig"],
            ),
            (
                "Error.LlmApiKey",
                self.argument_source["Error"]["LlmApiKey"],
                self.generated_args["Alas"]["Error"]["LlmApiKey"],
            ),
            (
                "OpsiGeneral.OpsiOnePushConfig",
                self.argument_source["OpsiGeneral"]["OpsiOnePushConfig"],
                self.generated_args["OpsiGeneral"]["OpsiGeneral"][
                    "OpsiOnePushConfig"
                ],
            ),
        ):
            with self.subTest(name=name):
                self.assertIs(source_data.get("sensitive"), True)
                self.assertIs(generated_data.get("sensitive"), True)


if __name__ == "__main__":
    unittest.main()
