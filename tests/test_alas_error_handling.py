import logging
import types
import unittest
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript
from module.config.config import TaskEnd
from module.exception import (
    EmulatorNotRunningError,
    GameNotRunningError,
    GameStuckError,
    GameTooManyClickError,
)
from module.logger import error_context


class TestErrorContext(unittest.TestCase):
    def test_can_log_exception_summary_without_traceback(self):
        error = GameNotRunningError('Game not running')

        with patch('module.logger.logger.log') as log:
            error_context(
                title='Игровой процесс не запущен',
                reason='Перед выполнением задачи процесс Azur Lane не обнаружен.',
                impact='Текущая задача пропущена.',
                action='Автоматически перезапустить игру.',
                exc=error,
                level=logging.WARNING,
                with_traceback=False,
            )

        self.assertFalse(log.call_args.kwargs['exc_info'])
        self.assertIn('Исключение: GameNotRunningError: Game not running', log.call_args.args[1])


class TestGameNotRunningErrorHandling(unittest.TestCase):
    def test_schedules_restart_without_requesting_traceback(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.cross_get.return_value = False
        error = GameNotRunningError('Game not running')
        script.__dict__['commission'] = Mock(side_effect=error)

        with (
            patch('alas.logger.error_context') as error_context_mock,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
        ):
            result = script.run('commission', skip_first_screenshot=True)

        self.assertEqual('recoverable', result)
        script.config.task_call.assert_called_once_with('Restart')
        error_context_mock.assert_called_once_with(
            title='Игровой процесс не запущен',
            reason='Перед выполнением задачи процесс Azur Lane не обнаружен.',
            impact='Текущая задача пропущена; планировщик автоматически назначит задачу Restart.',
            action='Обычно действие не требуется. При повторении проверьте имя пакета игры, состояние эмулятора и процедуру входа.',
            exc=error,
            level=30,
            with_traceback=False,
        )

    def test_task_end_stays_success_when_metrics_hook_fails(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = "test"
        script.__dict__["commission"] = Mock(side_effect=TaskEnd("normal stop"))

        with patch(
            "module.observability.mark_task_stopped",
            side_effect=RuntimeError("metrics hook failed"),
        ):
            result = script.run("commission", skip_first_screenshot=True)

        self.assertTrue(result)


class TestGameStuckRecovery(unittest.TestCase):
    def _make_script(
        self,
        error,
        *,
        stuck_count=0,
        threshold=3,
        sensitive=False,
        emulator_escalation=False,
    ):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.cross_get.return_value = sensitive
        script.config.Error_GameStuckRestart = emulator_escalation
        script.config.Error_GameStuckThreshold = threshold
        script.config.Error_AdbOfflineRestart = True
        script.config.Error_AdbOfflineThreshold = 3
        script.config.Error_OnePushConfig = Mock()
        script.__dict__['device'] = Mock()
        script.device.package = 'com.YoStarEN.AzurLane'
        script.__dict__['commission'] = Mock(side_effect=error)
        script.consecutive_game_stuck = stuck_count
        script.consecutive_adb_offline = 0
        script._emulator_recovery_transport_lost = False
        script.save_error_log = Mock()
        return script

    def _run_with_quiet_notifications(self, script):
        with (
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
            patch('alas.logger.exception_context'),
        ):
            return script.run('commission', skip_first_screenshot=True)

    def test_stuck_restarts_only_game_and_reports_recoverable_after_health_success(self):
        script = self._make_script(GameStuckError('synthetic stuck'))

        with (
            patch.object(script, '_try_restart_game', return_value=True) as restart_game,
            patch.object(script, '_try_restart_emulator', return_value=True) as restart_emulator,
        ):
            result = self._run_with_quiet_notifications(script)

        self.assertEqual('recoverable', result)
        restart_game.assert_called_once_with()
        restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()
        self.assertEqual(1, script.consecutive_game_stuck)

    def test_too_many_clicks_uses_same_verified_game_recovery_first(self):
        script = self._make_script(GameTooManyClickError('synthetic click loop'))

        with (
            patch.object(script, '_try_restart_game', return_value=True) as restart_game,
            patch.object(script, '_try_restart_emulator', return_value=True) as restart_emulator,
        ):
            result = self._run_with_quiet_notifications(script)

        self.assertEqual('recoverable', result)
        restart_game.assert_called_once_with()
        restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()

    def test_failed_game_restart_without_stage2_policy_remains_failure(self):
        script = self._make_script(
            GameStuckError('synthetic stuck'),
            emulator_escalation=False,
        )

        with patch.object(script, '_try_restart_game', return_value=False) as restart_game:
            result = self._run_with_quiet_notifications(script)

        self.assertFalse(result)
        restart_game.assert_called_once_with()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()

    def test_failed_game_restart_escalates_once_when_policy_enabled(self):
        script = self._make_script(
            GameStuckError('synthetic stuck'),
            emulator_escalation=True,
        )

        with (
            patch.object(script, '_try_restart_game', return_value=False) as restart_game,
            patch.object(script, '_try_restart_emulator', return_value=True) as restart_emulator,
        ):
            result = self._run_with_quiet_notifications(script)

        self.assertEqual('recoverable', result)
        restart_game.assert_called_once_with()
        restart_emulator.assert_called_once_with(reason='game_stuck', verify_game=True)
        script.config.task_call.assert_not_called()

    def test_failed_game_restart_and_failed_stage2_is_not_recoverable(self):
        script = self._make_script(
            GameStuckError('synthetic stuck'),
            emulator_escalation=True,
        )

        with (
            patch.object(script, '_try_restart_game', return_value=False),
            patch.object(script, '_try_restart_emulator', return_value=False) as restart_emulator,
        ):
            result = self._run_with_quiet_notifications(script)

        self.assertFalse(result)
        restart_emulator.assert_called_once_with(reason='game_stuck', verify_game=True)
        script.config.task_call.assert_not_called()

    def test_threshold_stops_recovery_loop_without_new_emulator_escalation(self):
        script = self._make_script(
            GameStuckError('synthetic repeated stuck'),
            stuck_count=3,
            threshold=3,
            emulator_escalation=True,
        )

        with (
            patch.object(script, '_try_restart_game', return_value=True) as restart_game,
            patch.object(script, '_try_restart_emulator', return_value=True) as restart_emulator,
        ):
            result = self._run_with_quiet_notifications(script)

        self.assertFalse(result)
        restart_game.assert_not_called()
        restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()
        self.assertEqual(4, script.consecutive_game_stuck)

    def test_sensitive_task_never_attempts_game_or_emulator_recovery(self):
        script = self._make_script(
            GameStuckError('synthetic sensitive-task stuck'),
            sensitive=True,
            emulator_escalation=True,
        )

        with (
            patch.object(script, '_try_restart_game', return_value=True) as restart_game,
            patch.object(script, '_try_restart_emulator', return_value=True) as restart_emulator,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
            self.assertRaises(SystemExit),
        ):
            script.run('commission', skip_first_screenshot=True)

        restart_game.assert_not_called()
        restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()

    def test_try_restart_game_requires_existing_login_restart_contract_to_finish(self):
        script = self._make_script(GameStuckError('unused'))

        with patch('module.handler.login.LoginHandler') as login_handler:
            result = script._try_restart_game()

        self.assertTrue(result)
        login_handler.assert_called_once_with(script.config, device=script.device)
        login_handler.return_value.app_restart.assert_called_once_with()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()

    def test_try_restart_game_converts_restart_exception_to_explicit_failure(self):
        script = self._make_script(GameStuckError('unused'))

        with (
            patch('module.handler.login.LoginHandler') as login_handler,
            patch('alas.logger.exception_context') as exception_context,
        ):
            login_handler.return_value.app_restart.side_effect = GameStuckError('still stuck after restart')
            result = script._try_restart_game()

        self.assertFalse(result)
        exception_context.assert_called_once()
        self.assertIn('game restart failed', exception_context.call_args.kwargs['reason'])
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()

    def test_game_stuck_policy_gate_is_independent_from_adb_budget(self):
        script = self._make_script(
            GameStuckError('unused'),
            emulator_escalation=False,
        )
        script.config.Error_AdbOfflineRestart = True
        script.consecutive_adb_offline = 2

        with patch('module.recovery.emulator_recovery.recover_emulator_transport') as recovery:
            self.assertFalse(script._try_restart_emulator(reason='game_stuck', verify_game=True))

        recovery.assert_not_called()
        self.assertEqual(2, script.consecutive_adb_offline)

    def test_transport_failure_cannot_be_reported_as_emulator_restart_success(self):
        script = self._make_script(GameStuckError('unused'))
        outcome = types.SimpleNamespace(
            success=False,
            stage='cold-start',
            mode='graceful',
            instance_name='MuMuPlayerGlobal-15.0-1',
            device=None,
        )

        with patch(
            'module.recovery.emulator_recovery.recover_emulator_transport',
            return_value=outcome,
        ) as recovery:
            result = script._try_restart_emulator(reason='adb_offline')

        self.assertFalse(result)
        self.assertNotIn('device', script.__dict__)
        self.assertTrue(script._emulator_recovery_transport_lost)
        recovery.assert_called_once()
        self.assertTrue(recovery.call_args.kwargs['allow_hard_kill'])
        self.assertEqual(1, script.consecutive_adb_offline)

    def test_successful_transport_replaces_cached_device(self):
        script = self._make_script(GameStuckError('unused'))
        fresh_device = Mock()
        outcome = types.SimpleNamespace(
            success=True,
            stage='transport-ready',
            mode='graceful',
            instance_name='MuMuPlayerGlobal-15.0-1',
            device=fresh_device,
        )

        def remove_cached(obj, name):
            obj.__dict__.pop(name, None)

        with (
            patch(
                'module.recovery.emulator_recovery.recover_emulator_transport',
                return_value=outcome,
            ),
            patch('alas.del_cached_property', side_effect=remove_cached),
        ):
            result = script._try_restart_emulator(reason='adb_offline')

        self.assertTrue(result)
        self.assertIs(fresh_device, script.device)
        self.assertEqual('graceful', script._last_emulator_recovery_mode)

    def test_post_emulator_game_health_failure_does_not_recurse(self):
        script = self._make_script(
            GameStuckError('unused'),
            emulator_escalation=True,
        )
        fresh_device = Mock()
        outcome = types.SimpleNamespace(
            success=True,
            stage='transport-ready',
            mode='hard-kill',
            instance_name='MuMuPlayerGlobal-15.0-1',
            device=fresh_device,
        )

        def remove_cached(obj, name):
            obj.__dict__.pop(name, None)

        with (
            patch(
                'module.recovery.emulator_recovery.recover_emulator_transport',
                return_value=outcome,
            ) as recovery,
            patch.object(script, '_try_restart_game', return_value=False) as game_health,
            patch('alas.del_cached_property', side_effect=remove_cached),
        ):
            result = script._try_restart_emulator(reason='game_stuck', verify_game=True)

        self.assertFalse(result)
        recovery.assert_called_once()
        game_health.assert_called_once_with()
        self.assertIs(fresh_device, script.device)


class TestAdbOfflineRecovery(unittest.TestCase):
    def test_successful_direct_adb_recovery_uses_separate_policy_and_schedules_restart(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.cross_get.return_value = False
        script.config.Error_OnePushConfig = Mock()
        script.__dict__['device'] = Mock()
        script.__dict__['commission'] = Mock(side_effect=EmulatorNotRunningError('offline'))
        script.save_error_log = Mock()

        with (
            patch.object(script, '_try_restart_emulator', return_value=True) as recovery,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
        ):
            result = script.run('commission', skip_first_screenshot=True)

        self.assertEqual('recoverable', result)
        recovery.assert_called_once_with(reason='adb_offline')
        script.config.task_call.assert_called_once_with('Restart')


if __name__ == '__main__':
    unittest.main()
