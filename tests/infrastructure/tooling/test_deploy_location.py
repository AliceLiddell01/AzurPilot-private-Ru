import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import config as portable_config
from deploy.Windows import config as windows_config


TEMPLATE = """Deploy:
  Python:
    PythonExecutable: python
    PypiMirror: null
    InstallDependencies: true

  Webui:
    EnableReload: false
    WebuiHost: 0.0.0.0
    WebuiPort: 25548
    Language: en-US
    Run: null
"""

LEGACY_CONFIG = """# user comment must survive
Repository: git://git.pull/AzurPilot
Branch: master
GitExecutable: custom-git
GitProxy: null
SSLVerify: false
GitOverCdn: true
CheckUpdateInterval: 5
AutoRestartTime: 03:50
EnableReload: true
WebuiPort: 25548
UnknownCustomKey: preserve-me
"""

LEGACY_EXTERNAL_CONFIG = LEGACY_CONFIG.replace(
    "git://git.pull/AzurPilot",
    "https://legacy.example.invalid/git/AzurPilot",
)


class DeployConfigCompatibilityTests(unittest.TestCase):
    modules = (portable_config, windows_config)

    def make_paths(self, root: Path, content: str = LEGACY_CONFIG):
        template = root / "template.yaml"
        user = root / "deploy.yaml"
        template.write_text(TEMPLATE, encoding="utf-8")
        user.write_text(content, encoding="utf-8")
        return template, user

    def load(self, module, template: Path, user: Path):
        with patch(
            "requests.get",
            side_effect=AssertionError("config read attempted network access"),
        ):
            return module.DeployConfig(
                file=str(user),
                template_file=str(template),
            )

    def test_current_config_loads_without_updater_runtime(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                template, user = self.make_paths(
                    root,
                    "EnableReload: false\nWebuiPort: 25548\n",
                )
                before = user.read_bytes()
                config = self.load(module, template, user)

                self.assertEqual(user.read_bytes(), before)
                self.assertFalse(config.EnableReload)
                for key in (
                    "Repository",
                    "GitOverCdn",
                    "CheckUpdateInterval",
                    "AutoRestartTime",
                ):
                    self.assertFalse(hasattr(config, key))

    def test_legacy_enable_reload_is_supervisor_only(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                template, user = self.make_paths(root)
                config = self.load(module, template, user)

                self.assertTrue(config.EnableReload)
                self.assertEqual(
                    config.config["Repository"],
                    "git://git.pull/AzurPilot",
                )
                self.assertFalse(hasattr(config, "Repository"))
                self.assertFalse(hasattr(config, "GitOverCdn"))

    def test_old_repository_aliases_are_preserved_but_ignored(self):
        for content in (LEGACY_CONFIG, LEGACY_EXTERNAL_CONFIG):
            for module in self.modules:
                with self.subTest(module=module.__name__, content=content), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    template, user = self.make_paths(root, content)
                    before = user.read_bytes()
                    config = self.load(module, template, user)

                    self.assertEqual(user.read_bytes(), before)
                    self.assertIn("Repository", config.config)
                    self.assertFalse(hasattr(config, "Repository"))

    def test_unknown_keys_and_comments_survive_explicit_write(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                template, user = self.make_paths(root)
                config = self.load(module, template, user)

                config.config["WebuiPort"] = 26666
                config.write(keys={"WebuiPort"})
                text = user.read_text(encoding="utf-8")

                self.assertIn("# user comment must survive", text)
                self.assertIn("UnknownCustomKey: preserve-me", text)
                self.assertIn(
                    "Repository: git://git.pull/AzurPilot",
                    text,
                )
                self.assertIn("WebuiPort: 26666", text)

    def test_missing_config_read_does_not_create_file(self):
        for module in self.modules:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                template = root / "template.yaml"
                user = root / "missing.yaml"
                template.write_text(TEMPLATE, encoding="utf-8")

                config = self.load(module, template, user)

                self.assertFalse(user.exists())
                self.assertEqual(config.WebuiPort, 25548)


if __name__ == "__main__":
    unittest.main()
