from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / 'module/device/platform/platform_windows_recovery.py'
PLATFORM_INIT = ROOT / 'module/device/platform/__init__.py'


class MuMuWindowsRecoveryContractTests(unittest.TestCase):
    def test_windows_platform_alias_uses_safe_recovery_subclass(self):
        text = PLATFORM_INIT.read_text(encoding='utf-8')
        self.assertIn('RecoveryPlatformWindows as Platform', text)
        self.assertNotIn('platform_windows import PlatformWindows as Platform', text)

    def test_mumu_manager_commands_use_argv_and_shell_false(self):
        text = RECOVERY.read_text(encoding='utf-8')
        tree = ast.parse(text)
        self.assertIn('shell=False', text)
        self.assertIn("'shutdown_player'", text)
        self.assertIn("'launch_player'", text)

        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue({'api', '-v'} <= literals)

        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        subprocess_runs = [
            node for node in calls
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'subprocess'
            and node.func.attr == 'run'
        ]
        self.assertEqual(1, len(subprocess_runs))
        shell_keywords = {
            keyword.arg: keyword.value for keyword in subprocess_runs[0].keywords
        }
        self.assertIsInstance(shell_keywords['shell'], ast.Constant)
        self.assertFalse(shell_keywords['shell'].value)

    def test_graceful_stop_requires_actual_state_probe(self):
        text = RECOVERY.read_text(encoding='utf-8')
        self.assertIn('wait_mumu_instance_stopped(', text)
        self.assertIn('Штатная остановка подтверждена', text)
        self.assertIn('экземпляр остаётся запущен', text)

    def test_cold_start_requires_manager_success_and_boot_health(self):
        text = RECOVERY.read_text(encoding='utf-8')
        self.assertIn('result is None or result.returncode != 0', text)
        self.assertIn('self.emulator_start_watch()', text)
        self.assertIn('Проверка загрузки пройдена', text)
        self.assertIn('partially_running = is_mumu_instance_running(instance)', text)

    def test_hidden_global_cleanup_is_not_reused(self):
        text = RECOVERY.read_text(encoding='utf-8')
        for forbidden in (
            'kill_process_by_regex(',
            "name.lower() in ('mumuplayer.exe'",
            'proc.kill()',
            'taskkill',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
