import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

    def test_uncensored_task_keeps_device_flow_without_git_manager(self):
        path = ROOT / 'module/daemon/uncensored.py'
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(path))

        self.assertNotIn('GitManager', source)
        self.assertNotIn('git_repository_init', source)
        self.assertIn('self.create_level1_uncensored()', source)
        self.assertIn('self.device.adb_command(command, timeout=30)', source)
        self.assertIn('self.device.app_stop()', source)
        self.assertIn('self.device.app_start()', source)
        self.assertIn('self.handle_app_login()', source)
        self.assertTrue(any(isinstance(node, ast.Try) for node in ast.walk(tree)))

    def test_start_allows_supervisor_reload_without_updater_guard(self):
        source = (ROOT / 'scripts/Start-AzurPilot.ps1').read_text(encoding='utf-8-sig')

        self.assertIn("-Key 'EnableReload'", source)
        self.assertIn('EnableReload = $enableReload', source)
        self.assertNotIn(
            'EnableReload должен быть явно установлен в false',
            source,
        )
        self.assertNotIn(
            'встроенный updater снова получает управление обновлениями',
            source,
        )


if __name__ == '__main__':
    unittest.main()
