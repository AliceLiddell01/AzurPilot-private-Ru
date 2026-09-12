"""
MaaTouch 触控输入方法。

基于 MaaTouch 工具实现高性能的设备触控操作。
MaaTouch 是 minitouch 的增强替代方案，通过 WebSocket 协议与设备通信，
支持更高的触控采样率和更稳定的连接。提供点击、长按、滑动等触控操作，
滑动使用贝塞尔曲线插值生成自然轨迹。兼容 minitouch 的命令格式，
通过 ADB 端口转发建立 WebSocket 连接。
"""
import socket
import threading
import time
from functools import wraps

from adbutils.errors import AdbError

from module.base.decorator import cached_property, del_cached_property, has_cached_property
from module.base.timer import Timer
from module.base.utils import *
from module.device.connection import Connection
from module.device.method.minitouch import Command, CommandBuilder, insert_swipe
from module.device.method.utils import RETRY_TRIES, handle_adb_error, retry_sleep
from module.exception import EmulatorNotRunningError, RequestHumanTakeover
from module.logger import logger


def handle_unknown_host_service(e):
    pass


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (MaaTouch):
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Обработать невозможно
            except RequestHumanTakeover:
                break
            # Если служба ADB была завершена
            except ConnectionResetError as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
                    del_cached_property(self, '_maatouch_builder')
            # Тайм-аут синхронизации MaaTouch
            # Возможно, служба ADB была завершена
            except MaaTouchSyncTimeout as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
                    del_cached_property(self, '_maatouch_builder')
                    self.reset_maatouch()
            # Эмулятор закрыт
            except ConnectionAbortedError as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
                    del_cached_property(self, '_maatouch_builder')
            # Ошибка ADB
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                        del_cached_property(self, '_maatouch_builder')
                elif handle_unknown_host_service(e):
                    def init():
                        self.adb_start_server()
                        self.adb_reconnect()
                        del_cached_property(self, '_maatouch_builder')
                else:
                    break
            # MaaTouchNotInstalledError: от MaaTouch получено "Aborted"
            except MaaTouchNotInstalledError as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.maatouch_install()
                    del_cached_property(self, '_maatouch_builder')
            except BrokenPipeError as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    del_cached_property(self, '_maatouch_builder')
            # Обработать невозможно — исключение нужно пробросить выше, чтобы перезапустить эмулятор
            except EmulatorNotRunningError:
                raise
            # Неизвестное исключение; возможно, изображение повреждено
            except Exception as e:
                logger.exception(str(f'[Устройство — MaaTouch] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in ['_maatouch_builder']:
            logger.critical(f'[Устройство] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError
        logger.critical(f'[Устройство] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class MaatouchBuilder(CommandBuilder):
    def __init__(
            self,
            device,
            contact=0,
            handle_orientation=False,
    ):
        """
        Args:
            device (MaaTouch): MaaTouch 设备实例。
        """

        super().__init__(device, contact, handle_orientation)

    def send(self):
        return self.device.maatouch_send(builder=self)

    def send_sync(self, mode=2):
        return self.device.maatouch_send_sync(builder=self, mode=mode)

    def end(self):
        self.device.sleep(self.DEFAULT_DELAY)


class MaaTouchNotInstalledError(Exception):
    pass


class MaaTouchSyncTimeout(Exception):
    pass


class MaaTouch(Connection):
    """
    实现与 scrcpy 相同功能、接口类似 minitouch 的控制方案。
    https://github.com/MaaAssistantArknights/MaaTouch
    """
    max_x: int
    max_y: int
    _maatouch_stream: socket.socket = None
    _maatouch_stream_storage = None
    _maatouch_init_thread = None
    _maatouch_orientation: int = None

    @cached_property
    @retry
    def _maatouch_builder(self):
        self.maatouch_init()
        return MaatouchBuilder(self)

    @property
    def maatouch_builder(self):
        # Ждём завершения потока инициализации
        if self._maatouch_init_thread is not None:
            self._maatouch_init_thread.join()
            del self._maatouch_init_thread
            self._maatouch_init_thread = None

        # Возвращаем пустой builder
        self._maatouch_builder.clear()
        return self._maatouch_builder

    def early_maatouch_init(self):
        """
        在 Alas 实例开始截图时启动线程初始化 maatouch 连接。
        这将加速首次点击约 0.2 ~ 0.4 秒。
        """
        if has_cached_property(self, '_maatouch_builder'):
            return

        def early_maatouch_init_func():
            _ = self._maatouch_builder

        thread = threading.Thread(target=early_maatouch_init_func, daemon=True)
        self._maatouch_init_thread = thread
        thread.start()

    def on_orientation_change_maatouch(self):
        """
        MaaTouch 在启动时缓存设备方向。
        方向改变时需要重启。
        """
        if self._maatouch_orientation is None:
            return
        if self.orientation == self._maatouch_orientation:
            return

        logger.info(f'[Устройство — MaaTouch] Ориентация изменена: {self._maatouch_orientation} → {self.orientation}; повторная инициализация MaaTouch')
        del_cached_property(self, '_maatouch_builder')
        self.early_maatouch_init()

    def maatouch_init(self):
        logger.hr('[Устройство — MaaTouch] Инициализация')
        max_x, max_y = 1280, 720
        max_contacts = 2
        max_pressure = 50

        # Пытаемся закрыть существующее соединение
        if self._maatouch_stream is not None:
            try:
                self._maatouch_stream.close()
            except Exception as e:
                logger.error(str(f'[Устройство — MaaTouch] Ошибка инициализации управления: {e}'))
            del self._maatouch_stream
        if self._maatouch_stream_storage is not None:
            del self._maatouch_stream_storage

        # MaaTouch кэширует ориентацию устройства при запуске
        super(MaaTouch, self).get_orientation()
        self._maatouch_orientation = self.orientation

        # CLASSPATH=/data/local/tmp/maatouch app_process / com.shxyke.MaaTouch.App
        stream = self.adb_shell(
            [f'CLASSPATH={self.config.MAATOUCH_FILEPATH_REMOTE}', 'app_process', '/', 'com.shxyke.MaaTouch.App'],
            stream=True,
            recvall=False
        )
        # Сохраняем shell stream, чтобы удаление объекта не закрыло socket
        self._maatouch_stream_storage = stream
        stream = stream.conn
        stream.settimeout(10)
        self._maatouch_stream = stream

        retry_timeout = Timer(5).start()
        while 1:
            # v <version>
            # Версия протокола, обычно 1; использовать её не требуется
            # Получаем информацию от сервера MaaTouch
            socket_out = stream.makefile()

            # ^ <max-contacts> <max-x> <max-y> <max-pressure>
            out = socket_out.readline().replace("\n", "").replace("\r", "")
            logger.info(out)
            if out.strip() == 'Aborted':
                stream.close()
                raise MaaTouchNotInstalledError(
                    '[Устройство — MaaTouch] Получено сообщение «Aborted»; вероятно, MaaTouch не установлен'
                )
            try:
                _, max_contacts, max_x, max_y, max_pressure = out.split(" ")
                break
            except ValueError:
                stream.close()
                if retry_timeout.reached():
                    raise MaaTouchNotInstalledError(
                        '[Устройство — MaaTouch] Получены пустые данные; вероятно, MaaTouch не установлен'
                    )
                else:
                    # MaaTouch мог ещё не успеть запуститься
                    self.sleep(1)
                    continue

        # self.max_contacts = max_contacts
        self.max_x = int(max_x)
        self.max_y = int(max_y)
        # self.max_pressure = max_pressure

        # $ <pid>
        out = socket_out.readline().replace("\n", "").replace("\r", "")
        logger.info(out)
        # _, pid = out.split(" ")
        # self._maatouch_pid = pid

        # Тайм-аут синхронизации — 2 секунды
        stream.settimeout(2)
        logger.info(
            '[Устройство — MaaTouch] Поток подключён'
        )
        logger.info(
            '[Устройство — MaaTouch] max_contact: {}; max_x: {}; max_y: {}; max_pressure: {}'.format(max_contacts, max_x, max_y, max_pressure)
        )

    def maatouch_send(self, builder: MaatouchBuilder):
        content = builder.to_minitouch()
        # logger.info("send operation: {}".format(content.replace("\n", "\\n")))
        byte_content = content.encode('utf-8')
        self._maatouch_stream.sendall(byte_content)
        self._maatouch_stream.recv(0)
        self.sleep(builder.delay / 1000 + builder.DEFAULT_DELAY)
        builder.clear()

    def maatouch_send_sync(self, builder: MaatouchBuilder, mode=2):
        # Задаём режим инъекции последней команды
        for command in builder.commands[::-1]:
            if command.operation in ['r', 'd', 'm', 'u']:
                command.mode = mode
                break

        # Добавляем команду синхронизации MaaTouch: 's <timestamp>\n'
        timestamp = str(int(time.time() * 1000))
        builder.commands.insert(0, Command(
            's', text=timestamp
        ))

        # Отправляем
        content = builder.to_maatouch_sync()
        # logger.info("send operation: {}".format(content.replace("\n", "\\n")))
        byte_content = content.encode('utf-8')
        self._maatouch_stream.sendall(byte_content)
        self._maatouch_stream.recv(0)

        # Ждём завершения операции
        # start = time.time()
        socket_out = self._maatouch_stream.makefile()
        max_trial = 3
        for n in range(3):
            try:
                out = socket_out.readline()
            except socket.timeout as e:
                raise MaaTouchSyncTimeout(str(e))
            out = out.strip()
            # logger.info(out)

            if out == timestamp:
                break
            if out == 'Killed':
                raise MaaTouchNotInstalledError('[Устройство — MaaTouch] MaaTouch завершил работу; вероятно, версия несовместима')
            if n == max_trial - 1:
                raise MaaTouchSyncTimeout('Получено слишком много некорректных ответов синхронизации')
            time.sleep(0.001)

        # logger.info(f'Delay: {builder.delay}')
        # logger.info(f'Waiting control {time.time() - start}')
        self.sleep(builder.DEFAULT_DELAY)
        builder.clear()

    def maatouch_install(self):
        logger.hr('[Устройство — MaaTouch] Установка')
        self.adb_push(self.config.MAATOUCH_FILEPATH_LOCAL, self.config.MAATOUCH_FILEPATH_REMOTE)

    def maatouch_uninstall(self):
        logger.hr('[Устройство — MaaTouch] Удаление')
        self.adb_shell(["rm", self.config.MAATOUCH_FILEPATH_REMOTE])

    @retry
    def click_maatouch(self, x, y):
        builder = self.maatouch_builder
        builder.down(x, y).commit()
        builder.up().commit()
        builder.send_sync()

    @retry
    def long_click_maatouch(self, x, y, duration=1.0):
        duration = int(duration * 1000)
        builder = self.maatouch_builder
        builder.down(x, y).wait(duration).commit()
        builder.up().commit()
        builder.send_sync()

    @retry
    def swipe_maatouch(self, p1, p2):
        points = insert_swipe(p0=p1, p3=p2)
        builder = self.maatouch_builder

        builder.down(*points[0]).commit().wait(10)
        builder.send_sync()

        for point in points[1:]:
            builder.move(*point).wait(10)
        builder.commit()
        builder.send_sync()

        builder.up().commit()
        builder.send_sync()

    @retry
    def drag_maatouch(self, p1, p2, point_random=(-10, -10, 10, 10)):
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        points = insert_swipe(p0=p1, p3=p2, speed=20)
        builder = self.maatouch_builder

        builder.down(*points[0]).commit().wait(10)
        builder.send_sync()

        for point in points[1:]:
            builder.move(*point).commit().wait(10)
        builder.send_sync()

        builder.move(*p2).commit().wait(140)
        builder.move(*p2).commit().wait(140)
        builder.send_sync()

        builder.up().commit()
        builder.send_sync()

    @retry
    def reset_maatouch(self):
        builder = self.maatouch_builder
        builder.reset().commit()
        builder.send_sync()
