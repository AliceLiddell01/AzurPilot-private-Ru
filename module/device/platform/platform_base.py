"""Базовый класс управления платформой. Определяет абстрактный интерфейс
запуска, остановки и перезапуска эмулятора, управляет конфигурацией EmulatorInfo
и жизненным циклом экземпляра."""

import os
import sys
import typing as t
import subprocess

from pydantic import BaseModel

from module.base.decorator import cached_property, del_cached_property
from module.base.ssh import clear_ssh_host_key
from module.device.connection import Connection
from module.device.method.utils import get_serial_pair
from module.device.platform.emulator_base import EmulatorInstanceBase, EmulatorManagerBase, remove_duplicated_path
from module.logger import logger
from module.map.map_grids import SelectedGrids


class EmulatorInfo(BaseModel):
    """Модель конфигурации информации об эмуляторе."""
    emulator: str = ''
    name: str = ''
    path: str = ''

    # API для облачной платформы телефонов chinac.com
    # access_key: SecretStr = ''
    # secret: SecretStr = ''


def serial_to_id(serial: str):
    """
    Вычисляет идентификатор экземпляра по серийному номеру (serial).
    Например:
        "127.0.0.1:16384" -> 0
        "127.0.0.1:16416" -> 1
        Порты от 16414 до 16418 -> 1

    Returns:
        int: Идентификатор экземпляра или None при неудаче.
    """
    try:
        port = int(serial.split(':')[1])
    except (IndexError, ValueError):
        return None
    index, offset = divmod(port - 16384 + 16, 32)
    offset -= 16
    if 0 <= index < 32 and offset in [-2, -1, 0, 1, 2]:
        return index
    else:
        return None


