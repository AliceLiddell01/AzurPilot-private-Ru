from __future__ import annotations

import types
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript


def test_failed_scheduled_recovery_moves_retry_window_before_next_task():
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    script.config_name = 'scheduled-backoff'
    script.is_first_task = False
    script.failure_record = {}
    script.consecutive_game_stuck = 0
    script.consecutive_adb_offline = 0
    script.last_emulator_restart_time = 0.0
    script._emulator_recovery_transport_lost = False

    stop_state = {'value': False}
    script.stop_event = types.SimpleNamespace(is_set=lambda: stop_state['value'])

    config = types.SimpleNamespace(
        EmulatorManagement_ScheduledEmulatorRestart=True,
        EmulatorManagement_RestartIntervalHours=1,
        Scheduler_PushNotification=False,
        Error_OnePushConfig={},
        Error_StrictRestart=False,
        Error_HandleError=False,
        cross_get=lambda **_: False,
    )
    script.__dict__['config'] = config
    script.__dict__['checker'] = types.SimpleNamespace(
        wait_until_available=lambda: None,
        is_recovered=lambda: False,
    )
    script.__dict__['device'] = types.SimpleNamespace(
        config=None,
        stuck_record_clear=lambda: None,
        click_record_clear=lambda: None,
    )

    script.get_next_task = Mock(return_value='Synthetic')
    script._try_restart_emulator = Mock(return_value=False)

    def run_once(command):
        stop_state['value'] = True
        return True

    script.run = run_once

    with (
        patch('module.config.utils.is_oobe_needed', return_value=False),
        patch('alas.time.monotonic', side_effect=[7200.0, 7205.0]),
        patch('alas.logger'),
    ):
        script.loop()

    script._try_restart_emulator.assert_called_once_with(reason='scheduled')
    assert script.last_emulator_restart_time == 7205.0

def test_scheduled_recovery_stops_before_task_when_transport_is_lost():
    script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
    script.config_name = 'scheduled-transport-loss'
    script.is_first_task = False
    script.failure_record = {}
    script.consecutive_game_stuck = 0
    script.consecutive_adb_offline = 0
    script.last_emulator_restart_time = 0.0
    script._emulator_recovery_transport_lost = False
    script.stop_event = None

    config = types.SimpleNamespace(
        EmulatorManagement_ScheduledEmulatorRestart=True,
        EmulatorManagement_RestartIntervalHours=1,
    )
    script.__dict__['config'] = config
    script.__dict__['checker'] = types.SimpleNamespace(
        wait_until_available=lambda: None,
        is_recovered=lambda: False,
    )
    script.get_next_task = Mock()

    def fail_recovery(*, reason):
        assert reason == 'scheduled'
        script._emulator_recovery_transport_lost = True
        return False

    script._try_restart_emulator = Mock(side_effect=fail_recovery)

    with (
        patch('module.config.utils.is_oobe_needed', return_value=False),
        patch('alas.time.monotonic', side_effect=[7200.0, 7205.0]),
        patch('alas.logger'),
    ):
        script.loop()

    script._try_restart_emulator.assert_called_once_with(reason='scheduled')
    script.get_next_task.assert_not_called()
    assert script.last_emulator_restart_time == 7205.0
