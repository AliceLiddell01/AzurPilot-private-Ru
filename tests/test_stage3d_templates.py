import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    'config/deploy.template.yaml',
    'config/deploy.template-cn.yaml',
    'config/deploy.template-AidLux.yaml',
    'config/deploy.template-AidLux-cn.yaml',
    'config/deploy.template-docker.yaml',
    'config/deploy.template-docker-cn.yaml',
    'config/deploy.template-linux.yaml',
    'config/deploy.template-linux-cn.yaml',
    'deploy/template',
    'deploy/Windows/template.yaml',
)


class Stage3DTemplateTests(unittest.TestCase):
    def test_templates_have_only_active_runtime_groups(self):
        for relative_path in TEMPLATES:
            with self.subTest(relative_path=relative_path):
                path = ROOT / relative_path
                data = yaml.safe_load(path.read_text(encoding='utf-8'))['Deploy']
                self.assertNotIn('Git', data)
                self.assertNotIn('Update', data)
                self.assertIn('EnableReload', data['Webui'])
                self.assertEqual(
                    set(data),
                    {'Python', 'Adb', 'Ocr', 'Misc', 'RemoteAccess', 'Webui'},
                )

    def test_legacy_updater_keys_do_not_appear_in_new_templates(self):
        forbidden = (
            'Repository:',
            'Branch:',
            'GitExecutable:',
            'GitProxy:',
            'SSLVerify:',
            'GitOverCdn:',
            'CheckUpdateInterval:',
            'AutoRestartTime:',
            'git://git.pull/AzurPilot',
            'git.nanoda.work',
        )
        for relative_path in TEMPLATES:
            with self.subTest(relative_path=relative_path):
                text = (ROOT / relative_path).read_text(encoding='utf-8')
                for token in forbidden:
                    self.assertNotIn(token, text)

    def test_platform_specific_values_are_preserved(self):
        linux = yaml.safe_load(
            (ROOT / 'config/deploy.template-linux.yaml').read_text(encoding='utf-8')
        )['Deploy']
        windows = yaml.safe_load(
            (ROOT / 'config/deploy.template.yaml').read_text(encoding='utf-8')
        )['Deploy']
        cn = yaml.safe_load(
            (ROOT / 'config/deploy.template-cn.yaml').read_text(encoding='utf-8')
        )['Deploy']

        self.assertEqual(linux['Python']['PythonExecutable'], './.venv/bin/python')
        self.assertEqual(linux['Adb']['AdbExecutable'], './.venv/bin/adb')
        self.assertFalse(linux['Adb']['ReplaceAdb'])
        self.assertEqual(linux['RemoteAccess']['SSHExecutable'], '/usr/bin/ssh')
        self.assertEqual(windows['Python']['PythonExecutable'], './.venv/Scripts/python.exe')
        self.assertEqual(cn['Python']['PypiMirror'], 'https://mirrors.aliyun.com/pypi/simple')
        self.assertEqual(cn['Webui']['Language'], 'zh-CN')


if __name__ == '__main__':
    unittest.main()
