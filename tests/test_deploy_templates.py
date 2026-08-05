
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_TEMPLATES = (
    "config/deploy.template.yaml",
    "config/deploy.template-AidLux.yaml",
    "config/deploy.template-docker.yaml",
    "config/deploy.template-linux.yaml",
    "deploy/template",
    "deploy/Windows/template.yaml",
)


class DeployTemplateTests(unittest.TestCase):
    def test_templates_have_only_active_runtime_groups(self) -> None:
        for relative_path in ACTIVE_TEMPLATES:
            with self.subTest(relative_path=relative_path):
                data = yaml.safe_load(
                    (ROOT / relative_path).read_text(encoding="utf-8")
                )["Deploy"]
                self.assertNotIn("Git", data)
                self.assertNotIn("Update", data)
                self.assertIn("EnableReload", data["Webui"])
                self.assertEqual(
                    set(data),
                    {"Python", "Adb", "Ocr", "Misc", "RemoteAccess", "Webui"},
                )

    def test_legacy_updater_keys_do_not_appear_in_active_templates(self) -> None:
        forbidden = (
            "Repository:",
            "Branch:",
            "GitExecutable:",
            "GitProxy:",
            "SSLVerify:",
            "GitOverCdn:",
            "CheckUpdateInterval:",
            "AutoRestartTime:",
            "git://git.pull/AzurPilot",
            "git.nanoda.work",
        )
        for relative_path in ACTIVE_TEMPLATES:
            with self.subTest(relative_path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                for token in forbidden:
                    self.assertNotIn(token, text)

    def test_platform_specific_values_are_preserved(self) -> None:
        linux = yaml.safe_load(
            (ROOT / "config/deploy.template-linux.yaml").read_text(encoding="utf-8")
        )["Deploy"]
        windows = yaml.safe_load(
            (ROOT / "config/deploy.template.yaml").read_text(encoding="utf-8")
        )["Deploy"]

        self.assertEqual(linux["Python"]["PythonExecutable"], "./.venv/bin/python")
        self.assertEqual(linux["Adb"]["AdbExecutable"], "./.venv/bin/adb")
        self.assertFalse(linux["Adb"]["ReplaceAdb"])
        self.assertEqual(linux["RemoteAccess"]["SSHExecutable"], "/usr/bin/ssh")
        self.assertEqual(windows["Python"]["PythonExecutable"], "./.venv/Scripts/python.exe")


if __name__ == "__main__":
    unittest.main()
