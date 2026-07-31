import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import set as deploy_set


TEMPLATE = '''Deploy:
  Python:
    PythonExecutable: python
    InstallDependencies: true
  Webui:
    WebuiPort: 25548
'''

USER = '''# keep comment
UnknownCustomKey: preserve-me
PythonExecutable: custom-python
WebuiPort: 25548
'''


class DeploySetTests(unittest.TestCase):
    def test_explicit_cli_update_preserves_unknown_keys_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / 'template.yaml'
            output = root / 'deploy.yaml'
            template.write_text(TEMPLATE, encoding='utf-8')
            output.write_text(USER, encoding='utf-8')

            with patch.object(deploy_set, 'DEPLOY_TEMPLATE', str(template)), patch.object(
                deploy_set,
                'get_args',
                return_value={'WebuiPort': '26666'},
            ):
                deploy_set.config_set(output=str(output))

            text = output.read_text(encoding='utf-8')
            self.assertIn('# keep comment', text)
            self.assertIn('UnknownCustomKey: preserve-me', text)
            self.assertIn('PythonExecutable: custom-python', text)
            self.assertIn('WebuiPort: 26666', text)

    def test_value_can_contain_equals_sign(self):
        with patch.object(
            deploy_set.sys,
            'argv',
            ['deploy.set', 'Password=a=b=c'],
        ):
            self.assertEqual(deploy_set.get_args(), {'Password': 'a=b=c'})


if __name__ == '__main__':
    unittest.main()
