import unittest

import yaml

from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
ACTIVE_TEMPLATES = (
    "config/deploy.template.yaml",
    "config/deploy.template-AidLux.yaml",
    "config/deploy.template-docker.yaml",
    "config/deploy.template-linux.yaml",
    "deploy/template",
    "deploy/Windows/template.yaml",
)
GIT_TEMPLATES = {
    "config/deploy.template.yaml",
    "config/deploy.template-AidLux.yaml",
    "config/deploy.template-docker.yaml",
    "config/deploy.template-linux.yaml",
    "deploy/template",
}
CANONICAL_GIT = {
    "Remote": "origin",
    "Branch": "personal/stable",
    "Repository": "git@github.com:AliceLiddell01/AzurPilot-private-Ru.git",
    "UpstreamRemote": "upstream",
    "UpstreamPushUrl": "DISABLED",
}


class DeployTemplateTests(unittest.TestCase):
    def test_templates_have_only_active_runtime_groups(self) -> None:
        for relative_path in ACTIVE_TEMPLATES:
            with self.subTest(relative_path=relative_path):
                data = yaml.safe_load(
                    (ROOT / relative_path).read_text(encoding="utf-8")
                )["Deploy"]
                self.assertNotIn("Update", data)
                self.assertIn("EnableReload", data["Webui"])
                expected = {"Python", "Adb", "Ocr", "Misc", "RemoteAccess", "Webui"}
                if relative_path in GIT_TEMPLATES:
                    expected.add("Git")
                    self.assertEqual(data["Git"], CANONICAL_GIT)
                else:
                    self.assertNotIn("Git", data)
                self.assertEqual(set(data), expected)

    def test_legacy_updater_keys_do_not_appear_in_active_templates(self) -> None:
        forbidden = (
            "GitExecutable:",
            "GitProxy:",
            "SSLVerify:",
            "GitOverCdn:",
            "CheckUpdateInterval:",
            "AutoRestartTime:",
            "git://git.pull/AzurPilot",
            "git." + "nanoda" + ".work",
        )
        for relative_path in ACTIVE_TEMPLATES:
            with self.subTest(relative_path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                tokens = forbidden
                if relative_path not in GIT_TEMPLATES:
                    tokens += ("Repository:", "Branch:")
                for token in tokens:
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
