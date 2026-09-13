from __future__ import annotations

import types
import unittest
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript
from module.exception import GameStuckError


class FullChainRecoveryTests(unittest.TestCase):
    def make_script(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.cross_get.return_value = False
        script.config.Error_GameStuckRestart = True
        script.config.Error_GameStuckThreshold = 3
        script.config.Error_AdbOfflineRestart = True
        script.config.Error_AdbOfflineThreshold = 3
        script.config.Error_OnePushConfig = Mock()
        script.__dict__['device'] = Mock()
        script.device.package = 'com.YoStarEN.AzurLane'
        script.__dict__['commission'] = Mock(side_effect=GameStuckError('synthetic stuck'))
        script.consecutive_game_stuck = 0
        script.consecutive_adb_offline = 0
        script._last_emulator_recovery_mode = ''
        script._emulator_recovery_transport_lost = False
        script.save_error_log = Mock()
        return script

    def run_quiet(self, script):
        with (
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
            patch('alas.logger.exception_context'),
        ):
            return script.run('commission', skip_first_screenshot=True)

    def test_full_chain_hard_kill_success_preserves_required_order(self):
        script = self.make_script()
        order = []
        health_calls = 0

        def game_health():
            nonlocal health_calls
            health_calls += 1
            order.append(f'game-health-{health_calls}')
            return health_calls == 2

        script.device.release_during_wait.side_effect = lambda: order.append('release-old-device')
        platform = types.SimpleNamespace(
            emulator_instance=types.SimpleNamespace(name='MuMuPlayerGlobal-15.0-1'),
            emulator_stop=Mock(side_effect=lambda: order.append('graceful-stop') or False),
            emulator_force_stop_instance=Mock(side_effect=lambda: order.append('hard-kill') or True),
            emulator_start=Mock(side_effect=lambda: order.append('cold-start') or True),
        )
        fresh_device = Mock()

        with (
            patch.object(script, '_try_restart_game', side_effect=game_health),
            patch('module.device.platform.get_recovery_platform', return_value=platform),
            patch(
                'module.recovery.emulator_recovery._fresh_recovery_device',
                side_effect=lambda **_: order.append('fresh-device') or fresh_device,
            ),
        ):
            result = self.run_quiet(script)

        self.assertEqual('recoverable', result)
        self.assertEqual(
            [
                'game-health-1',
                'release-old-device',
                'graceful-stop',
                'hard-kill',
                'cold-start',
                'fresh-device',
                'game-health-2',
            ],
            order,
        )
        self.assertIs(script.device, fresh_device)
        self.assertEqual('hard-kill', script._last_emulator_recovery_mode)
        self.assertFalse(script._emulator_recovery_transport_lost)

    def test_full_chain_graceful_success_never_calls_hard_kill(self):
        script = self.make_script()
        order = []
        health_calls = 0

        def game_health():
            nonlocal health_calls
            health_calls += 1
            order.append(f'game-health-{health_calls}')
            return health_calls == 2

        platform = types.SimpleNamespace(
            emulator_instance=types.SimpleNamespace(name='MuMuPlayerGlobal-15.0-1'),
            emulator_stop=Mock(side_effect=lambda: order.append('graceful-stop') or True),
            emulator_force_stop_instance=Mock(side_effect=lambda: order.append('hard-kill') or True),
            emulator_start=Mock(side_effect=lambda: order.append('cold-start') or True),
        )
        fresh_device = Mock()

        with (
            patch.object(script, '_try_restart_game', side_effect=game_health),
            patch('module.device.platform.get_recovery_platform', return_value=platform),
            patch(
                'module.recovery.emulator_recovery._fresh_recovery_device',
                side_effect=lambda **_: order.append('fresh-device') or fresh_device,
            ),
        ):
            result = self.run_quiet(script)

        self.assertEqual('recoverable', result)
        platform.emulator_force_stop_instance.assert_not_called()
        self.assertNotIn('hard-kill', order)
        self.assertEqual('graceful', script._last_emulator_recovery_mode)

    def test_hard_kill_failure_is_bounded_and_invalidates_stale_device(self):
        script = self.make_script()
        old_device = script.device
        platform = types.SimpleNamespace(
            emulator_instance=types.SimpleNamespace(name='MuMuPlayerGlobal-15.0-1'),
            emulator_stop=Mock(return_value=False),
            emulator_force_stop_instance=Mock(return_value=False),
            emulator_start=Mock(return_value=True),
        )

        with (
            patch.object(script, '_try_restart_game', return_value=False) as game_health,
            patch('module.device.platform.get_recovery_platform', return_value=platform),
        ):
            result = self.run_quiet(script)

        self.assertFalse(result)
        game_health.assert_called_once_with()
        platform.emulator_stop.assert_called_once_with()
        platform.emulator_force_stop_instance.assert_called_once_with()
        platform.emulator_start.assert_not_called()
        self.assertNotIn('device', script.__dict__)
        self.assertTrue(script._emulator_recovery_transport_lost)
        old_device.release_during_wait.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
