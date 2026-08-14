"""Безопасное управление современными экземплярами MuMu на Windows.

Модуль расширяет существующий PlatformWindows только для семейства
MuMuPlayer12, куда текущий детектор также относит MuMuPlayer 6.0 / Android 15.
Штатная остановка проверяется по фактическому процессу выбранного экземпляра.
Принудительное завершение допускается только для однозначно найденного
MuMuNxDevice.exe выбранного экземпляра и его дочерних процессов; общие
MuMuNxMain/MuMuNxSVC никогда не входят в целевой набор.
"""

from __future__ import annotations

import subprocess

from module.device.platform.emulator_windows import Emulator, EmulatorInstance
from module.device.platform.mumu_process_control import (
    MuMuInstanceIdentityError,
    force_stop_mumu_instance,
    is_mumu_instance_running,
    wait_mumu_instance_stopped,
)
from module.device.platform.platform_windows import EmulatorUnknown, PlatformWindows
from module.logger import logger


class RecoveryPlatformWindows(PlatformWindows):
    """PlatformWindows с проверяемым instance-safe lifecycle современного MuMu."""

    MUMU_STOP_VERIFY_TIMEOUT = 15.0
    MUMU_STOP_VERIFY_INTERVAL = 0.5
    MUMU_START_ATTEMPTS = 3

    @staticmethod
    def _is_modern_mumu(instance) -> bool:
        return instance is not None and instance == Emulator.MuMuPlayer12

    @staticmethod
    def _mumu_manager_args(instance: EmulatorInstance, action: str) -> list[str]:
        instance_id = instance.MuMuPlayer12_id
        if instance_id is None:
            raise MuMuInstanceIdentityError(
                f'Не удалось получить индекс MuMu из имени {instance.name!r}'
            )
        console = Emulator.single_to_console(instance.emulator.path)
        return [console, 'api', '-v', str(instance_id), action]

    @classmethod
    def _execute_argv(cls, command: list[str], *, timeout: int = 30):
        logger.info(f'[Устройство — Windows] Выполнение команды: {command}')
        try:
            result = subprocess.run(
                command,
                shell=False,
                timeout=timeout,
                close_fds=True,
                capture_output=True,
                text=True,
                errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f'[Устройство — Windows] Истёк тайм-аут команды: {timeout} с')
            return None
        logger.info(f'[Устройство — Windows] Команда завершилась с кодом {result.returncode}')
        output = (result.stdout or '').strip()
        error = (result.stderr or '').strip()
        if output:
            logger.info(f'[Устройство — Windows] Вывод MuMuManager: {output}')
        if error:
            logger.warning(f'[Устройство — Windows] Ошибки MuMuManager: {error}')
        return result

    def _mumu_manager_command(self, instance: EmulatorInstance, action: str):
        try:
            args = self._mumu_manager_args(instance, action)
        except MuMuInstanceIdentityError as exc:
            logger.error(f'[Устройство — Windows] Команда MuMu отклонена: {exc}')
            return None
        return self._execute_argv(args, timeout=30)

    def is_emulator_instance_running(self, instance: EmulatorInstance | None = None) -> bool:
        instance = instance or self.emulator_instance
        if not self._is_modern_mumu(instance):
            raise EmulatorUnknown(
                'Проверка состояния Stage 2 реализована только для семейства MuMuPlayer12'
            )
        return is_mumu_instance_running(instance)

    def emulator_stop(self):
        instance = self.emulator_instance
        if not self._is_modern_mumu(instance):
            return super().emulator_stop()

        logger.hr('Остановка эмулятора', level=1)
        logger.info(
            f'[Устройство — Windows] Запрошена штатная остановка '
            f'{instance.name} (id={instance.MuMuPlayer12_id})'
        )
        result = self._mumu_manager_command(instance, 'shutdown_player')
        if result is None:
            logger.warning('[Устройство — Windows] Команда штатной остановки завершилась тайм-аутом')
        elif result.returncode != 0:
            logger.warning(
                f'[Устройство — Windows] Команда штатной остановки вернула код {result.returncode}; '
                'проверяется фактическое состояние экземпляра'
            )

        try:
            stopped = wait_mumu_instance_stopped(
                instance,
                timeout=self.MUMU_STOP_VERIFY_TIMEOUT,
                interval=self.MUMU_STOP_VERIFY_INTERVAL,
            )
        except MuMuInstanceIdentityError as exc:
            logger.error(f'[Устройство — Windows] Не удалось проверить остановку MuMu: {exc}')
            return False

        if stopped:
            logger.info(f'[Устройство — Windows] Штатная остановка подтверждена: {instance.name}')
            return True

        logger.warning(
            f'[Устройство — Windows] Штатная остановка не остановила {instance.name}; '
            'экземпляр остаётся запущен'
        )
        return False

    def emulator_force_stop_instance(self):
        instance = self.emulator_instance
        if not self._is_modern_mumu(instance):
            logger.error('[Устройство — Windows] Hard kill доступен только для семейства MuMuPlayer12')
            return False
        logger.warning(
            f'[Устройство — Windows] Запущен instance-scoped hard kill '
            f'{instance.name} (id={instance.MuMuPlayer12_id})'
        )
        return force_stop_mumu_instance(instance)

    def emulator_start(self):
        instance = self.emulator_instance
        if not self._is_modern_mumu(instance):
            return super().emulator_start()

        logger.hr('Запуск эмулятора', level=1)
        try:
            running = is_mumu_instance_running(instance)
        except MuMuInstanceIdentityError as exc:
            logger.error(f'[Устройство — Windows] Холодный запуск MuMu отклонён: {exc}')
            return False

        if running:
            logger.warning(
                f'[Устройство — Windows] Холодный запуск отклонён: {instance.name} ещё запущен; '
                'сначала выполняется только штатная остановка'
            )
            if not self.emulator_stop():
                return False

        for attempt in range(1, self.MUMU_START_ATTEMPTS + 1):
            logger.info(
                f'[Устройство — Windows] Холодный запуск {instance.name}: '
                f'попытка {attempt}/{self.MUMU_START_ATTEMPTS}'
            )
            result = self._mumu_manager_command(instance, 'launch_player')
            if result is None or result.returncode != 0:
                logger.warning('[Устройство — Windows] MuMuManager не подтвердил launch_player')
                try:
                    partially_running = is_mumu_instance_running(instance)
                except MuMuInstanceIdentityError as exc:
                    logger.error(
                        f'[Устройство — Windows] Не удалось проверить состояние после launch_player: {exc}'
                    )
                    return False
                if partially_running:
                    logger.warning(
                        f'[Устройство — Windows] После ошибки launch_player экземпляр '
                        f'{instance.name} всё же появился; перед повтором требуется штатная остановка'
                    )
                    if not self.emulator_stop():
                        return False
                continue

            if self.emulator_start_watch():
                logger.info(f'[Устройство — Windows] Проверка загрузки пройдена: {instance.name}')
                return True

            logger.warning(
                f'[Устройство — Windows] Проверка загрузки не пройдена для {instance.name}; '
                'перед повтором выполняется проверяемая штатная остановка'
            )
            if not self.emulator_stop():
                return False

        logger.error(
            f'[Устройство — Windows] Не удалось запустить {instance.name} '
            f'после {self.MUMU_START_ATTEMPTS} попыток'
        )
        return False
