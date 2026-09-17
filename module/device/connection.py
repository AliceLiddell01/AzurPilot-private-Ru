"""Уровень управления ADB-подключением. Обертка над adbutils для подключения устройств,
проброса портов, выполнения shell-команд, обработки повторов, восстановления после ошибок и управления serial."""

import ipaddress
import json
import re
import socket
import subprocess
import time
from functools import wraps

import uiautomator2 as u2
from adbutils import AdbClient, AdbDevice, AdbTimeout, ForwardItem, ReverseItem
from adbutils.errors import AdbError

from module.base.decorator import Config, cached_property, del_cached_property, has_cached_property, run_once
from module.base.timer import Timer
from module.base.utils import ensure_time
from module.config.deep import deep_get
from module.config.server import VALID_CHANNEL_PACKAGE, VALID_PACKAGE, set_server
from module.device.connection_attr import ConnectionAttr
from module.device.env import IS_LINUX, IS_MACINTOSH, IS_WINDOWS
from module.device.method.pool import WORKER_POOL
from module.device.method.remove_warning import remove_shell_warning
from module.device.method.utils import (PackageNotInstalled, RETRY_TRIES, get_serial_pair, handle_adb_error,
                                        handle_unknown_host_service, possible_reasons, random_port, recv_all,
                                        retry_sleep)
from module.exception import EmulatorNotRunningError, RequestHumanTakeover
from module.logger import logger
from module.map.map_grids import SelectedGrids


