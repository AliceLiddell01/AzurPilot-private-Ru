"""Windows 平台模拟器控制。继承 PlatformBase 和 EmulatorManager，
实现 Windows 上模拟器的启动、窗口聚焦和进程管理。"""

from __future__ import annotations
import ctypes
import re
import subprocess

import psutil

from deploy.Windows.utils import DataProcessInfo
from module.base.decorator import run_once
from module.base.timer import Timer
from module.device.connection_attr import ConnectionAttr
from module.device.platform.platform_base import PlatformBase
from module.device.platform.emulator_windows import Emulator, EmulatorInstance, EmulatorManager
from module.logger import logger


class EmulatorUnknown(Exception):
    """未知模拟器类型异常。"""
    pass


def get_focused_window():
    """获取当前前台窗口的句柄。"""
    return ctypes.windll.user32.GetForegroundWindow()


def set_focus_window(hwnd):
    """将指定窗口设置为前台窗口。"""
    ctypes.windll.user32.SetForegroundWindow(hwnd)


def get_window_text(hwnd):
    """获取窗口标题文本。"""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ''
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def check_mumu_error_dialog():
    """
    检测 MuMu 模拟器的错误对话框（如权限冲突）。

    Returns:
        bool: True 表示检测到错误对话框
    """
    # Заголовок окна ошибки MuMu12 содержит "MuMu" или "NemuWindow"
    # Заголовок окна конфликта прав обычно содержит "MuMuPlayer" или похожий текст
    found = False

    def enum_callback(hwnd, _):
        nonlocal found
        text = get_window_text(hwnd)
        if text and ('MuMu' in text or 'Nemu' in text):
            # Проверяем, является ли окно диалогом ошибки (обычно короткий заголовок и всплывающее окно)
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                # Перебираем дочерние окна в поиске текста "无法启动" или "冲突"
                child_found = [False]

                def child_callback(child_hwnd, __):
                    child_text = get_window_text(child_hwnd)
                    if child_text and ('无法启动' in child_text or '冲突' in child_text
                                       or 'error' in child_text.lower()
                                       or 'cannot' in child_text.lower()):
                        child_found[0] = True
                    return True

                ctypes.windll.user32.EnumChildWindows(
                    hwnd,
                    ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(child_callback),
                    0
                )
                if child_found[0]:
                    found = True
                    logger.warning(f'[Устройство — Windows] Обнаружено окно ошибки MuMu: «{text}»')
        return True

    try:
        ctypes.windll.user32.EnumWindows(
            ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(enum_callback),
            0
        )
    except Exception as e:
        logger.warning(f'[Устройство — Windows] Не удалось проверить окна ошибок MuMu: {e}')
    return found


def minimize_window(hwnd):
    """最小化指定窗口。"""
    ctypes.windll.user32.ShowWindow(hwnd, 6)


def get_window_title(hwnd):
    """
    获取指定窗口的标题文本。

    Args:
        hwnd: 窗口句柄

    Returns:
        str: 窗口标题
    """
    text_len_in_characters = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    string_buffer = ctypes.create_unicode_buffer(
        text_len_in_characters + 1)  # +1 для завершающего null-символа \0
    ctypes.windll.user32.GetWindowTextW(hwnd, string_buffer, text_len_in_characters + 1)
    return string_buffer.value


def flash_window(hwnd, flash=True):
    """闪烁指定窗口以吸引注意力。"""
    ctypes.windll.user32.FlashWindow(hwnd, flash)


