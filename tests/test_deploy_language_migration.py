from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from deploy.config import DeployConfig
from deploy.language_migration import (
    DeployLanguageMigrationError,
    migrate_deploy_language,
)


TEMPLATE = """Deploy:\n  Webui:\n    Language: ru-RU\n    Theme: default\n  Ocr:\n    UseOcrServer: false\n"""


def deploy_text(value: str | None = "en-US", *, newline: str = "\n", final_newline: bool = True) -> str:
    language = "" if value is None else value
    text = (
        "# пользовательский комментарий\n"
        "Deploy:\n"
        "  Webui:\n"
        f"    Language: {language}  # оставить комментарий\n"
        "    Theme: dark\n"
        "  Emulator:\n"
        "    PackageName: com.YoStarEN.AzurLane\n"
        "    ServerName: en-0\n"
        "  Ocr:\n"
        "    UseOcrServer: true\n"
        "    OcrModelVersionEnglish: azur_lane_v6_6\n"
        "  UnknownNested:\n"
        "    TokenLikeValue: keep-me\n"
    ).replace("\n", newline)
    if not final_newline:
        text = text.rstrip("\r\n")
    return text


class DeployLanguageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "deploy.yaml"
        self.template = self.root / "template.yaml"
        self.template.write_text(TEMPLATE, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_legacy_unknown_empty_and_null_values_migrate(self) -> None:
        values = ("en-US", "ja-JP", "zh-CN", "zh-MIAO", "zh-TW", "unknown-value", "", None, "null")
        for value in values:
            with self.subTest(value=value):
                self.file.write_text(deploy_text(value), encoding="utf-8")
                before = self.file.read_text(encoding="utf-8")
                result = migrate_deploy_language(str(self.file))
                after = self.file.read_text(encoding="utf-8")
                self.assertTrue(result.changed)
                self.assertIn("ru-RU", after)
                self.assertIn("# оставить комментарий", after)
                before_yaml = yaml.safe_load(before)
                after_yaml = yaml.safe_load(after)
                self.assertEqual(after_yaml["Deploy"]["Webui"]["Language"], "ru-RU")
                before_yaml["Deploy"]["Webui"]["Language"] = "ru-RU"
                self.assertEqual(after_yaml, before_yaml)

    def test_missing_language_adds_only_one_scalar(self) -> None:
        original = "# keep\nDeploy:\n  Webui:\n    Theme: dark\nUnknownCustomKey: preserve-me"
        self.file.write_text(original, encoding="utf-8")
        result = migrate_deploy_language(str(self.file))
        self.assertTrue(result.changed)
        self.assertEqual(
            self.file.read_text(encoding="utf-8"),
            original + "\nLanguage: ru-RU",
        )

    def test_block_scalar_text_is_not_treated_as_language_key(self) -> None:
        original = (
            "Deploy:\n"
            "  Webui:\n"
            "    Theme: dark\n"
            "UnknownNotes: |\n"
            "  Language: en-US\n"
            "  Keep this text byte-for-byte.\n"
        )
        self.file.write_text(original, encoding="utf-8")
        result = migrate_deploy_language(str(self.file))
        after = self.file.read_text(encoding="utf-8")
        self.assertTrue(result.changed)
        self.assertIn("UnknownNotes: |\n  Language: en-US\n", after)
        self.assertEqual(after, original + "Language: ru-RU\n")

    def test_quoted_key_is_patched_without_touching_block_scalar_text(self) -> None:
        original = (
            "Deploy:\n"
            "  Webui:\n"
            '    "Language": "en-US"  # keep\n'
            "UnknownNotes: |\n"
            "  Language: untouched\n"
        )
        self.file.write_text(original, encoding="utf-8")
        result = migrate_deploy_language(str(self.file))
        after = self.file.read_text(encoding="utf-8")
        self.assertTrue(result.changed)
        self.assertEqual(
            after,
            original.replace('"en-US"', "ru-RU", 1),
        )
        self.assertIn("  Language: untouched\n", after)

    def test_ru_ru_is_byte_for_byte_no_op(self) -> None:
        original = deploy_text("ru-RU", newline="\r\n", final_newline=False).encode("utf-8")
        self.file.write_bytes(original)
        result = migrate_deploy_language(str(self.file))
        self.assertFalse(result.changed)
        self.assertEqual(self.file.read_bytes(), original)

    def test_comments_unknown_keys_crlf_and_no_final_newline_are_preserved(self) -> None:
        original = deploy_text("en-US", newline="\r\n", final_newline=False).encode("utf-8")
        self.file.write_bytes(original)
        result = migrate_deploy_language(str(self.file))
        self.assertTrue(result.changed)
        after = self.file.read_bytes()
        self.assertIn(b"\r\n", after)
        self.assertFalse(after.endswith(b"\n"))
        self.assertEqual(
            after,
            original.replace(b"Language: en-US", b"Language: ru-RU"),
        )

    def test_duplicate_and_damaged_yaml_remain_unchanged(self) -> None:
        fixtures = (
            "Deploy:\n  Webui:\n    Language: en-US\n    Language: zh-CN\n",
            "Deploy:\n  Webui: [\n    Language: en-US\n",
            "Deploy:\n  Webui:\n    Language:\n      nested: invalid\n",
            "Deploy:\n  Webui:\n    Language: |\n      en-US\n",
        )
        for content in fixtures:
            with self.subTest(content=content):
                self.file.write_text(content, encoding="utf-8")
                before = self.file.read_bytes()
                with self.assertRaises(DeployLanguageMigrationError):
                    migrate_deploy_language(str(self.file))
                self.assertEqual(self.file.read_bytes(), before)
                self.assertEqual(list(self.root.glob("deploy.yaml.*.tmp")), [])

    def test_atomic_write_failure_leaves_original_and_no_temp_file(self) -> None:
        original = deploy_text("en-US").encode("utf-8")
        self.file.write_bytes(original)
        with patch("deploy.language_migration.replace_tmp", side_effect=PermissionError("locked")):
            with self.assertRaises(DeployLanguageMigrationError):
                migrate_deploy_language(str(self.file))
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(list(self.root.glob("deploy.yaml.*.tmp")), [])

    def test_missing_file_and_import_are_side_effect_free(self) -> None:
        self.assertFalse(self.file.exists())
        import deploy.language_migration as migration
        importlib.reload(migration)
        self.assertFalse(self.file.exists())
        result = migration.migrate_deploy_language(str(self.file))
        self.assertFalse(result.changed)
        self.assertFalse(self.file.exists())

    def test_deploy_config_read_and_constructor_do_not_write(self) -> None:
        self.file.write_text(deploy_text("en-US"), encoding="utf-8")
        before = self.file.read_bytes()
        config = DeployConfig(file=str(self.file), template_file=str(self.template))
        self.assertEqual(self.file.read_bytes(), before)
        config.read()
        self.assertEqual(self.file.read_bytes(), before)

    def test_cached_state_migrates_before_deploy_config_constructor(self) -> None:
        from module.webui.setting import State

        class FreshState(State):
            pass

        events: list[str] = []
        expected_config = object()

        def migrate():
            events.append("migration")
            return SimpleNamespace(changed=False)

        def construct():
            events.append("constructor")
            return expected_config

        with patch(
            "deploy.language_migration.migrate_deploy_language",
            side_effect=migrate,
        ), patch(
            "module.webui.config.DeployConfig",
            side_effect=construct,
        ):
            self.assertIs(FreshState.deploy_config, expected_config)
            self.assertIs(FreshState.deploy_config, expected_config)

        self.assertEqual(events, ["migration", "constructor"])

    def test_parsed_runtime_values_except_language_are_unchanged(self) -> None:
        self.file.write_text(deploy_text("ja-JP"), encoding="utf-8")
        before = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        migrate_deploy_language(str(self.file))
        after = yaml.safe_load(self.file.read_text(encoding="utf-8"))
        self.assertEqual(after["Deploy"]["Emulator"], before["Deploy"]["Emulator"])
        self.assertEqual(after["Deploy"]["Ocr"], before["Deploy"]["Ocr"])
        self.assertEqual(after["Deploy"]["UnknownNested"], before["Deploy"]["UnknownNested"])
        self.assertEqual(after["Deploy"]["Webui"]["Theme"], "dark")
        self.assertEqual(after["Deploy"]["Webui"]["Language"], "ru-RU")

    def test_repeated_migration_is_idempotent(self) -> None:
        self.file.write_text(deploy_text("en-US"), encoding="utf-8")
        first = migrate_deploy_language(str(self.file))
        after_first = self.file.read_bytes()
        second = migrate_deploy_language(str(self.file))
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(self.file.read_bytes(), after_first)


if __name__ == "__main__":
    unittest.main()
