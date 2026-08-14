from __future__ import annotations

import types
import unittest
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript


class EmulatorRecoverySchedulerPolicyTests(unittest.TestCase):
    def make_script(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.Error_GameStuckRestart = True
        script.config.Error_AdbOfflineRestart = True
        script.config.Error_AdbOfflineThreshold = 3
        script.consecutive_adb_offline = 0
        script.__dict__['device'] = Mock()
        return script

    @staticmethod
    def outcome(*, mode='graceful', device=None):
        return types.SimpleNamespace(
            success=True,
            stage='transport-ready',
            mode=mode,
            instance_name='MuMuPlayerGlobal-15.0-1',
            device=device or Mock(),
        )

    @staticmethod
    def remove_cached(obj, name):
        obj.__dict__.pop(name, None)

    def test_scheduled_recovery_does_not_consume_adb_budget_or_allow_hard_kill(self):
        script = self.make_script()
        fresh = Mock()

        with (
            patch(
                'module.recovery.emulator_recovery.recover_emulator_transport',
                return_value=self.outcome(device=fresh),
            ) as recovery,
            patch('alas.del_cached_property', side_effect=self.remove_cached),
        ):
            result = script._try_restart_emulator(reason='scheduled')

        self.assertTrue(result)
        self.assertEqual(0, script.consecutive_adb_offline)
        self.assertFalse(recovery.call_args.kwargs['allow_hard_kill'])
        self.assertIs(fresh, script.device)

    def test_game_stuck_recovery_does_not_consume_adb_budget(self):
        script = self.make_script()
        script.consecutive_adb_offline = 2

        with (
            patch(
                'module.recovery.emulator_recovery.recover_emulator_transport',
                return_value=self.outcome(),
            ) as recovery,
            patch.object(script, '_try_restart_game', return_value=True) as game_health,
            patch('alas.del_cached_property', side_effect=self.remove_cached),
        ):
            result = script._try_restart_emulator(reason='game_stuck', verify_game=True)

        self.assertTrue(result)
        self.assertEqual(2, script.consecutive_adb_offline)
        self.assertTrue(recovery.call_args.kwargs['allow_hard_kill'])
        game_health.assert_called_once_with()

    def test_adb_recovery_consumes_only_adb_budget(self):
        script = self.make_script()

        with (
            patch(
                'module.recovery.emulator_recovery.recover_emulator_transport',
                return_value=self.outcome(),
            ) as recovery,
            patch('alas.del_cached_property', side_effect=self.remove_cached),
        ):
            result = script._try_restart_emulator(reason='adb_offline')

        self.assertTrue(result)
        self.assertEqual(1, script.consecutive_adb_offline)
        self.assertTrue(recovery.call_args.kwargs['allow_hard_kill'])

    def test_adb_threshold_blocks_transport_before_destructive_action(self):
        script = self.make_script()
        script.config.Error_AdbOfflineThreshold = 2
        script.consecutive_adb_offline = 2

        with patch('module.recovery.emulator_recovery.recover_emulator_transport') as recovery:
            result = script._try_restart_emulator(reason='adb_offline')

        self.assertFalse(result)
        self.assertEqual(3, script.consecutive_adb_offline)
        recovery.assert_not_called()


if __name__ == '__main__':
    unittest.main()
