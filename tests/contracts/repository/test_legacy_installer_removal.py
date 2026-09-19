import unittest
from pathlib import Path

from tests.support.contracts import assert_no_legacy_operator_surfaces
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def legacy_runtime_exists(path: Path) -> bool:
    """Не считать оставшийся после удалённого runtime bytecode рабочим кодом."""
    if not path.exists():
        return False
    if path.is_file():
        return True

    return any(
        '__pycache__' not in candidate.relative_to(path).parts
        for candidate in path.rglob('*')
    )


class LegacyInstallerRemovalTests(unittest.TestCase):
    def test_autonomous_git_runtime_is_absent(self):
        for relative_path in (
            'deploy/geo.py',
            'deploy/git.py',
            'deploy/Windows/git.py',
            'deploy/installer.py',
            'deploy/Windows/installer_test.py',
            'deploy/git_over_cdn',
            'tests/test_git_over_cdn.py',
        ):
            with self.subTest(relative_path=relative_path):
                self.assertFalse(legacy_runtime_exists(ROOT / relative_path))

    def test_cn_only_uncensored_runtime_is_absent(self):
        self.assertFalse(legacy_runtime_exists(ROOT / 'module/daemon/uncensored.py'))

    def test_operator_cutover_has_one_python_owner(self):
        assert_no_legacy_operator_surfaces(ROOT)
        self.assertTrue((ROOT / 'azurpilot/tooling/lifecycle.py').is_file())
        self.assertTrue((ROOT / 'azurpilot/tooling/update.py').is_file())
        self.assertTrue((ROOT / 'azurpilot/tooling/repair.py').is_file())
        self.assertTrue((ROOT / 'azurpilot/tooling/bootstrap.py').is_file())


if __name__ == '__main__':
    unittest.main()
