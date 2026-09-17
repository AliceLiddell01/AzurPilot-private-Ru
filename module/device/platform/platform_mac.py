"""Управление эмуляторами на macOS. Наследует PlatformBase и EmulatorManagerMac,
реализуя запуск, остановку и управление процессами эмуляторов на macOS."""

from __future__ import annotations
import os
import re
import subprocess
import time

import psutil

from module.base.decorator import run_once
from module.base.timer import Timer
from module.device.platform.platform_base import PlatformBase
from module.device.platform.emulator_mac import (
    EmulatorMac,
    EmulatorInstanceMac,
    EmulatorManagerMac,
)
from module.logger import logger


class PlatformMac(PlatformBase, EmulatorManagerMac):
    """
    Интерфейс управления эмуляторами на macOS.
    Поддерживает BlueStacks Air и MuMu Pro.
    """

    @classmethod
    def execute(cls, command, wait=True):
        """
        Выполняет внешнюю команду.

        Args:
            command (str): Выполняемая команда.
            wait (bool): Ожидать ли завершения команды.

        Returns:
            subprocess.CompletedProcess или subprocess.Popen: Результат выполнения команды.
        """
        # На Mac используем shell=True для выполнения сложных команд
        logger.info(f'[Устройство — эмулятор macOS] Выполнение команды: {command}')
        if wait:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True
            )
            return result
        else:
            return subprocess.Popen(command, shell=True)

    @classmethod
    def kill_process_by_regex(cls, regex: str) -> int:
        """
        Завершает процесс, имя которого совпадает с регулярным выражением.

        Args:
            regex: Регулярное выражение для поиска имени процесса.

        Returns:
            int: Количество завершённых процессов.
        """
        count = 0
        for proc in psutil.process_iter():
            try:
                name = proc.name()
                if re.search(regex, name, re.IGNORECASE):
                    logger.info(f'[Устройство — эмулятор macOS] Завершение процесса эмулятора: {name}')
                    proc.kill()
                    count += 1
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return count

    @classmethod
    def renice_process_by_regex(cls, regex: str, priority: int = -20) -> int:
        """
        Изменяет приоритет процесса, имя которого совпадает с регулярным выражением.

        Args:
            regex: Регулярное выражение для поиска имени процесса.
            priority: Значение nice (-20 наивысший приоритет, 19 наинизший).

        Returns:
            int: Количество процессов с изменённым приоритетом.
        """
        count = 0
        for proc in psutil.process_iter():
            try:
                name = proc.name()
                if re.search(regex, name, re.IGNORECASE):
                    pid = proc.pid
                    # Используем sudo renice для установки приоритета (требуется пароль администратора)
                    result = subprocess.run(
                        f'sudo -n renice -n {priority} -p {pid}',
                        shell=True,
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0:
                        logger.info(f'[Устройство — эмулятор macOS] Приоритет процесса {name} (PID: {pid}) изменён на {priority}')
                        count += 1
                    else:
                        logger.warning(f'[Устройство — эмулятор macOS] Не удалось изменить приоритет {name}: {result.stderr.strip()}')
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return count

    def boost_emulator_priority(self, instance: EmulatorInstanceMac):
        """
        Повышает приоритет процесса эмулятора после запуска.

        Args:
            instance: Экземпляр эмулятора для повышения приоритета.
        """
        if instance == EmulatorMac.BlueStacksAir:
            time.sleep(3)
            self.renice_process_by_regex(r'BlueStacks', -20)

        elif instance == EmulatorMac.MuMuPro:
            time.sleep(3)
            self.renice_process_by_regex(r'MuMuEmulator|MuMuPlayer', -20)

        else:
            if instance.name:
                time.sleep(3)
                self.renice_process_by_regex(instance.name, -20)

    def boost_running_emulator_priority(self):
        """
        Повышает приоритет процесса текущего запущенного эмулятора.
        Вызывается при старте Alas, если обнаружен уже запущенный эмулятор.
        """
        # Пытаемся повысить приоритет процессов MuMu
        count = self.renice_process_by_regex(r'MuMuEmulator|MuMuPlayer', -20)
        if count > 0:
            logger.info(f'[Устройство — эмулятор macOS] Повышен приоритет процессов MuMu: {count}')
            return

        # Пытаемся повысить приоритет процессов BlueStacks
        count = self.renice_process_by_regex(r'BlueStacks', -20)
        if count > 0:
            logger.info(f'[Устройство — эмулятор macOS] Повышен приоритет процессов BlueStacks: {count}')
            return

        logger.info('[Устройство — эмулятор macOS] Запущенные процессы эмулятора для повышения приоритета не найдены')

    def _emulator_start(self, instance: EmulatorInstanceMac):
        """
        Запускает эмулятор (без обработки ошибок).

        Args:
            instance: Экземпляр эмулятора.
        """
        exe: str = instance.emulator.path

        if instance == EmulatorMac.BlueStacksAir:
            # Запускаем приложение BlueStacks Air командой open
            # Сначала ищем пакет приложения
            app_path = EmulatorMac.find_app_bundle('BlueStacks')
            if app_path:
                self.execute(f'open -a "{app_path}"', wait=False)
            else:
                raise Exception('[Устройство — эмулятор] Приложение BlueStacks Air не найдено')

        elif instance == EmulatorMac.MuMuPro:
            # Корректная последовательность запуска MuMu на macOS:
            # 1. open -a MuMuPlayer.app — запускаем основную программу
            # 2. mumutool open <index> — запускаем экземпляр эмулятора
            app_path = EmulatorMac.find_app_bundle('MuMu')
            if app_path:
                # Шаг 1: запускаем основную программу MuMuPlayer
                self.execute(f'open -a "{app_path}"', wait=False)
                time.sleep(3)
                # Шаг 2: запускаем нужный экземпляр эмулятора через mumutool
                mumu_bin_path = os.path.join(app_path, 'Contents/MacOS/mumutool')
                if os.path.exists(mumu_bin_path):
                    # Используем instance.index для открытия нужного экземпляра
                    instance_index = getattr(instance, 'index', 0)
                    self.execute(f'"{mumu_bin_path}" open {instance_index}', wait=False)
                else:
                    logger.warning(f'[Устройство — эмулятор macOS] mumutool не найден по пути {mumu_bin_path}; используется резервный способ')
                    # Резервный вариант: пробуем структуру MuMuEmulator.app
                    mumu_emulator_app = os.path.join(app_path, 'Contents/MacOS/MuMuEmulator.app')
                    if os.path.exists(mumu_emulator_app):
                        self.execute(f'open "{mumu_emulator_app}"', wait=False)
            else:
                raise Exception('[Устройство — эмулятор] Приложение MuMu Pro не найдено')

        else:
            # Общий резервный вариант: пытаемся открыть по пути
            if os.path.exists(exe):
                self.execute(f'open "{exe}"', wait=False)
            else:
                raise Exception(f'[Устройство — эмулятор] Невозможно запустить неизвестный эмулятор: {instance}')

    def _emulator_stop(self, instance: EmulatorInstanceMac):
        """
        Останавливает эмулятор (без обработки ошибок).

        Args:
            instance: Экземпляр эмулятора.
        """
        if instance == EmulatorMac.BlueStacksAir:
            # Пытаемся найти и завершить процессы BlueStacks
            killed = self.kill_process_by_regex(r'BlueStacks')
            if killed == 0:
                # Резервный вариант: завершаем приложение через osascript
                self.execute('osascript -e \'tell application "BlueStacks" to quit\'', wait=True)

        elif instance == EmulatorMac.MuMuPro:
            # Закрываем указанный экземпляр через mumutool
            app_path = EmulatorMac.find_app_bundle('MuMu')
            if app_path:
                mumu_bin_path = os.path.join(app_path, 'Contents/MacOS/mumutool')
                if os.path.exists(mumu_bin_path):
                    # Используем instance.index для закрытия нужного экземпляра
                    instance_index = getattr(instance, 'index', 0)
                    self.execute(f'"{mumu_bin_path}" close {instance_index}', wait=True)
                    time.sleep(2)

            # Важно: не используем osascript для выхода, поскольку он закроет все экземпляры
            # Вместо этого убеждаемся, что нужный процесс остановлен
            # Завершаем только конкретный процесс эмулятора MuMu, если он всё ещё работает
            # Имя экземпляра можно использовать для поиска его процесса

        else:
            # Общий резервный вариант: завершаем процесс по имени экземпляра
            if instance.name:
                self.kill_process_by_regex(instance.name)

    def _emulator_function_wrapper(self, func):
        """
        Унифицированная обёртка операций запуска и остановки эмулятора с обработкой исключений.

        Args:
            func (callable): _emulator_start или _emulator_stop.

        Returns:
            bool: Успешна ли операция.
        """
        try:
            func(self.emulator_instance)
            return True
        except Exception as e:
            logger.exception(str(f'[Устройство — платформа macOS] Ошибка операции эмулятора: {e}'))

        logger.error(f'[Устройство — эмулятор macOS] Не удалось выполнить функцию эмулятора {func.__name__}()')
        return False

    def emulator_start_watch(self):
        """
        Отслеживает процесс запуска эмулятора, ожидая его завершения.

        Returns:
            bool: True при успешном завершении, False при истечении тайм-аута.
        """
        logger.hr('[Устройство — эмулятор macOS] Запуск эмулятора', level=2)
        serial = self.emulator_instance.serial

        @run_once
        def show_online(m):
            logger.info(f'[Устройство — эмулятор macOS] Эмулятор доступен: {m}')

        @run_once
        def show_ping(m):
            logger.info(f'[Устройство — эмулятор macOS] Команда ping: {m}')

        @run_once
        def show_package(m):
            logger.info(f'[Устройство — эмулятор macOS] Найден пакет Azur Lane: {m}')

        interval = Timer(0.5).start()
        timeout = Timer(180).start()

        while 1:
            interval.wait()
            interval.reset()
            if timeout.reached():
                logger.warning('[Устройство — эмулятор macOS] Истёк тайм-аут запуска эмулятора')
                return False

            try:
                # Проверяем подключение устройства
                devices = self.list_device().select(serial=serial)
                if devices:
                    device = devices.first_or_none()
                    if device.status == 'device':
                        pass
                    if device.status == 'offline':
                        self.adb_client.disconnect(serial)
                        self.adb_client.connect(serial)
                        continue
                else:
                    # Пытаемся подключиться
                    self.adb_client.connect(serial)
                    continue
                show_online(devices.first_or_none())

                # Проверяем доступность команд
                try:
                    pong = self.adb_shell(['echo', 'pong'])
                except Exception as e:
                    logger.info(str(f'[Устройство — платформа macOS] Ошибка ожидания запуска эмулятора: {e}'))
                    continue
                show_ping(pong)

                # Проверяем имя пакета Azur Lane
                packages = self.list_known_packages(show_log=False)
                if len(packages):
                    pass
                else:
                    continue
                show_package(packages)

                # Все проверки пройдены
                break
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
                logger.info(str(f'[Устройство — платформа macOS] Ошибка ожидания запуска эмулятора: {e}'))
                continue
            except Exception as e:
                logger.exception(str(f'[Устройство — платформа macOS] Ошибка ожидания запуска эмулятора: {e}'))
                continue

        logger.info('[Устройство — эмулятор macOS] Запуск эмулятора завершён')
        return True

    def emulator_start(self):
        """Запускает эмулятор с максимумом 3 повторными попытками."""
        logger.hr('[Устройство — эмулятор macOS] Запуск эмулятора', level=1)
        self.run_remote_ssh_command()
        for _ in range(3):
            # Сначала останавливаем
            if not self._emulator_function_wrapper(self._emulator_stop):
                return False
            # Затем запускаем
            if self._emulator_function_wrapper(self._emulator_start):
                # Успешно
                # Повышаем приоритет процесса эмулятора
                self.boost_emulator_priority(self.emulator_instance)
                if self.emulator_start_watch():
                    return True
                logger.warning('[Устройство — эмулятор macOS] Ошибка наблюдения за запуском эмулятора; повторная попытка')
                if self._emulator_function_wrapper(self._emulator_stop):
                    continue
                else:
                    return False
            else:
                # Запуск не удался: останавливаем и пробуем снова
                if self._emulator_function_wrapper(self._emulator_stop):
                    continue
                else:
                    return False

        logger.error('[Устройство — эмулятор macOS] Не удалось запустить эмулятор после 3 попыток; дальнейшие попытки прекращены')
        return False

    def emulator_stop(self):
        """Останавливает эмулятор с максимумом 3 повторными попытками."""
        logger.hr('[Устройство — эмулятор macOS] Остановка эмулятора', level=1)
        for _ in range(3):
            # Останавливаем
            if self._emulator_function_wrapper(self._emulator_stop):
                # Успешно
                return True
            else:
                # Остановка не удалась: запускаем и пробуем снова
                if self._emulator_function_wrapper(self._emulator_start):
                    continue
                else:
                    return False

        logger.error('[Устройство — эмулятор macOS] Не удалось остановить эмулятор после 3 попыток; дальнейшие попытки прекращены')
        return False


if __name__ == '__main__':
    from module.config import AzurLaneConfig
    config = AzurLaneConfig(config_name='alas')
    self = PlatformMac(config)
    d = self.emulator_instance
    print(d)