class PlatformBase(Connection, EmulatorManagerBase):
    """
    Базовый класс платформы. Платформой может быть операционная система или облачный телефон.
    Каждый подкласс `Platform` должен реализовывать следующие API:
    - all_emulators()
    - all_emulator_instances()
    - emulator_start()
    - emulator_stop()
    """

    def __init__(self, config, *, connect: bool = True):
        """
        Args:
            config: Экземпляр AzurLaneConfig или имя конфигурации.
            connect: Устанавливать ли подключение ADB немедленно.
        """
        if connect:
            super().__init__(config)
        else:
            from module.device.connection_attr import ConnectionAttr
            ConnectionAttr.__init__(self, config)

    def emulator_start(self):
        """
        Запускает эмулятор и ожидает завершения запуска.
        - Должен поддерживать повторные попытки.
        - Запрещено использовать простой sleep для ожидания запуска.
        """
        logger.info(f'[Устройство — платформа] Платформа {sys.platform} не поддерживает запуск эмулятора; операция пропущена')

    def emulator_stop(self):
        """
        Останавливает эмулятор.
        """
        logger.info(f'[Устройство — платформа] Платформа {sys.platform} не поддерживает остановку эмулятора; операция пропущена')

    def run_remote_ssh_command(self, command=None):
        """
        Выполняет команду через удалённый SSH.

        Args:
            command: Выполняемая удалённая команда.
        """
        if not getattr(self.config, 'EmulatorInfo_EnableRemoteSSH', False):
            logger.info('[Устройство — SSH] Удалённый SSH отключён (EnableRemoteSSH=False); операция пропущена')
            return

        host = self.config.EmulatorInfo_RemoteSSHHost
        port = self.config.EmulatorInfo_RemoteSSHPort
        user = self.config.EmulatorInfo_RemoteSSHUser
        key = getattr(self.config, 'EmulatorInfo_RemoteSSHPublicKey', '')

        if not command:
            logger.warning('[Устройство — SSH] Команда SSH не задана; операция пропущена')
            return

        if not host:
            logger.warning(f'[Устройство — SSH] RemoteSSHHost пуст; удалённая команда SSH пропущена: {command}')
            return

        logger.hr('Удалённая команда SSH', level=1)
        target = f'{user}@{host}' if user else host
        clear_ssh_host_key(host, port)
        # -n: перенаправляет stdin в /dev/null
        # -T: отключает выделение псевдотерминала
        # BatchMode: не даёт зависнуть на запросе пароля
        cmd = [
            'ssh', '-n', '-T', '-p', str(port),
            '-o', 'StrictHostKeyChecking=no',
            '-o', f'UserKnownHostsFile={os.devnull}',
            '-o', f'GlobalKnownHostsFile={os.devnull}',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        ]

        key_file = None
        if key and len(key) > 50:
            import tempfile
            try:
                fd, key_file = tempfile.mkstemp()
                with os.fdopen(fd, 'w') as f:
                    f.write(key.strip() + '\n')

                if os.name == 'nt':
                    user_env = os.environ.get('USERNAME')
                    subprocess.run(['icacls', key_file, '/reset'], capture_output=True)
                    subprocess.run(['icacls', key_file, '/inheritance:r'], capture_output=True)
                    subprocess.run(['icacls', key_file, '/grant:r', f'{user_env}:F'], capture_output=True)
                else:
                    os.chmod(key_file, 0o600)

                cmd += ['-i', key_file]
                logger.info(f'[Устройство — SSH] Для аутентификации используется указанный приватный ключ')
            except Exception as e:
                logger.error(f'[Устройство — SSH] Не удалось создать или защитить временный файл ключа: {e}')

        cmd += [target, command]
        logger.info(f"[Устройство — SSH] Выполнение удалённой команды: {' '.join(cmd)}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # Кэшируем stderr и показываем его только при ошибке
            stderr_content = []

            import threading

            def collect_stderr():
                for line in process.stderr:
                    stderr_content.append(line.strip())

            def collect_stdout():
                for line in process.stdout:
                    logger.info(f'[Устройство — SSH] Удалённый вывод: {line.strip()}')

            stderr_thread = threading.Thread(target=collect_stderr)
            stdout_thread = threading.Thread(target=collect_stdout)
            stderr_thread.start()
            stdout_thread.start()

            try:
                # Главный поток ожидает завершения процесса
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                logger.error('[Устройство — SSH] Истёк 30-секундный тайм-аут удалённой команды SSH')
            finally:
                stderr_thread.join(timeout=5)
                stdout_thread.join(timeout=5)

            if process.returncode == 0:
                logger.info('[Устройство — SSH] Удалённая команда выполнена успешно')
            else:
                logger.error(f'[Устройство — SSH] Удалённая команда завершилась с кодом {process.returncode}')
                for line in stderr_content:
                    logger.error(f'[Устройство — SSH] Удалённая ошибка: {line}')
        except Exception as e:
            logger.error(f'[Устройство — SSH] Не удалось выполнить удалённую команду SSH: {e}')
        finally:
            if key_file and os.path.exists(key_file):
                try:
                    os.remove(key_file)
                except Exception as e:
                    logger.error(f'[Устройство — SSH] Не удалось удалить временный файл ключа: {e}')

    @cached_property
    def emulator_info(self) -> EmulatorInfo:
        """
        Разбирает информацию об эмуляторе из конфигурации.

        Returns:
            EmulatorInfo: Информация об эмуляторе.
        """
        emulator = self.config.EmulatorInfo_Emulator
        if emulator == 'auto':
            emulator = ''

        def parse_info(value):
            if isinstance(value, str):
                value = value.strip().replace('\n', '')
                if value in ['None', 'False', 'True']:
                    value = ''
                return value
            else:
                return ''

        name = parse_info(self.config.EmulatorInfo_name)
        path = parse_info(self.config.EmulatorInfo_path)

        return EmulatorInfo(
            emulator=emulator,
            name=name,
            path=path,
        )

    @cached_property
    def emulator_instance(self) -> t.Optional[EmulatorInstanceBase]:
        """
        Находит и возвращает экземпляр эмулятора для текущей конфигурации.

        Returns:
            EmulatorInstanceBase: Экземпляр эмулятора или None, если не найден.
        """
        data = self.emulator_info
        old_info = dict(
            emulator=data.emulator,
            path=data.path,
            name=data.name,
        )
        # Перенаправляем emulator-5554 на 127.0.0.1:5555
        serial = self.serial
        port_serial, _ = get_serial_pair(self.serial)
        if port_serial is not None:
            serial = port_serial

        instance = self.find_emulator_instance(
            serial=serial,
            name=data.name,
            path=data.path,
            emulator=data.emulator,
        )

        # Записываем полные данные эмулятора
        if instance is not None:
            new_info = dict(
                emulator=instance.type,
                path=instance.path,
                name=instance.name,
            )
            if new_info != old_info:
                with self.config.multi_set():
                    self.config.EmulatorInfo_Emulator = instance.type
                    self.config.EmulatorInfo_name = instance.name
                    self.config.EmulatorInfo_path = instance.path
                del_cached_property(self, 'emulator_info')

        return instance

    def find_emulator_instance(
            self,
            serial: str,
            name: str = None,
            path: str = None,
            emulator: str = None
    ) -> t.Optional[EmulatorInstanceBase]:
        """
        Находит экземпляр эмулятора по серийному номеру, имени, пути и типу.

        Args:
            serial: Серийный номер, например "127.0.0.1:5555".
            name: Имя экземпляра, например "Nougat64".
            path: Путь установки эмулятора, например "C:/Program Files/BlueStacks_nxt/HD-Player.exe".
            emulator: Тип эмулятора, определённый в классе Emulator, например "BlueStacks5".

        Returns:
            EmulatorInstanceBase: Экземпляр эмулятора или None, если не найден.
        """
        logger.hr('Поиск экземпляра эмулятора', level=2)
        if emulator == 'SSH':
            instance = EmulatorInstanceBase(
                serial=serial,
                name=name or '',
                path=path or '',
            )
            # Для SSH-экземпляра временно изменяем атрибут type
            instance.__dict__['type'] = 'SSH'
            logger.hr('Экземпляр эмулятора', level=2)
            logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора через SSH: {instance}')
            return instance

        instances = SelectedGrids(self.all_emulator_instances)
        for instance in instances:
            logger.debug('[Устройство — платформа] Кандидат экземпляра эмулятора: %s', instance)
        search_args = dict(serial=serial)

        # Ищем по серийному номеру
        select = instances.select(**search_args)
        if select.count == 0:
            logger.warning(f'[Устройство — платформа] Экземпляр эмулятора {search_args} не найден: недопустимый serial')

            # Исправление дрейфа serial MuMu12, перенесённое сюда из недостижимого кода
            # Во время работы MuMu12 serial равен 127.0.0.1:16384, а после остановки в конфигурации .nemu он может стать 127.0.0.1:7555
            # В этом случае вычисляем instance_id по serial и сопоставляем экземпляр по id
            instance_id = serial_to_id(serial)
            if instance_id is not None:
                select_by_id = instances.select(MuMuPlayer12_id=instance_id)
                if select_by_id.count >= 1:
                    instance = select_by_id[0]
                    logger.hr('Экземпляр эмулятора', level=2)
                    logger.info(f'[Устройство — эмулятор] Найден экземпляр эмулятора по ID MuMu12: настроенный serial {serial} → экземпляр {instance}: {instance}')
                    # Обновляем serial экземпляра значением из конфигурации, чтобы последующие команды запуска/остановки использовали правильный порт
                    instance.serial = serial
                    return instance

            # Fallback: если экземпляр отсутствует в списке обнаруженных, пытаемся построить его напрямую из известных данных конфигурации
            # Типичный случай: после перезагрузки компьютера процесс MuMu12 не запущен, а записи реестра/MuiCache могли быть очищены,
            # поэтому all_emulator_instances не содержит экземпляр MuMu12.
            # Но в конфигурации уже сохранены EmulatorInfo_Emulator/name/path от прошлого успешного запуска,
            # поэтому по этим данным можно напрямую создать экземпляр и запустить эмулятор.
            if emulator and path and name and serial:
                logger.info(f'[Устройство — эмулятор] Создание резервного экземпляра из конфигурации: emulator={emulator}, name={name}, path={path}, serial={serial}')
                if os.path.exists(path):
                    fallback = EmulatorInstanceBase(
                        serial=serial,
                        name=name,
                        path=path,
                    )
                    # Проверяем, совпадает ли тип созданного экземпляра с конфигурацией
                    # Важно: type базового EmulatorInstanceBase зависит от EmulatorBase,
                    # который может не распознать конкретный тип и вернуть пустую строку, поэтому пустой тип допускается
                    fallback_type = fallback.type
                    if fallback_type == emulator or not fallback_type:
                        # Если тип совпадает или базовый класс не смог его определить, доверяем типу из конфигурации
                        fallback.__dict__['type'] = emulator
                        logger.hr('Экземпляр эмулятора', level=2)
                        logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора по резервной конфигурации: {fallback}')
                        return fallback
                    else:
                        logger.warning(f'[Устройство — эмулятор] Тип резервного экземпляра не совпадает: ожидался {emulator}, получен {fallback_type}')
                else:
                    logger.warning(f'[Устройство — платформа] Резервный путь не существует: {path}')

            return None
        if select.count == 1:
            instance = select[0]
            logger.hr('Экземпляр эмулятора', level=2)
            logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора: {instance}')
            return instance

        # Среди нескольких экземпляров с одинаковым serial сначала ищем по типу эмулятора — это самый простой и надёжный пользовательский параметр
        if emulator:
            search_args['type'] = emulator
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'[Устройство — платформа] Экземпляр эмулятора {search_args} не найден: недопустимый тип')
                search_args.pop('type')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Экземпляр эмулятора', level=2)
                logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора: {instance}')
                return instance

        # Среди нескольких экземпляров с одинаковым serial ищем по имени
        if name:
            search_args['name'] = name
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'[Устройство — платформа] Экземпляр эмулятора {search_args} не найден: недопустимое имя')
                search_args.pop('name')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Экземпляр эмулятора', level=2)
                logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора: {instance}')
                return instance

        # Среди нескольких экземпляров с одинаковыми serial и именем ищем по пути
        if path:
            search_args['path'] = path
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'[Устройство — платформа] Экземпляр эмулятора {search_args} не найден: недопустимый путь')
                search_args.pop('path')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Экземпляр эмулятора', level=2)
                logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора: {instance}')
                return instance

        # Если экземпляров всё ещё несколько, ищем среди запущенных эмуляторов
        running = remove_duplicated_path(list(self.iter_running_emulator()))
        logger.info('[Устройство — платформа] Запущенные эмуляторы')
        for exe in running:
            logger.info(exe)
        if len(running) == 1:
            logger.info('[Устройство — платформа] Запущен только один эмулятор')
            # Эквивалент поиска по пути
            search_args['path'] = running[0]
            select = instances.select(**search_args)
            if select.count == 0:
                logger.warning(f'[Устройство — платформа] Экземпляр эмулятора {search_args} не найден: недопустимый путь')
                search_args.pop('path')
            elif select.count == 1:
                instance = select[0]
                logger.hr('Экземпляр эмулятора', level=2)
                logger.info(f'[Устройство — платформа] Найден экземпляр эмулятора: {instance}')
                return instance

        # Экземпляров всё ещё несколько
        logger.warning(f'[Устройство — платформа] Найдено несколько экземпляров эмулятора: {search_args}')
        return None
