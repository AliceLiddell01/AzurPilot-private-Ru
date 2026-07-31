import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
GUI_PATH = ROOT / 'module/config/argument/gui.yaml'


class Stage3FGuiSchemaTests(unittest.TestCase):
    def test_legacy_updater_and_git_labels_are_absent(self):
        data = yaml.safe_load(GUI_PATH.read_text(encoding='utf-8'))

        self.assertNotIn('Update', data)
        self.assertNotIn('Updating', data['Status'])
        self.assertNotIn('Update', data['MenuDevelop'])
        for key in (
            'CheckUpdate',
            'ClickToUpdate',
            'RetryUpdate',
            'CancelUpdate',
        ):
            self.assertNotIn(key, data['Button'])

        deploy = data['DeploySetting']
        for key in (
            'GroupGit',
            'GroupUpdate',
            'Repository',
            'RepositoryHelp',
            'Branch',
            'BranchHelp',
            'GitExecutable',
            'GitExecutableHelp',
            'GitProxy',
            'GitProxyHelp',
            'SSLVerify',
            'SSLVerifyHelp',
        ):
            self.assertNotIn(key, deploy)

    def test_active_supervisor_and_runtime_labels_remain(self):
        data = yaml.safe_load(GUI_PATH.read_text(encoding='utf-8'))
        deploy = data['DeploySetting']

        for key in (
            'GroupPython',
            'GroupAdb',
            'GroupOcr',
            'GroupMisc',
            'GroupRemoteAccess',
            'GroupWebui',
            'EnableReload',
            'EnableReloadHelp',
            'WebuiHost',
            'WebuiPort',
        ):
            self.assertIn(key, deploy)


if __name__ == '__main__':
    unittest.main()