def retry(func):
    """Декоратор с автоматическими повторными попытками для обработки исключений ADB-подключения и устройства.

    Выполняет до RETRY_TRIES повторов для указанной функции, применяя различные стратегии
    восстановления в зависимости от типа исключения (переподключение к ADB, перезапуск службы, обнаружение пакета и т. д.).
    """
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Adb): Экземпляр устройства ADB.
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Необрабатываемое исключение: прерываем повторные попытки
            except RequestHumanTakeover:
                break
            # Невозможно обработать: передаем наверх для перезапуска эмулятора
            except EmulatorNotRunningError:
                raise
            # Срабатывает при завершении службы ADB
            except ConnectionResetError as e:
                logger.error(str(f'[Устройство — соединение] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
            # Ошибка ADB
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                elif handle_unknown_host_service(e):
                    def init():
                        self.adb_start_server()
                        self.adb_reconnect()
                else:
                    break
            # Пакет не установлен
            except PackageNotInstalled as e:
                logger.error(str(f'[Устройство — соединение] Ошибка повторной попытки: {e}'))

                def init():
                    self.detect_package()
            # Неизвестное исключение, возможно, поврежденные данные изображения
            except Exception as e:
                logger.exception(str(f'[Устройство — соединение] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in [
            'adb_connect', 'adb_reconnect', 'adb_start_server',
            'screenshot', 'screenshot_adb', 'screenshot_uiautomator2', 'screenshot_ascreencap',
            'screenshot_droidcast', 'screenshot_droidcast_raw', 'screenshot_scrcpy',
            'screenshot_nemu_ipc', 'screenshot_ldopengl',
        ]:
            logger.critical(f'[Устройство] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError
        logger.critical(f'[Устройство] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class AdbDeviceWithStatus(AdbDevice):
    def __init__(self, client: AdbClient, serial: str, status: str):
        self.status = status
        super().__init__(client, serial)

    def __str__(self):
        return f'AdbDevice({self.serial}, {self.status})'

    __repr__ = __str__

    def __bool__(self):
        return True

    @cached_property
    def port(self) -> int:
        try:
            return int(self.serial.split(':')[1])
        except (IndexError, ValueError):
            return 0

    @cached_property
    def may_mumu12_family(self):
        # 127.0.0.1:16XXX
        return 16384 <= self.port <= 17408


class Connection(ConnectionAttr):
    def __init__(self, config):
        """
        Args:
            config (AzurLaneConfig, str): Имя пользовательской конфигурации в каталоге ./config.
        """
        super().__init__(config)
        if not self.is_over_http:
            self.detect_device()

        # Подключение к устройству
        self.adb_connect(wait_device=False)
        logger.attr('Устройство ADB', self.adb)

        # Определение имени пакета
        self.package = self.config.Emulator_PackageName
        if self.package == 'auto':
            self.detect_package()
        else:
            set_server(self.package)
        logger.attr('Пакет приложения', self.package)
        logger.attr('Сервер', self.config.SERVER)

        self.check_mumu_app_keep_alive()

    @Config.when(DEVICE_OVER_HTTP=False)
    def adb_command(self, cmd, timeout=10):
        """Выполнить команду ADB в подпроцессе, обычно для отправки или получения больших файлов.

        Args:
            cmd (list): Список аргументов команды ADB.
            timeout (int): Время ожидания в секундах.

        Returns:
            str: Стандартный вывод команды.
        """
        cmd = list(map(str, cmd))
        cmd = [self.adb_binary, '-s', self.serial] + cmd
        return self.subprocess_run(cmd, timeout=timeout)

    def subprocess_run(self, cmd, timeout=10):
        """Запустить команду в подпроцессе и вернуть стандартный вывод.

        Args:
            cmd (list): Список аргументов команды.
            timeout (int): Время ожидания в секундах.

        Returns:
            str: Стандартный вывод команды.
        """
        logger.info(f'[Устройство — соединение] Выполнение команды: {cmd}')
        # Больше не используем gooey, напрямую shell=False
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=False)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            logger.warning(f'[Устройство — соединение] Истёк тайм-аут команды {cmd}, stdout={stdout}, stderr={stderr}')
        return stdout

    @Config.when(DEVICE_OVER_HTTP=True)
    def adb_command(self, cmd, timeout=10):
        logger.critical(
            f'[Устройство — соединение] Невозможно выполнить {cmd}: adb_command() недоступен при HTTP-подключении к {self.serial}, '
        )
        raise RequestHumanTakeover

    def adb_start_server(self):
        """Запустить службу ADB.

        Использует `adb devices` вместо `adb start-server`, запуская ADB через подпроцесс
        для завершения других существующих процессов ADB; возвращаемое значение практически не используется.
        """
        stdout = self.subprocess_run([self.adb_binary, 'devices'])
        logger.info(stdout)
        return stdout

    @Config.when(DEVICE_OVER_HTTP=False)
    def adb_shell(self, cmd, stream=False, recvall=True, timeout=10, rstrip=True):
        """Выполнить команду ADB shell, эквивалентно `adb -s <serial> shell <*cmd>`.

        Args:
            cmd (list, str): Команда shell или список её аргументов.
            stream (bool): Если True, возвращает объект потока вместо строки. По умолчанию False.
            recvall (bool): При stream=True определять, считывать ли все данные целиком. По умолчанию True.
            timeout (int): Время ожидания в секундах. По умолчанию 10.
            rstrip (bool): Удалять ли завершающие пустые строки. По умолчанию True.

        Returns:
            При stream=False возвращает str.
            При stream=True и recvall=True возвращает bytes.
            При stream=True и recvall=False возвращает socket.
        """
        if not isinstance(cmd, str):
            cmd = list(map(str, cmd))

        if stream:
            result = self.adb.shell(cmd, stream=stream, timeout=timeout, rstrip=rstrip)
            if recvall:
                try:
                    # Возвращает bytes
                    return recv_all(result)
                finally:
                    try:
                        if hasattr(result, 'close'):
                            result.close()
                        elif hasattr(result, 'conn') and hasattr(result.conn, 'close'):
                            result.conn.close()
                    except Exception:
                        pass
            else:
                # Возвращает socket
                return result
        else:
            result = self.adb.shell(cmd, stream=stream, timeout=timeout, rstrip=rstrip)
            result = remove_shell_warning(result)
            # Возвращает str
            return result

    @Config.when(DEVICE_OVER_HTTP=True)
    def adb_shell(self, cmd, stream=False, recvall=True, timeout=10, rstrip=True):
        """Выполнить shell-команду через HTTP, эквивалентно http://127.0.0.1:7912/shell?command={command}.

        Args:
            cmd (list, str): Команда shell или список её аргументов.
            stream (bool): Если True, возвращает поток данных вместо строки. По умолчанию False.
            recvall (bool): При stream=True определять, считывать ли все данные целиком. По умолчанию True.
            timeout (int): Время ожидания в секундах. По умолчанию 10.
            rstrip (bool): Удалять ли завершающие пустые строки. По умолчанию True.

        Returns:
            При stream=False возвращает str.
            При stream=True возвращает bytes.
        """
        if not isinstance(cmd, str):
            cmd = list(map(str, cmd))

        if stream:
            result = self.u2.shell(cmd, stream=stream, timeout=timeout)
            # Все данные получены, параметр `recvall` игнорируется
            result = remove_shell_warning(result.content)
            # Возвращает bytes
            return result
        else:
            result = self.u2.shell(cmd, stream=stream, timeout=timeout).output
            if rstrip:
                result = result.rstrip()
            result = remove_shell_warning(result)
            # Возвращает str
            return result

    def adb_getprop(self, name):
        """Получить системное свойство Android, эквивалентно `getprop <name>`.

        Args:
            name (str): Имя свойства.

        Returns:
            str: Значение свойства.
        """
        return self.adb_shell(['getprop', name]).strip()

    @cached_property
    @retry
    def cpu_abi(self) -> str:
        """Получить тип ABI процессора устройства.

        Returns:
            str: ABI процессора, например arm64-v8a, armeabi-v7a, x86, x86_64.
        """
        abi = self.adb_getprop('ro.product.cpu.abi')
        if not len(abi):
            logger.error(f'[Устройство — соединение] Недопустимое значение CPU ABI: "{abi}"')
        return abi

    @cached_property
    @retry
    def sdk_ver(self) -> int:
        """Получить номер версии Android SDK/API, подробнее: https://apilevels.com/."""
        sdk = self.adb_getprop('ro.build.version.sdk')
        try:
            return int(sdk)
        except ValueError:
            logger.error(f'[Устройство — соединение] Недопустимая версия SDK: {sdk}')

        return 0

    @cached_property
    @retry
    def is_avd(self):
        if get_serial_pair(self.serial)[0] is None:
            return False
        if 'ranchu' in self.adb_getprop('ro.hardware'):
            return True
        if 'goldfish' in self.adb_getprop('ro.hardware.audio.primary'):
            return True
        return False

    @cached_property
    @retry
    def is_waydroid(self):
        res = self.adb_getprop('ro.product.brand')
        logger.attr('Марка устройства', res)
        return 'waydroid' in res.lower()

    @cached_property
    @retry
    def is_bluestacks_air(self):
        # BlueStacks Air — версия BlueStacks для Mac
        if not IS_MACINTOSH:
            return False
        # 127.0.0.1:5555 + 10*n, предполагается не более 32 экземпляров
        if not (5555 <= self.port <= 5875):
            return False
        # [bst.installed_images]: [Tiramisu64]
        # [bst.instance]: [Tiramisu64]
        # Tiramisu64 — Android 13; BlueStacks Air — единственная версия BlueStacks на Android 13
        res = self.adb_getprop('bst.installed_images')
        logger.attr('Образ BlueStacks', res)
        if 'Tiramisu64' in res:
            return True
        return False

    @cached_property
    @retry
    def is_mumu_pro(self):
        # MuMu Pro — версия MuMu для Mac
        if not IS_MACINTOSH:
            return False
        if not self.is_mumu_family:
            return False
        logger.attr('MuMu Pro', True)
        return True

    @cached_property
    @retry
    def nemud_app_keep_alive(self) -> str:
        res = self.adb_getprop('nemud.app_keep_alive')
        logger.attr('Фоновая работа MuMu', res)
        return res

    @cached_property
    @retry
    def nemud_player_version(self) -> str:
        # [nemud.player_product_version]: [3.8.27.2950], номер версии эмулятора MuMu
        res = self.adb_getprop('nemud.player_version')
        logger.attr('Версия MuMu Player', res)
        return res

    @cached_property
    @retry
    def nemud_player_engine(self) -> str:
        # Тип движка эмулятора MuMu: NEMUX или MACPRO
        res = self.adb_getprop('nemud.player_engine')
        logger.attr('Движок MuMu Player', res)
        return res

    def check_mumu_app_keep_alive(self):
        if not self.is_mumu_family:
            return False

        res = self.nemud_app_keep_alive
        if res == '':
            # Свойство пустое, возможно MuMu6 или MuMu12 версии < 3.5.6
            return True
        elif res == 'false':
            # Отключено
            return True
        elif res == 'true':
            # https://mumu.163.com/help/20230802/35047_1102450.html
            logger.critical('[Устройство] Отключите «Сохранять работу в фоне» в настройках эмулятора MuMu')
            raise RequestHumanTakeover
        else:
            logger.warning(f'[Устройство — соединение] Недопустимое значение фоновой работы MuMu: {res}')
            return False

    @cached_property
    def is_mumu_over_version_400(self) -> bool:
        if not self.is_mumu_family:
            return False
        # Версии >= 4.0 не содержат сведений о версии в getprop
        if self.nemud_player_version == '':
            return True
        return False

    @cached_property
    def is_mumu_over_version_356(self) -> bool:
        """Определить, является ли версия MuMu12 >= 3.5.6.

        В этой версии есть свойство nemud.app_keep_alive, и устройство всегда находится в портретной ориентации.
        Аналогичной особенностью обладает MuMu PRO на macOS.

        Returns:
            bool: Является ли эмулятор MuMu12 версии >= 3.5.6.
        """
        if not self.is_mumu_family:
            return False
        if self.is_mumu_over_version_400:
            return True
        if self.nemud_app_keep_alive != '':
            return True
        if IS_MACINTOSH:
            if 'MACPRO' in self.nemud_player_engine:
                return True
        return False

    @cached_property
    def _nc_server_host_port(self):
        """Получить информацию о хосте и портах прослушивания/подключения сервера netcat.

        Returns:
            tuple: (server_listen_host, server_listen_port, client_connect_host, client_connect_port)
        """
        # BlueStacks Hyper-V использует ADB reverse
        if self.is_bluestacks_hyperv:
            host = '127.0.0.1'
            logger.info(f'[Устройство — соединение] Подключение к BlueStacks Hyper-V через хост {host}')
            port = self.adb_reverse(f'tcp:{self.config.REVERSE_SERVER_PORT}')
            return host, port, host, self.config.REVERSE_SERVER_PORT
        # Эмулятор слушает хост
        if self.is_emulator or self.is_over_http:
            # Эмулятор Mac
            if self.is_bluestacks_air or self.is_mumu_pro:
                logger.info(f'[Устройство — соединение] Подключение к локальному эмулятору через хост 127.0.0.1')
                port = random_port(self.config.FORWARD_PORT_RANGE)
                return '127.0.0.1', port, "10.0.2.2", port
            # Получение IP-адреса хоста
            try:
                host = socket.gethostbyname(socket.gethostname())
            except socket.gaierror as e:
                logger.error(str(f'[Устройство — соединение] Ошибка определения адреса nc-сервера: {e}'))
                logger.error(f'[Устройство — соединение] Неизвестное имя хоста: {socket.gethostname()}')
                host = '127.0.0.1'
            # Исправление адреса хоста Linux AVD
            if IS_LINUX and host == '127.0.1.1':
                host = '127.0.0.1'
            logger.info(f'[Устройство — соединение] Подключение к локальному эмулятору через хост {host}')
            port = random_port(self.config.FORWARD_PORT_RANGE)
            # Экземпляр AVD использует 10.0.2.2 как адрес клиента
            if self.is_avd:
                return host, port, "10.0.2.2", port
            return host, port, host, port
        # Устройство в LAN: слушаем хост в той же подсети, что и целевое устройство
        if self.is_network_device:
            hosts = socket.gethostbyname_ex(socket.gethostname())[2]
            logger.info(f'[Устройство — соединение] Текущие хосты: {hosts}')
            ip = ipaddress.ip_address(self.serial.split(':')[0])
            for host in hosts:
                if ip in ipaddress.ip_interface(f'{host}/24').network:
                    logger.info(f'[Устройство — соединение] Подключение к устройству в локальной сети через хост {host}')
                    port = random_port(self.config.FORWARD_PORT_RANGE)
                    return host, port, host, port
        # Другие устройства: создаем ADB reverse и слушаем 127.0.0.1
        host = '127.0.0.1'
        logger.info(f'[Устройство — соединение] Подключение к неизвестному устройству через хост {host}')
        port = self.adb_reverse(f'tcp:{self.config.REVERSE_SERVER_PORT}')
        return host, port, host, self.config.REVERSE_SERVER_PORT

    @cached_property
    def reverse_server(self):
        """Создать сервер на стороне Alas для доступа со стороны эмулятора.

        Передает данные напрямую в обход adb shell, что обеспечивает более высокую скорость.
        """
        del_cached_property(self, '_nc_server_host_port')
        host_port = self._nc_server_host_port
        logger.info(f'[Устройство — соединение] Обратный сервер слушает {host_port[0]}:{host_port[1]}; клиент может отправлять данные на {host_port[2]}:{host_port[3]}')
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(host_port[:2])
        server.settimeout(5)
        server.listen(5)
        return server

    @cached_property
    def nc_command(self):
        """Получить доступную на устройстве команду netcat.

        Returns:
            list[str]: Доступная команда nc, например ['nc'] или ['busybox', 'nc'].
        """
        if self.is_emulator:
            sdk = self.sdk_ver
            logger.info(f'[Устройство — соединение] Версия SDK: {sdk}')
            if sdk >= 28:
                # В LDPlayer 9 нет `nc`, пробуем `busybox nc`
                # В BlueStacks Pie (Android 9) есть `nc`, но не отправляет данные; приоритетно пробуем `busybox nc`
                trial = [
                    ['busybox', 'nc'],
                    ['nc'],
                ]
            else:
                trial = [
                    ['nc'],
                    ['busybox', 'nc'],
                ]
        else:
            trial = [
                ['nc'],
                ['busybox', 'nc'],
            ]
        for command in trial:
            # Около 3 мс
            # При успехе результатом должна быть справка команды
            # nc: bad argument count (see "nc --help")
            result = self.adb_shell(command)
            # `/system/bin/sh: nc: not found`
            if 'not found' in result:
                continue
            # `/system/bin/sh: busybox: inaccessible or not found\n`
            if 'inaccessible' in result:
                continue
            logger.attr('Команда nc', command)
            return command

        logger.error('[Устройство] Команда `netcat` недоступна. Используйте метод снимка экрана без суффикса `_nc`')
        raise RequestHumanTakeover

    def adb_shell_nc(self, cmd, timeout=5, chunk_size=262144):
        """Передать данные через netcat напрямую в обход adb shell для максимальной скорости.

        Args:
            cmd (list): Список аргументов команды shell.
            timeout (int): Время ожидания в секундах. По умолчанию 5.
            chunk_size (int): Размер блока принимаемых данных. По умолчанию 262144.

        Returns:
            bytes: Принятые сырые данные.
        """
        # Сервер начинает прослушивание
        server = self.reverse_server
        server.settimeout(timeout)
        # Клиент отправляет данные, ожидание принятия подключения сервером
        # <command> | nc 127.0.0.1 {port}
        cmd += ["|", *self.nc_command, *self._nc_server_host_port[2:]]
        stream = self.adb_shell(cmd, stream=True, recvall=False)

        def _safe_close(s):
            try:
                # AdbConnection может предоставлять метод close или свойство `conn`
                if hasattr(s, 'close'):
                    s.close()
                    return
                if hasattr(s, 'conn') and hasattr(s.conn, 'close'):
                    s.conn.close()
                    return
                if isinstance(s, socket.socket):
                    s.close()
            except Exception:
                pass

        try:
            # Сервер принимает подключение
            conn, conn_port = server.accept()
        except socket.timeout:
            try:
                output = recv_all(stream, chunk_size=chunk_size)
                logger.warning(f'[Устройство — соединение] {output}')
            finally:
                _safe_close(stream)
            raise AdbTimeout('Истекло время ожидания подключения к reverse-серверу ADB')

        try:
            # Сервер принимает данные
            data = recv_all(conn, chunk_size=chunk_size, recv_interval=0.001)
        finally:
            # Сервер закрывает подключение и освобождает ресурсы потока ADB
            try:
                conn.close()
            except Exception:
                pass
            _safe_close(stream)

        return data

    def adb_exec_out(self, cmd, serial=None):
        cmd.insert(0, 'exec-out')
        return self.adb_command(cmd, serial)

    def adb_forward(self, remote):
        """Выполнить `adb forward <local> <remote>`.

        Выбирает случайный порт из FORWARD_PORT_RANGE либо переиспользует существующий проброс порта,
        одновременно удаляя лишние записи проброса.

        Args:
            remote (str): Удаленный адрес, например:
                tcp:<port>
                localabstract:<unix domain socket name>
                localreserved:<unix domain socket name>
                localfilesystem:<unix domain socket name>
                dev:<character device name>
                jdwp:<process pid> (remote only)

        Returns:
            int: Номер локального порта.
        """
        port = 0
        for forward in self.adb.forward_list():
            if forward.serial == self.serial and forward.remote == remote and forward.local.startswith('tcp:'):
                if not port:
                    logger.info(f'[Устройство — соединение] Повторное использование перенаправления порта: {forward}')
                    port = int(forward.local[4:])
                else:
                    logger.info(f'[Устройство — соединение] Удаление лишнего перенаправления порта: {forward}')
                    self.adb_forward_remove(forward.local)

        if port:
            return port
        else:
            # Создание нового перенаправления портов
            port = random_port(self.config.FORWARD_PORT_RANGE)
            forward = ForwardItem(self.serial, f'tcp:{port}', remote)
            logger.info(f'[Устройство — соединение] Создание перенаправления порта: {forward}')
            self.adb.forward(forward.local, forward.remote)
            return port

    def _adb_reverse_transport(self, remote: str, local: str, norebind: bool = False):
        """Выполнить проброс ADB reverse (портировано из исправления https://github.com/openatx/adbutils/pull/116).

        Используйте данный метод вместо self.adb.reverse().
        """
        args = ["reverse:forward"]
        if norebind:
            args.append("norebind")
        args.append(remote + ";" + local)
        cmd = ":".join(args)
        with self.adb_client.make_connection() as c:
            c.send_command(f'host:transport:{self.serial}')
            c.check_okay()
            c.send_command(cmd)
            c.check_okay()

    def adb_reverse(self, remote):
        port = 0
        for reverse in self.adb.reverse_list():
            if reverse.remote == remote and reverse.local.startswith('tcp:'):
                if not port:
                    logger.info(f'[Устройство — соединение] Повторное использование обратного перенаправления: {reverse}')
                    port = int(reverse.local[4:])
                else:
                    logger.info(f'[Устройство — соединение] Удаление лишнего обратного перенаправления: {reverse}')
                    self.adb_reverse_remove(reverse.remote)

        if port:
            return port
        else:
            # Создание нового reverse-перенаправления
            port = random_port(self.config.FORWARD_PORT_RANGE)
            reverse = ReverseItem(remote, f'tcp:{port}')
            logger.info(f'[Устройство — соединение] Создание обратного перенаправления: {reverse}')
            self._adb_reverse_transport(reverse.remote, reverse.local)
            return port

    def adb_forward_remove(self, local):
        """Удалить проброс порта ADB, эквивалентно `adb -s <serial> forward --remove <local>`.

        При удалении несуществующего проброса исключение не выбрасывается.

        Подробнее о командах, отправляемых на ADB-сервер:
        https://cs.android.com/android/platform/superproject/+/master:packages/modules/adb/SERVICES.TXT

        Args:
            local (str): Локальный адрес, например 'tcp:2437'.
        """
        try:
            with self.adb_client.make_connection() as c:
                list_cmd = f"host-serial:{self.serial}:killforward:{local}"
                c.send_command(list_cmd)
                c.check_okay()
        except AdbError as e:
            # Удаление несуществующего перенаправления не вызывает исключений
            # adbutils.errors.AdbError: listener 'tcp:8888' not found
            msg = str(e)
            if re.search(r'listener .*? not found', msg):
                logger.warning(f'[Устройство — соединение] {type(e).__name__}: {msg}')
            else:
                raise

    def adb_reverse_remove(self, local):
        """Удалить проброс ADB reverse, эквивалентно `adb -s <serial> reverse --remove <local>`.

        При удалении несуществующего reverse-проброса исключение не выбрасывается.

        Args:
            local (str): Локальный адрес, например 'tcp:2437'.
        """
        try:
            with self.adb_client.make_connection() as c:
                c.send_command(f"host:transport:{self.serial}")
                c.check_okay()
                list_cmd = f"reverse:killforward:{local}"
                c.send_command(list_cmd)
                c.check_okay()
        except AdbError as e:
            # Удаление несуществующего перенаправления не вызывает исключений
            # adbutils.errors.AdbError: listener 'tcp:8888' not found
            msg = str(e)
            if re.search(r'listener .*? not found', msg):
                logger.warning(f'[Устройство — соединение] {type(e).__name__}: {msg}')
            else:
                raise

    def adb_push(self, local, remote):
        """Отправить файл на устройство, эквивалентно `adb push <local> <remote>`.

        Args:
            local (str): Путь к локальному файлу.
            remote (str): Целевой путь на устройстве.

        Returns:
            str: Вывод команды.
        """
        cmd = ['push', local, remote]
        return self.adb_command(cmd)

    def _wait_device_appear(self, serial, first_devices=None):
        """Ожидать появления устройства в списке устройств ADB.

        Args:
            serial (str): Серийный номер (serial) устройства.
            first_devices (list[AdbDeviceWithStatus]): Исходный список устройств во избежание повторного запроса.

        Returns:
            bool: Появилось ли устройство.
        """
        # Ожидание чуть дольше 5 секунд
        timeout = Timer(5.2).start()
        first_log = True
        while 1:
            if first_devices is not None:
                devices = first_devices
                first_devices = None
            else:
                devices = self.list_device()
            # Проверка появления устройства
            for device in devices:
                if device.serial == serial and device.status == 'device':
                    return True
            # Повторная проверка после задержки
            if timeout.reached():
                break
            if first_log:
                logger.info(f'[Устройство — соединение] Ожидание появления устройства: {serial}')
                first_log = False
            time.sleep(0.05)

        return False

    @Config.when(DEVICE_OVER_HTTP=False)
    def adb_connect(self, wait_device=True):
        """Подключиться к устройству с указанным серийным номером, до 3 попыток.

        Если работает устаревший сервер ADB, а Alas использует более новую версию (часто встречается в китайских эмуляторах),
        первая попытка завершает старый сервер, а вторая выполняет фактическое подключение.

        Args:
            wait_device (bool): Ожидать ли появление emulator-* и Android-устройств. По умолчанию True.

        Returns:
            bool: Успешно ли подключение.
        """
        # Перед подключением отключаем офлайн-устройства
        devices = self.list_device()
        for device in devices:
            if device.status == 'offline':
                logger.warning(f'[Устройство — соединение] Устройство {device.serial} находится offline; перед подключением выполняется отключение')
                msg = self.adb_client.disconnect(device.serial)
                if msg:
                    logger.info(msg)
            elif device.status == 'unauthorized':
                logger.error(f'[Устройство — соединение] Устройство {device.serial} не авторизовано. Подтвердите отладку ADB на устройстве')
            elif device.status == 'device':
                pass
            else:
                logger.warning(f'[Устройство — соединение] Неизвестное состояние устройства {device.serial}: {device.status}')

        # Пропускаем подключение emulator-5554 и Android-телефонов: они подключаются автоматически
        if 'emulator-' in self.serial:
            if wait_device:
                if self._wait_device_appear(self.serial, first_devices=devices):
                    logger.info(f'[Устройство — соединение] Serial {self.serial} подключён')
                    return True
                else:
                    logger.info(f'[Устройство — соединение] Serial {self.serial} не подключён')
            logger.info(f'[Устройство — соединение] «{self.serial}» является serial вида `emulator-*`; подключение ADB пропущено')
            return True
        if re.match(r'^[a-zA-Z0-9]+$', self.serial):
            if wait_device:
                if self._wait_device_appear(self.serial, first_devices=devices):
                    logger.info(f'[Устройство — соединение] Serial {self.serial} подключён')
                    return True
                else:
                    logger.info(f'[Устройство — соединение] Serial {self.serial} не подключён')
            logger.info(f'[Устройство — соединение] «{self.serial}» выглядит как Android serial; подключение ADB пропущено')
            return True

        # Попытка подключения
        for _ in range(3):
            msg = self.adb_client.connect(self.serial)
            logger.info(msg)
            # Connected to 127.0.0.1:59865
            # Already connected to 127.0.0.1:59865
            if 'connected' in msg:
                return True
            # bad port number '598265' in '127.0.0.1:598265'
            elif 'bad port' in msg:
                possible_reasons('[Устройство — ADB] Серийный идентификатор указан неверно; возможно, допущена опечатка')
                raise RequestHumanTakeover
            # cannot connect to 127.0.0.1:55555:
            # No connection could be made because the target machine actively refused it. (10061)
            elif '(10061)' in msg:
                # При занятом порте MuMu12 может изменить серийный номер
                # Проверяем соседние порты перебором при смене серийного номера
                if self.is_mumu12_family:
                    before = self.serial
                    serial_list = [self.serial.replace(str(self.port), str(self.port + offset))
                                   for offset in [1, -1, 2, -2]]
                    self.adb_brute_force_connect(serial_list)
                    self.detect_device()
                    if self.serial != before:
                        return True
                run_once(self.check_mumu_bridge_network)()
                # Устройство не существует
                logger.warning('[Устройство — соединение] Устройство не существует. Перезапустите эмулятор или задайте правильный serial')
                logger.warning('[Устройство] Serial эмулятора не существует. Перезапустите эмулятор или задайте правильный Serial')
                logger.warning('[Устройство] ADB не может подключиться к эмулятору либо эмулятор не запущен')
                raise EmulatorNotRunningError

        # Ошибка подключения
        logger.warning(f'[Устройство — соединение] Не удалось подключиться к {self.serial} после 3 попыток; соединение считается установленным')
        self.detect_device()
        return False

    def adb_brute_force_connect(self, serial_list):
        """Параллельное подключение к нескольким serial для обработки смены портов в MuMu12.

        Args:
            serial_list (list[str]): Список проверяемых серийных номеров.
        """
        def connect(s):
            try:
                msg = self.adb_client.connect(s)
            except Exception:
                return ''
            logger.info(msg)
            return msg

        with WORKER_POOL.wait_jobs() as pool:
            for serial in serial_list:
                pool.start_thread_soon(connect, serial)

    def check_mumu_bridge_network(self):
        """Проверить, включен ли сетевой мост в MuMu12 (должен быть отключен).

        Returns:
            bool: True при успешной проверке, False если проверка пропущена.
        """
        if not self.is_mumu12_family:
            return True
        if not hasattr(self, 'find_emulator_instance'):
            return False
        # Предполагается, что PlatformBase наследует этот класс
        instance = self.find_emulator_instance(
            serial=self.serial,
        )
        if instance is None:
            logger.warning(f'[Устройство — соединение] Не удалось проверить сетевой мост MuMu: экземпляр эмулятора не найден')
            return False
        file = instance.mumu_vms_config('customer_config.json')
        try:
            with open(file, mode='r', encoding='utf-8') as f:
                s = f.read()
                data = json.loads(s)
        except FileNotFoundError:
            logger.warning(f'[Устройство — соединение] Не удалось проверить сетевой мост MuMu: файл {file} не существует')
            return False
        value = deep_get(data, keys='customer.network_bridge_opened', default=None)
        logger.attr('Сетевой мост включён', value)
        if str(value).lower() == 'true':
            logger.critical('[Устройство — соединение] Отключите «Сетевой мост» в настройках MuMuPlayer')
            logger.critical('[Устройство] Отключите «Сетевой мост» в настройках эмулятора MuMu')
            raise RequestHumanTakeover
        return True

    @Config.when(DEVICE_OVER_HTTP=True)
    def adb_connect(self, wait_device=True):
        # При подключении по HTTP adb connect не требуется
        return True

    def release_resource(self):
        del_cached_property(self, 'hermit_session')
        del_cached_property(self, 'droidcast_session')
        del_cached_property(self, '_minitouch_builder')
        del_cached_property(self, '_maatouch_builder')
        del_cached_property(self, 'reverse_server')

    def adb_disconnect(self):
        msg = self.adb_client.disconnect(self.serial)
        if msg:
            logger.info(msg)
        self.release_resource()

    def adb_restart(self):
        """Перезапустить клиент ADB."""
        logger.info('[Устройство — соединение] Перезапуск ADB')
        # Завершение текущего клиента
        self.adb_client.server_kill()
        # Повторная инициализация клиента ADB
        del_cached_property(self, 'adb_client')
        self.release_resource()
        _ = self.adb_client

    @Config.when(DEVICE_OVER_HTTP=False)
    def adb_reconnect(self):
        """Повторно подключить устройство ADB.

        Перезапускает клиент ADB, если устройство не найдено, иначе пытается повторно подключиться.
        """
        if self.config.Emulator_AdbRestart and len(self.list_device()) == 0:
            # Перезапуск ADB
            self.adb_restart()
            # Подключение к устройству
            self.adb_connect()
            self.detect_device()
        else:
            self.adb_disconnect()
            self.adb_connect()
            self.detect_device()

    @Config.when(DEVICE_OVER_HTTP=True)
    def adb_reconnect(self):
        logger.warning(
            f'[Устройство — соединение] Устройство подключено по HTTP: {self.serial}; adb_reconnect() пропущен. Возможно, потребуется вручную перезапустить ATX'
        )

    def install_uiautomator2(self):
        """Инициализировать встроенный uiautomator2 3.x server и убрать minicap."""
        if self.is_over_http:
            logger.info('[Устройство — соединение] HTTP uiautomator2 использует уже доступный endpoint')
            return

        logger.info('[Устройство — соединение] Инициализация uiautomator2 3.x')
        if has_cached_property(self, 'u2'):
            self.u2.reset_uiautomator()
        else:
            _ = self.u2
        self.uninstall_minicap()

    def uninstall_minicap(self):
        """Удалить minicap. Minicap не работает на некоторых эмуляторах или передает сжатые изображения."""
        logger.info('[Устройство — соединение] Удаление minicap')
        self.adb_shell(["rm", "/data/local/tmp/minicap"])
        self.adb_shell(["rm", "/data/local/tmp/minicap.so"])

    @Config.when(DEVICE_OVER_HTTP=False)
    def restart_atx(self):
        """Перезапустить встроенный uiautomator2 server."""
        logger.info('[Устройство — соединение] Перезапуск uiautomator2')
        self.u2.reset_uiautomator()

    @Config.when(DEVICE_OVER_HTTP=True)
    def restart_atx(self):
        logger.info(
            f'[Устройство — соединение] Перезапуск HTTP uiautomator2 для устройства {self.serial}'
        )
        self.u2.reset_uiautomator()

    @staticmethod
    def sleep(second):
        """Приостановить выполнение на указанное время.

        Args:
            second (int, float, tuple): Время ожидания в секундах: фиксированное число или кортеж диапазона.
        """
        time.sleep(ensure_time(second))

    _orientation_description = {
        0: 'обычная',
        1: 'кнопка «Домой» справа',
        2: 'кнопка «Домой» сверху',
        3: 'кнопка «Домой» слева',
    }
    orientation = 0

    @retry
    def get_orientation(self):
        """Получить ориентацию экрана устройства.

        Returns:
            int: Значение ориентации экрана:
                0: обычная
                1: кнопка «Домой» справа
                2: кнопка «Домой» сверху
                3: кнопка «Домой» слева
        """
        _DISPLAY_RE = re.compile(
            r'.*DisplayViewport{.*valid=true, .*orientation=(?P<orientation>\d+), .*deviceWidth=(?P<width>\d+), deviceHeight=(?P<height>\d+).*'
        )
        output = self.adb_shell(['dumpsys', 'display'])

        res = _DISPLAY_RE.search(output, 0)

        if res:
            o = int(res.group('orientation'))
            if o in Connection._orientation_description:
                pass
            else:
                invalid_orientation = o
                o = 0
                logger.warning(f'[Устройство — соединение] Недопустимая ориентация устройства: {invalid_orientation}; используется обычная ориентация')
        else:
            o = 0
            logger.warning('[Устройство — соединение] Не удалось получить ориентацию устройства; используется обычная ориентация')

        self.orientation = o
        logger.attr('Ориентация устройства', f'{o} ({Connection._orientation_description.get(o, "Неизвестно")})')
        return o

    @retry
    def list_device(self):
        """Получить список всех устройств ADB.

        Returns:
            SelectedGrids[AdbDeviceWithStatus]: Список устройств.
        """
        devices = []
        try:
            for info in self.adb_client.list():
                devices.append(AdbDeviceWithStatus(self.adb_client, info.serial, info.state))
        except ConnectionResetError as e:
            # Встречается у некоторых пользователей
            # ConnectionResetError: [WinError 10054] Удаленный хост принудительно разорвал существующее подключение.
            logger.error(str(f'[Устройство — соединение] Ошибка получения списка ADB-устройств: {e}'))
            if '强迫关闭' in str(e):
                logger.critical('[Устройство] Не удалось подключиться к службе ADB. Закройте UU Accelerator, частные серверы Genshin Impact и прокси-программы, перехватывающие локальные соединения между Alas и эмулятором')
        return SelectedGrids(devices)

    def detect_device(self):
        """Обнаружить доступные устройства.

        Если serial=='auto' и обнаружено ровно 1 устройство, использовать его.
        """
        logger.hr('Обнаружение устройств')
        available = SelectedGrids([])
        devices = SelectedGrids([])

        @run_once
        def brute_force_connect():
            logger.info('[Устройство — соединение] Принудительное подключение')
            from deploy.Windows.emulator import EmulatorManager
            manager = EmulatorManager()
            manager.brute_force_connect()

        for _ in range(2):
            logger.info('[Устройство — соединение] Доступные устройства перечислены ниже. Скопируйте нужный serial в Alas.Emulator.Serial или задайте Alas.Emulator.Serial="auto"')
            devices = self.list_device()

            # Отображение доступных устройств
            available = devices.select(status='device')
            for device in available:
                logger.info(device.serial)
            if not len(available):
                logger.info('[Устройство — соединение] Доступных устройств нет')

            # Отображение недоступных устройств
            unavailable = devices.delete(available)
            if len(unavailable):
                logger.info('[Устройство — соединение] Обнаружены следующие недоступные устройства')
                for device in unavailable:
                    logger.info(f'{device.serial} ({device.status})')

            # Подключение перебором портов
            if self.config.Emulator_Serial == 'auto' and available.count == 0:
                logger.warning(f'[Устройство — соединение] Доступные устройства не найдены')
                if IS_WINDOWS:
                    brute_force_connect()
                    continue
                else:
                    break
            else:
                break

        # Автоопределение устройства
        if self.config.Emulator_Serial == 'auto':
            if available.count == 0:
                logger.critical('[Устройство — соединение] Доступные устройства не найдены, поэтому автоматическое обнаружение не работает. Задайте точный serial в Alas.Emulator.Serial вместо "auto"')
                raise RequestHumanTakeover
            elif available.count == 1:
                logger.info(f'[Устройство — соединение] Автоматическое обнаружение нашло одно устройство; оно будет использовано')
                self.config.Emulator_Serial = self.serial = available[0].serial
                del_cached_property(self, 'adb')
            elif available.count == 2 \
                    and available.select(serial='127.0.0.1:7555') \
                    and available.select(may_mumu12_family=True):
                logger.info(f'[Устройство — соединение] Автоматическое обнаружение нашло устройство MuMu12; оно будет использовано')
                # Для серийных номеров MuMu12 вроде 127.0.0.1:7555 и 127.0.0.1:16384
                # игнорируем 7555 и используем 16384
                remain = available.select(may_mumu12_family=True).first_or_none()
                self.config.Emulator_Serial = self.serial = remain.serial
                del_cached_property(self, 'adb')
            else:
                logger.critical('[Устройство — соединение] Найдено несколько устройств, поэтому автоматический выбор невозможен. Скопируйте одно из перечисленных устройств в Alas.Emulator.Serial')
                raise RequestHumanTakeover

        # Обработка эмулятора LDPlayer
        # Серийный номер LDPlayer переключается между `127.0.0.1:5555+{X}` и `emulator-5554+{X}`
        # Обрабатываем динамически, не записывая в конфигурацию
        port_serial, emu_serial = get_serial_pair(self.serial)
        if port_serial and emu_serial:
            # Возможно, LDPlayer: проверяем подключенные устройства
            port_device = devices.select(serial=port_serial).first_or_none()
            emu_device = devices.select(serial=emu_serial).first_or_none()
            if port_device and emu_device:
                # Найдено сопряженное устройство, проверяем статус для получения верного серийного номера
                if port_device.status == 'device' and emu_device.status == 'offline':
                    self.serial = port_serial
                    logger.info(f'[Устройство — соединение] Найдена пара устройств LDPlayer: {port_device}, {emu_device}. Используется serial: {self.serial}')
                elif port_device.status == 'offline' and emu_device.status == 'device':
                    self.serial = emu_serial
                    logger.info(f'[Устройство — соединение] Найдена пара устройств LDPlayer: {port_device}, {emu_device}. Используется serial: {self.serial}')
            elif not devices.select(serial=self.serial):
                # Текущий серийный номер не найден
                if port_device and not emu_device:
                    logger.info(f'[Устройство — соединение] Текущий serial {self.serial} не найден, но найдено парное устройство {port_serial}. Используется serial: {port_serial}')
                    self.serial = port_serial
                if not port_device and emu_device:
                    logger.info(f'[Устройство — соединение] Текущий serial {self.serial} не найден, но найдено парное устройство {emu_serial}. Используется serial: {emu_serial}')
                    self.serial = emu_serial

        # Перенаправляем MuMu12 с 127.0.0.1:7555 на 127.0.0.1:16xxx
        if self.serial == '127.0.0.1:7555':
            for _ in range(2):
                mumu12 = available.select(may_mumu12_family=True)
                if mumu12.count == 1:
                    emu_serial = mumu12.first_or_none().serial
                    logger.warning(f'[Устройство — соединение] Перенаправление MuMu12: {self.serial} → {emu_serial}')
                    self.config.Emulator_Serial = self.serial = emu_serial
                    break
                elif mumu12.count >= 2:
                    logger.warning(f'[Устройство] Обнаружено несколько serial MuMu12; перенаправление невозможно')
                    break
                else:
                    # Присутствует только 127.0.0.1:7555
                    if self.is_mumu_over_version_356:
                        # is_mumu_over_version_356 и nemud_app_keep_alive уже кэшированы;
                        # так как устройство то же самое, это допустимо
                        logger.warning(f'[Устройство — соединение] Устройство {self.serial} относится к MuMu12, но соответствующий порт не найден')
                        if IS_WINDOWS:
                            brute_force_connect()
                        devices = self.list_device()
                        # Отображение доступных устройств
                        available = devices.select(status='device')
                        for device in available:
                            logger.info(device.serial)
                        if not len(available):
                            logger.info('[Устройство — соединение] Доступных устройств нет')
                        continue
                    else:
                        # MuMu6
                        break

        # Если порт 16384 в MuMu12 занят, используется 127.0.0.1:16385; автоперенаправление
        # Обрабатываем динамически, не записывая в конфигурацию
        if self.is_mumu12_family:
            matched = False
            for device in available.select(may_mumu12_family=True):
                if device.port == self.port:
                    # Точное совпадение
                    matched = True
                    break
            if not matched:
                for device in available.select(may_mumu12_family=True):
                    if -2 <= device.port - self.port <= 2:
                        # Порт переключился
                        logger.info(f'[Устройство — соединение] Замена serial MuMu12: {self.serial} → {device.serial}')
                        del_cached_property(self, 'port')
                        del_cached_property(self, 'is_mumu12_family')
                        del_cached_property(self, 'is_mumu_family')
                        self.serial = device.serial
                        break

    @retry
    def list_package(self, show_log=True):
        """Получить список всех установленных на устройстве пакетов.

        В первую очередь используется dumpsys для максимальной скорости.
        """
        # Около 80 мс
        if show_log:
            logger.info('[Устройство — соединение] Получение списка пакетов')
        output = self.adb_shell(r'dumpsys package | grep "Package \["')
        packages = re.findall(r'Package \[([^\s]+)\]', output)
        if len(packages):
            return packages

        # Около 200 мс
        if show_log:
            logger.info('[Устройство — соединение] Получение списка пакетов')
        output = self.adb_shell(['pm', 'list', 'packages'])
        packages = re.findall(r'package:([^\s]+)', output)
        return packages

    def list_known_packages(self, show_log=True):
        """Получить список известных игровых пакетов на устройстве (Azur Lane и дистрибутивы каналов).

        Args:
            show_log (bool): Выводить ли лог. По умолчанию True.

        Returns:
            list[str]: Список имен пакетов.
        """
        packages = self.list_package(show_log=show_log)
        packages = [p for p in packages if p in VALID_PACKAGE or p in VALID_CHANNEL_PACKAGE]
        return packages

    def detect_package(self, set_config=True):
        """Обнаружить установленный клиентский пакет Azur Lane на устройстве."""
        logger.hr('Обнаружение пакета приложения')
        packages = self.list_known_packages()

        # Отображение доступных пакетов
        logger.info(f'[Устройство — соединение] Доступные пакеты на устройстве «{self.serial}» перечислены ниже. Скопируйте нужный пакет в Alas.Emulator.PackageName')
        if len(packages):
            for package in packages:
                logger.info(package)
        else:
            logger.info(f'[Устройство — соединение] На устройстве «{self.serial}» не найдено доступных пакетов')

        # Автоопределение пакета
        if len(packages) == 0:
            logger.critical(f'[Устройство — соединение] Пакет Azur Lane не найден. Убедитесь, что игра установлена на устройстве «{self.serial}»')
            raise RequestHumanTakeover
        if len(packages) == 1:
            logger.info('[Устройство — соединение] Автоматическое обнаружение нашло один пакет; он будет использован')
            self.package = packages[0]
            # Запись конфигурации
            if set_config:
                self.config.Emulator_PackageName = self.package
            # Настройка сервера
            logger.info('[Устройство — соединение] Сервер изменён; ресурсы освобождаются')
            set_server(self.package)
        else:
            logger.critical(
                '[Устройство — соединение] Найдено несколько пакетов Azur Lane, поэтому автоматический выбор невозможен. Скопируйте один из перечисленных пакетов в Alas.Emulator.PackageName')
            raise RequestHumanTakeover
