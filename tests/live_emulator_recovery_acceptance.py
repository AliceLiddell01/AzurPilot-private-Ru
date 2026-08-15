"""Контролируемый приёмочный запуск Windows/MuMu для Stage 3.

Этот файл намеренно не начинается с ``test_`` и не выполняется pytest/CI.
Он разрушительно завершает выбранный MuMu instance только при явном
``--allow-hard-kill`` и использует production hard-kill/cold-start/fresh-Device
реализацию. Искусственными остаются только две boundary-инъекции: первый Stage 1
health result и graceful-stop result.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from unittest.mock import patch

import inflection

from alas import AzurLaneAutoScript
from module.config.utils import DEFAULT_CONFIG_NAME
from module.device.platform import get_recovery_platform
from module.exception import GameStuckError
from module.logger import logger


@dataclass
class ContinuationResult:
    task: str = ''
    outcome: object = None


class ForcedGracefulFailurePlatform:
    """Делегировать production lifecycle, кроме synthetic graceful result."""

    def __init__(self, delegate):
        self._delegate = delegate

    @property
    def emulator_instance(self):
        return self._delegate.emulator_instance

    def emulator_stop(self):
        logger.warning(
            '[Stage 3 live smoke] Synthetic boundary: graceful stop считается '
            'неуспешным; production hard-kill safety остаётся без изменений'
        )
        return False

    def emulator_force_stop_instance(self):
        return self._delegate.emulator_force_stop_instance()

    def emulator_start(self):
        return self._delegate.emulator_start()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Контролируемая разрушительная приёмочная проверка восстановления эмулятора Stage 3.',
    )
    parser.add_argument('--config', default=DEFAULT_CONFIG_NAME)
    parser.add_argument(
        '--allow-hard-kill',
        action='store_true',
        help='Явно разрешить instance-scoped hard kill выбранного MuMu instance.',
    )
    return parser.parse_args(argv)


def _require_runtime_safety(script: AzurLaneAutoScript):
    if sys.platform != 'win32':
        raise RuntimeError('Live Stage 3 smoke разрешён только на Windows.')

    config = script.config
    if not config.Error_GameStuckRestart or not config.Error_AdbOfflineRestart:
        raise RuntimeError(
            'Runtime profile не получил ожидаемый Stage 3 default-on rollout: '
            'GameStuckRestart и AdbOfflineRestart должны быть включены.'
        )

    platform = get_recovery_platform(config)
    instance = getattr(platform, 'emulator_instance', None)
    if instance is None:
        raise RuntimeError('Recovery backend не смог однозначно определить target emulator instance.')

    name = getattr(instance, 'name', '')
    instance_id = getattr(instance, 'MuMuPlayer12_id', None)
    if not name or 'mumu' not in name.casefold() or instance_id is None:
        raise RuntimeError(
            'Live hard-kill smoke разрешён только для однозначно определённого '
            'современного MuMu instance с name и instance id.'
        )

    logger.info(
        f'[Stage 3 live smoke] Target подтверждён: instance={name}, id={instance_id}'
    )
    return platform


def _run_full_chain(script: AzurLaneAutoScript, platform) -> None:
    old_device = script.device
    real_game_restart = script._try_restart_game
    health_calls = 0

    def injected_game_health():
        nonlocal health_calls
        health_calls += 1
        actual_result = real_game_restart()
        if health_calls == 1:
            logger.warning(
                '[Stage 3 live smoke] Synthetic boundary: первый реальный Stage 1 '
                f'restart завершился с result={actual_result!r}, но его health result '
                'принудительно считается FAIL для проверки эскалации'
            )
            return False
        return actual_result

    def injected_fault():
        raise GameStuckError('Stage 3 controlled live fault injection')

    script.__dict__['stage3_live_fault'] = injected_fault
    proxy = ForcedGracefulFailurePlatform(platform)

    try:
        with (
            patch.object(script, '_try_restart_game', side_effect=injected_game_health),
            patch('module.device.platform.get_recovery_platform', return_value=proxy),
        ):
            outcome = script.run('stage3_live_fault', skip_first_screenshot=True)
    finally:
        script.__dict__.pop('stage3_live_fault', None)

    if outcome != 'recoverable':
        raise RuntimeError(f'Full-chain live recovery не завершилась успешно: outcome={outcome!r}')
    if health_calls != 2:
        raise RuntimeError(
            f'Ожидались две Stage 1 health boundary проверки, получено: {health_calls}'
        )
    if getattr(script, '_last_emulator_recovery_mode', '') != 'hard-kill':
        raise RuntimeError(
            'Production recovery не подтвердил hard-kill mode после synthetic graceful failure.'
        )
    if getattr(script, '_emulator_recovery_transport_lost', False):
        raise RuntimeError('После успешной full-chain recovery transport остался помечен как lost.')
    if script.device is old_device:
        raise RuntimeError('Recovery не создала fresh Device.')

    logger.info('[Stage 3 live smoke] Full-chain hard-kill recovery PASS')


def _run_one_scheduler_task(script: AzurLaneAutoScript) -> ContinuationResult:
    """Дать production scheduler выполнить ровно одну фактическую следующую задачу."""
    stop_event = threading.Event()
    script.stop_event = stop_event
    script.is_first_task = False
    script.last_emulator_restart_time = time.monotonic()

    real_get_next_task = script.get_next_task
    real_run = script.run
    state = ContinuationResult()
    armed_command = {'value': ''}

    def get_next_task_once():
        task = real_get_next_task()
        armed_command['value'] = inflection.underscore(task)
        return task

    def run_once(command, *args, **kwargs):
        outcome = real_run(command, *args, **kwargs)
        if armed_command['value'] and command == armed_command['value']:
            state.task = command
            state.outcome = outcome
            stop_event.set()
        return outcome

    with (
        patch.object(script, 'get_next_task', side_effect=get_next_task_once),
        patch.object(script, 'run', side_effect=run_once),
    ):
        script.loop()

    if not state.task:
        raise RuntimeError('Scheduler не запустил следующую нормальную задачу после recovery.')
    if state.outcome is not True:
        raise RuntimeError(
            f'Следующая scheduler-задача {state.task!r} завершилась не обычным success: '
            f'{state.outcome!r}'
        )
    if script.consecutive_game_stuck != 0 or script.consecutive_adb_offline != 0:
        raise RuntimeError(
            'После обычного scheduler success recovery budgets не вернулись в ноль.'
        )

    logger.info(
        f'[Stage 3 live smoke] Scheduler continuation PASS: task={state.task}, outcome=True'
    )
    return state


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.allow_hard_kill:
        print(
            'ОТКАЗ: destructive live smoke требует явный флаг --allow-hard-kill. '
            'Без него ни один process kill не запускается.',
            file=sys.stderr,
        )
        return 2

    script = AzurLaneAutoScript(config_name=args.config)
    try:
        platform = _require_runtime_safety(script)
        _ = script.device
        _run_full_chain(script, platform)
        continuation = _run_one_scheduler_task(script)
    except Exception as exc:
        logger.exception_context(
            title='Stage 3 controlled live smoke FAIL',
            exc=exc,
            impact='Stage 3 нельзя считать принятой на реальной MuMu-среде.',
            action='Сохраните полный log.txt и консольный вывод; не повторяйте hard-kill smoke вслепую.',
            level=50,
        )
        return 1

    logger.info(
        '[Stage 3 live smoke] PASS: runtime default-on, production hard-kill recovery, '
        f'fresh Device/final UI health и следующая scheduler-задача {continuation.task}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
