import logging
import unittest
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript
from module.exception import GameNotRunningError, GameStuckError, GameTooManyClickError
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


class TestGameStuckRecovery(unittest.TestCase):
    def _make_script(self, error, *, stuck_count=0, threshold=3, sensitive=False):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script.__dict__['config'] = Mock()
        script.config.cross_get.return_value = sensitive
        script.config.Error_GameStuckThreshold = threshold
        script.config.Error_OnePushConfig = Mock()
        script.__dict__['device'] = Mock()
        script.device.package = 'com.YoStarEN.AzurLane'
        script.__dict__['commission'] = Mock(side_effect=error)
        script.consecutive_game_stuck = stuck_count
        script.save_error_log = Mock()
        script._try_restart_emulator = Mock(return_value=True)
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

        with patch.object(script, '_try_restart_game', return_value=True) as restart_game:
            result = self._run_with_quiet_notifications(script)

        self.assertEqual('recoverable', result)
        restart_game.assert_called_once_with()
        script._try_restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()
        self.assertEqual(1, script.consecutive_game_stuck)

    def test_too_many_clicks_uses_same_verified_game_recovery(self):
        script = self._make_script(GameTooManyClickError('synthetic click loop'))

        with patch.object(script, '_try_restart_game', return_value=True) as restart_game:
            result = self._run_with_quiet_notifications(script)

        self.assertEqual('recoverable', result)
        restart_game.assert_called_once_with()
        script._try_restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()

    def test_failed_game_restart_is_not_masked_as_recoverable(self):
        script = self._make_script(GameStuckError('synthetic stuck'))

        with patch.object(script, '_try_restart_game', return_value=False) as restart_game:
            result = self._run_with_quiet_notifications(script)

        self.assertFalse(result)
        restart_game.assert_called_once_with()
        script._try_restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()

    def test_threshold_stops_recovery_loop_without_touching_emulator(self):
        script = self._make_script(
            GameStuckError('synthetic repeated stuck'),
            stuck_count=3,
            threshold=3,
        )

        with patch.object(script, '_try_restart_game', return_value=True) as restart_game:
            result = self._run_with_quiet_notifications(script)

        self.assertFalse(result)
        restart_game.assert_not_called()
        script._try_restart_emulator.assert_not_called()
        script.device.emulator_stop.assert_not_called()
        script.device.emulator_start.assert_not_called()
        script.config.task_call.assert_not_called()
        self.assertEqual(4, script.consecutive_game_stuck)

    def test_sensitive_task_never_attempts_game_or_emulator_recovery(self):
        script = self._make_script(
            GameStuckError('synthetic sensitive-task stuck'),
            sensitive=True,
        )

        with (
            patch.object(script, '_try_restart_game', return_value=True) as restart_game,
            patch('alas.handle_notify'),
            patch('alas.notify_webui'),
            patch('alas.logger.error_context'),
            self.assertRaises(SystemExit),
        ):
            script.run('commission', skip_first_screenshot=True)

        restart_game.assert_not_called()
        script._try_restart_emulator.assert_not_called()
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


if __name__ == '__main__':
    unittest.main()
