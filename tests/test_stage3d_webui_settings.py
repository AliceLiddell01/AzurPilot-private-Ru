import tempfile
import unittest
from pathlib import Path

from deploy.config import DeployConfig
from module.webui.setting import State
from module.webui import deploy_settings

TEMPLATE = '''Deploy:\n  Python:\n    PythonExecutable: python\n    PypiMirror: null\n    InstallDependencies: true\n  Webui:\n    EnableReload: false\n    WebuiHost: 0.0.0.0\n    WebuiPort: 25548\n    Language: en-US\n    Theme: default\n    DpiScaling: true\n    Password: null\n    CDN: false\n    WebuiSSLKey: null\n    WebuiSSLCert: null\n    Run: null\n'''
USER = '''# keep me\nRepository: git://git.pull/AzurPilot\nUnknownCustomKey: preserve-me\nEnableReload: false\nWebuiPort: 25548\nRun: null\n'''

class WebUiDeploySettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.template = root / 'template.yaml'
        self.user = root / 'deploy.yaml'
        self.template.write_text(TEMPLATE, encoding='utf-8')
        self.user.write_text(USER, encoding='utf-8')
        State.deploy_config = DeployConfig(
            file=str(self.user),
            template_file=str(self.template),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_has_no_git_or_update_group(self):
        schema = deploy_settings.deploy_settings_schema(lambda value: value)
        groups = [group['key'] for group in schema['groups']]
        self.assertNotIn('Git', groups)
        self.assertNotIn('Update', groups)
        webui = next(group for group in schema['groups'] if group['key'] == 'Webui')
        self.assertIn('EnableReload', [field['key'] for field in webui['fields']])

    def test_explicit_save_preserves_legacy_unknown_and_comment(self):
        result = deploy_settings.save_deploy_settings(
            {'values': {'WebuiPort': 26666, 'EnableReload': True}}
        )
        self.assertEqual(result['updated'], ['EnableReload', 'WebuiPort'])
        text = self.user.read_text(encoding='utf-8')
        self.assertIn('# keep me', text)
        self.assertIn('Repository: git://git.pull/AzurPilot', text)
        self.assertIn('UnknownCustomKey: preserve-me', text)
        self.assertIn('EnableReload: true', text)
        self.assertIn('WebuiPort: 26666', text)

    def test_startup_run_save_only_patches_run(self):
        result = deploy_settings.set_startup_run('alpha', True)
        self.assertTrue(result['enabled'])
        text = self.user.read_text(encoding='utf-8')
        self.assertIn('UnknownCustomKey: preserve-me', text)
        self.assertIn('Run: ["alpha"]', text)

if __name__ == '__main__':
    unittest.main()
