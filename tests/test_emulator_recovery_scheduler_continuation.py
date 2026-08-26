from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from alas import AzurLaneAutoScript


class SchedulerContinuationTests(unittest.TestCase):
    def make_script(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.is_first_task = False
        script.failure_record = {}
        script.consecutive_game_stuck = 2
        script.consecutive_adb_offline = 1
        script._last_emulator_recovery_mode = 'hard-kill'
        script._emulator_recovery_transport_lost = False
        script.last_emulator_restart_time = 0
        script._manual_scan_wakeup = False
        script.__dict__['config'] = Mock()
        script.config.EmulatorManagement_ScheduledEmulatorRestart = False
        script.config.FleetAutoScan_Mode = 'disabled'
        script.config.FleetAutoScan_Fleets = [1, 2, 3, 4, 5, 6]
        script.config.Scheduler_PushNotification = False
        script.config.Error_StrictRestart = False
        script.config.Error_HandleError = True
        script.config.Error_OnePushConfig = Mock()
        script.config.cross_get.return_value = False
        script.__dict__['device'] = Mock()
        script.__dict__['checker'] = Mock()
        script.checker.is_recovered.return_value = False
        manual_scan = Mock()
        manual_scan.process_next.return_value = None
        manual_scan.has_pending.return_value = False
        script.__dict__['fleet_manual_scan'] = manual_scan
        return script

    def test_recoverable_incident_continues_to_next_task_and_normal_success_resets_budgets(self):
        script = self.make_script()

        with (
            patch('module.config.utils.is_oobe_needed', return_value=False),
            patch('alas.del_cached_property'),
            patch.object(script, 'get_next_task', side_effect=['TaskA', 'TaskB', SystemExit]),
            patch.object(script, 'run', side_effect=['recoverable', True]) as run_task,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            self.assertRaises(SystemExit),
        ):
            script.loop()

        self.assertEqual(
            [call('task_a'), call('task_b')],
            run_task.call_args_list,
        )
        self.assertEqual(0, script.consecutive_game_stuck)
        self.assertEqual(0, script.consecutive_adb_offline)
        self.assertEqual(0, script.failure_record.get('TaskA', 0))
        self.assertEqual(0, script.failure_record.get('TaskB', 0))
        script.checker.check_now.assert_called_once_with()

    def test_transport_loss_stops_scheduler_before_next_task(self):
        script = self.make_script()

        def fail_with_transport_loss(_command):
            script._emulator_recovery_transport_lost = True
            return False

        with (
            patch('module.config.utils.is_oobe_needed', return_value=False),
            patch('alas.del_cached_property'),
            patch.object(script, 'get_next_task', return_value='TaskA') as get_next_task,
            patch.object(script, 'run', side_effect=fail_with_transport_loss) as run_task,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
        ):
            script.loop()

        get_next_task.assert_called_once_with()
        run_task.assert_called_once_with('task_a')
        self.assertTrue(script._emulator_recovery_transport_lost)


if __name__ == '__main__':
    unittest.main()