class PlatformWindows(PlatformBase, EmulatorManager):
    """Windows 平台的模拟器控制接口。"""

    def __init__(self, config, *, connect: bool = True):
        """
        Args:
            config: AzurLaneConfig 实例或配置名称
            connect: 是否立即建立 ADB 连接。
                     AlasPlus 在仅需要模拟器发现/启停控制
                     且模拟器当前离线时使用 connect=False，
                     以避免过早抛出 EmulatorNotRunningError。
        """
        if connect:
            # Исходное поведение: выполняем полный процесс Connection.__init__,
            # включая detect_device() и adb_connect()
            super().__init__(config)
        else:
            # Облегчённая инициализация: подготавливаем только config/adb_client/serial,
            # не вызываем adb_connect(), поэтому даже при ещё не запущенном эмуляторе
            # можно безопасно использовать emulator_instance/emulator_start()
            ConnectionAttr.__init__(self, config)

    @classmethod
    def execute(cls, command, wait=False, timeout=30):
        """
        执行外部命令。

        Args:
            command (str): 要执行的命令
            wait (bool): 是否同步等待命令完成（默认False异步执行）
            timeout (int): 同步执行时的超时秒数（默认30秒）

        Returns:
            subprocess.Popen: 异步执行时返回子进程对象
            subprocess.CompletedProcess: 同步执行时返回完成结果
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        logger.info(f'[Устройство — Windows] Выполнение команды: {command}')

        if wait:
            # Выполняем синхронно и ждём завершения команды
            # Используется там, где нужно гарантировать завершение команды (например shutdown_player в MuMu12)
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    timeout=timeout,
                    close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                logger.info(f'[Устройство — Windows] Команда завершилась с кодом {result.returncode}')
                return result
            except subprocess.TimeoutExpired:
                logger.warning(f'[Устройство — Windows] Истёк тайм-аут команды: {timeout} с')
                return None
        else:
            # Выполняем асинхронно, не ожидая завершения (исходное поведение)
            # `close_fds` действует только на Windows
            # `start_new_session` не даёт завершить дерево процессов эмулятора при kill Alas
            return subprocess.Popen(command, close_fds=True, start_new_session=True)

    @classmethod
    def kill_process_by_regex(cls, regex: str) -> int:
        """
        终止命令行匹配给定正则表达式的进程。

        Args:
            regex: 正则表达式

        Returns:
            int: 已终止的进程数量
        """
        count = 0

        for proc in psutil.process_iter():
            cmdline = DataProcessInfo(proc=proc, pid=proc.pid).cmdline
            if re.search(regex, cmdline):
                logger.info(f'[Устройство — Windows] Завершение эмулятора: {cmdline}')
                proc.kill()
                count += 1

        return count

    def _emulator_start(self, instance: EmulatorInstance):
        """
        启动模拟器（不含错误处理）。

        Args:
            instance: 模拟器实例
        """
        exe: str = instance.emulator.path
        if instance == Emulator.MuMuPlayer:
            # NemuPlayer.exe
            self.execute(exe)
        elif instance == Emulator.MuMuPlayerX:
            # NemuPlayer.exe -m nemu-12.0-x64-default
            self.execute(f'"{exe}" -m {instance.name}')
        elif instance == Emulator.MuMuPlayer12:
            # MuMuManager.exe api -v 0 launch_player
            # Launch via MuMuManager instead of MuMuPlayer.exe/MuMuNxMain.exe.
            # MuMuNxMain.exe is a GUI singleton, if two instances get launched at the same time,
            # the second launch request is handed over to a MuMuNxMain.exe that is still initializing
            # and gets silently dropped, while MuMuManager queues requests in backend service.
            if instance.MuMuPlayer12_id is None:
                logger.warning(f'[Устройство — Windows] Не удалось получить индекс экземпляра MuMu из имени {instance.name}')
            self.execute(f'"{Emulator.single_to_console(exe)}" api -v {instance.MuMuPlayer12_id} launch_player')
        elif instance == Emulator.LDPlayerFamily:
            # ldconsole.exe launch --index 0
            self.execute(f'"{Emulator.single_to_console(exe)}" launch --index {instance.LDPlayer_id}')
        elif instance == Emulator.NoxPlayerFamily:
            # Nox.exe -clone:Nox_1
            self.execute(f'"{exe}" -clone:{instance.name}')
        elif instance == Emulator.BlueStacks5:
            # HD-Player.exe --instance Pie64
            self.execute(f'"{exe}" --instance {instance.name}')
        elif instance == Emulator.BlueStacks4:
            # Bluestacks.exe -vmname Android_1
            self.execute(f'"{exe}" -vmname {instance.name}')
        elif instance == Emulator.MEmuPlayer:
            # MEmu.exe MEmu_0
            self.execute(f'"{exe}" {instance.name}')
        elif instance.type == 'SSH':
            logger.info('[Устройство — Windows] Запуск эмулятора по удалённой команде SSH')
            self.run_remote_ssh_command(getattr(self.config, 'EmulatorInfo_RemoteStartCommand', ''))
        else:
            raise EmulatorUnknown(f'Не удалось запустить неизвестный экземпляр эмулятора: {instance}')

    def _emulator_stop(self, instance: EmulatorInstance):
        """
        停止模拟器（不含错误处理）。

        Args:
            instance: 模拟器实例
        """
        exe: str = instance.emulator.path
        if instance == Emulator.MuMuPlayer:
            # MuMu6 не поддерживает несколько экземпляров: завершение одного завершает все
            # Всего 4 процесса:
            # "C:\Program Files\NemuVbox\Hypervisor\NemuHeadless.exe" --comment nemu-6.0-x64-default --startvm
            # "E:\ProgramFiles\MuMu\emulator\nemu\EmulatorShell\NemuPlayer.exe"
            # E:\ProgramFiles\MuMu\emulator\nemu\EmulatorShell\NemuService.exe
            # "C:\Program Files\NemuVbox\Hypervisor\NemuSVC.exe" -Embedding
            self.kill_process_by_regex(
                rf'('
                rf'NemuHeadless.exe'
                rf'|NemuPlayer.exe\"'
                rf'|NemuPlayer.exe$'
                rf'|NemuService.exe'
                rf'|NemuSVC.exe'
                rf')'
            )
        elif instance == Emulator.MuMuPlayerX:
            # В MuMu X есть 3 процесса:
            # "E:\ProgramFiles\MuMu9\emulator\nemu9\EmulatorShell\NemuPlayer.exe" -m nemu-12.0-x64-default -s 0 -l
            # "C:\Program Files\Muvm6Vbox\Hypervisor\Muvm6Headless.exe" --comment nemu-12.0-x64-default --startvm xxx
            # "C:\Program Files\Muvm6Vbox\Hypervisor\Muvm6SVC.exe" --Embedding
            self.kill_process_by_regex(
                rf'('
                rf'NemuPlayer.exe.*-m {instance.name}'
                rf'|Muvm6Headless.exe'
                rf'|Muvm6SVC.exe'
                rf')'
            )
        elif instance == Emulator.MuMuPlayer12:
            # MuMuManager.exe api -v 1 shutdown_player
            # Используем синхронное выполнение и ждём завершения, чтобы асинхронный запуск не вызвал ошибку поиска экземпляра
            if instance.MuMuPlayer12_id is None:
                logger.warning(f'[Устройство — Windows] Не удалось получить индекс экземпляра MuMu из имени {instance.name}')
            logger.info('[Устройство — Windows] Остановка MuMuPlayer12: используется синхронное выполнение')
            self.execute(
                f'"{Emulator.single_to_console(exe)}" api -v {instance.MuMuPlayer12_id} shutdown_player',
                wait=True,
                timeout=30
            )
        elif instance == Emulator.LDPlayerFamily:
            # ldconsole.exe quit --index 0
            self.execute(f'"{Emulator.single_to_console(exe)}" quit --index {instance.LDPlayer_id}')
        elif instance == Emulator.NoxPlayerFamily:
            # Nox.exe -clone:Nox_1 -quit
            self.execute(f'"{exe}" -clone:{instance.name} -quit')
        elif instance == Emulator.BlueStacks5:
            # В BlueStacks есть 2 процесса:
            # C:\Program Files\BlueStacks_nxt_cn\HD-Player.exe --instance Pie64
            # C:\Program Files\BlueStacks_nxt_cn\BstkSVC.exe -Embedding
            self.kill_process_by_regex(
                rf'('
                rf'HD-Player.exe.*"--instance" "{instance.name}"'
                rf')'
            )
        elif instance == Emulator.BlueStacks4:
            # E:\Program Files (x86)\BluestacksCN\bsconsole.exe quit --name Android
            self.execute(f'"{Emulator.single_to_console(exe)}" quit --name {instance.name}')
        elif instance == Emulator.MEmuPlayer:
            # F:\Program Files\Microvirt\MEmu\memuc.exe stop -n MEmu_0
            self.execute(f'"{Emulator.single_to_console(exe)}" stop -n {instance.name}')
        elif instance.type == 'SSH':
            logger.info('[Устройство — Windows] Остановка эмулятора по удалённой команде SSH')
            self.run_remote_ssh_command(getattr(self.config, 'EmulatorInfo_RemoteStopCommand', ''))
        else:
            raise EmulatorUnknown(f'Не удалось остановить неизвестный экземпляр эмулятора: {instance}')

    def _emulator_function_wrapper(self, func: callable):
        """
        模拟器启停操作的统一包装器，处理异常。

        Args:
            func (callable): _emulator_start 或 _emulator_stop

        Returns:
            bool: 是否成功
        """
        try:
            func(self.emulator_instance)
            return True
        except OSError as e:
            msg = str(e)
            # OSError: [WinError 740] Запрошенная операция требует повышения прав.
            if 'WinError 740' in msg:
                logger.error('[Устройство — Windows] Для запуска или остановки MuMu требуются права администратора')
        except EmulatorUnknown as e:
            logger.error(str(f'[Устройство — платформа Windows] Ошибка операции эмулятора: {e}'))
        except Exception as e:
            logger.exception(str(f'[Устройство — платформа Windows] Ошибка операции эмулятора: {e}'))

        logger.error(f'[Устройство — Windows] Не удалось выполнить функцию эмулятора {func.__name__}()')
        return False

    def emulator_start_watch(self):
        """
        监控模拟器启动过程，等待启动完成。

        Returns:
            bool: True 表示启动完成，False 表示超时
        """
        logger.hr('Запуск эмулятора', level=2)
        current_window = get_focused_window()
        serial = self.emulator_instance.serial
        logger.info(f'[Устройство — Windows] Текущее окно: {current_window}')

        def adb_connect():
            m = self.adb_client.connect(self.serial)
            if 'connected' in m:
                # Connected to 127.0.0.1:59865
                # Already connected to 127.0.0.1:59865
                return False
            elif '(10061)' in m:
                # cannot connect to 127.0.0.1:55555:
                # No connection could be made because the target machine actively refused it. (10061)
                return False
            else:
                return True

        @run_once
        def show_online(m):
            logger.info(f'[Устройство — Windows] Эмулятор доступен: {m}')

        @run_once
        def show_ping(m):
            logger.info(f'[Устройство — Windows] Команда ping: {m}')

        @run_once
        def show_package(m):
            logger.info(f'[Устройство — Windows] Найден пакет Azur Lane: {m}')

        interval = Timer(0.5).start()
        timeout = Timer(180).start()
        new_window = 0
        while 1:
            interval.wait()
            interval.reset()
            if timeout.reached():
                logger.warning(f'[Устройство — Windows] Истёк тайм-аут запуска эмулятора')
                return False

            try:
                # Проверяем, появилось ли окно эмулятора
                if current_window != 0 and new_window == 0:
                    new_window = get_focused_window()
                    if current_window != new_window:
                        logger.info(f'[Устройство — Windows] Появилось новое окно: {new_window}; фокус возвращён')
                        set_focus_window(current_window)
                    else:
                        new_window = 0

                # Проверяем подключение устройства
                devices = self.list_device().select(serial=serial)
                if devices:
                    device = devices.first_or_none()
                    if device.status == 'device':
                        # Эмулятор уже в сети
                        pass
                    if device.status == 'offline':
                        self.adb_client.disconnect(serial)
                        adb_connect()
                        continue
                else:
                    # Пытаемся подключиться
                    adb_connect()
                    continue
                show_online(devices.first_or_none())

                # Проверяем доступность команд
                try:
                    pong = self.adb_shell(['echo', 'pong'])
                except Exception as e:
                    logger.info(str(f'[Устройство — платформа Windows] Ошибка ожидания запуска эмулятора: {e}'))
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
            except (ConnectionResetError, ConnectionAbortedError) as e:
                # [WinError 10054] Существующее подключение было принудительно закрыто удалённым узлом.
                # Часто возникает во время запуска эмулятора
                logger.info(str(f'[Устройство — платформа Windows] Ошибка ожидания запуска эмулятора: {e}'))
                continue
            except Exception as e:
                logger.exception(str(f'[Устройство — платформа Windows] Ошибка ожидания запуска эмулятора: {e}'))
                continue

            # Обнаружение окон ошибок MuMu, включая конфликт прав
            # При обнаружении окна ошибки немедленно прекращаем ожидание и возвращаем False для повторной попытки
            if check_mumu_error_dialog():
                logger.warning('[Устройство — Windows] Обнаружено окно ошибки MuMu; наблюдение за запуском прервано')
                return False

        if new_window != 0 and new_window != current_window:
            logger.info(f'[Устройство — Windows] Сворачивание нового окна: {new_window}')
            minimize_window(new_window)
        if current_window:
            logger.info(f'[Устройство — Windows] Отключение мигания текущего окна: {current_window}')
            flash_window(current_window, flash=False)
        if new_window:
            logger.info(f'[Устройство — Windows] Мигание нового окна: {new_window}')
            flash_window(new_window, flash=True)
        logger.info('[Устройство — Windows] Запуск эмулятора завершён')
        return True

    def emulator_start(self):
        """
        启动模拟器，最多重试 3 次。
        针对 MuMu12 等模拟器添加实例查找失败后的等待重试机制，
        以及权限冲突时的强制进程清理。
        """
        logger.hr('Запуск эмулятора', level=1)

        # Для MuMuPlayer12 добавляем обработку ошибки поиска экземпляра
        emulator_type = getattr(self.config, 'EmulatorInfo_Emulator', '')
        is_mumu12 = emulator_type == 'MuMuPlayer12' or (
            hasattr(self, '_emulator_instance') and
            self._emulator_instance and
            self._emulator_instance.type == 'MuMuPlayer12'
        )

        for attempt in range(3):
            # Сначала останавливаем (для MuMu12 синхронное выполнение уже гарантирует завершение)
            if not self._emulator_function_wrapper(self._emulator_stop):
                return False

            # MuMu12: немного ждём стабилизации состояния процессов
            if is_mumu12:
                import time
                # Проверяем, не осталось ли процессов, вызывающих конфликт прав
                # Конфликт прав обычно вызывают зависшие процессы MuMuManager/MuMuPlayer
                has_mumu_process = False
                for proc in psutil.process_iter(['name', 'cmdline']):
                    try:
                        name = proc.info['name'] or ''
                        if name.lower() in ('mumuplayer.exe', 'mumumanager.exe',
                                            'nemuplayer.exe', 'nemuheadless.exe'):
                            has_mumu_process = True
                            logger.warning(f'[Устройство — Windows] Обнаружен оставшийся процесс MuMu: {name} (PID={proc.pid})')
                            proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if has_mumu_process:
                    logger.info('[Устройство — Windows] MuMuPlayer12: оставшиеся процессы завершены; ожидание 5 с')
                    time.sleep(5)
                else:
                    logger.info('[Устройство — Windows] MuMuPlayer12: ожидание стабилизации процессов 2 с')
                    time.sleep(2)

            # Затем запускаем
            if self._emulator_function_wrapper(self._emulator_start):
                # Успешно
                if self.emulator_start_watch():
                    return True
                logger.warning('[Устройство — Windows] Ошибка наблюдения за запуском эмулятора; повторная попытка')
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

        logger.error('[Устройство — Windows] Не удалось запустить эмулятор после 3 попыток; дальнейшие попытки прекращены')
        return False

    def emulator_stop(self):
        """停止模拟器，最多重试 3 次。"""
        logger.hr('Остановка эмулятора', level=1)
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

        logger.error('[Устройство — Windows] Не удалось остановить эмулятор после 3 попыток; дальнейшие попытки прекращены')
        return False


if __name__ == '__main__':
    self = PlatformWindows('alas')
    d = self.emulator_instance
    print(d)
