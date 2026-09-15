"""
minitouch 触控输入方法。

基于 minitouch 工具实现低延迟的设备触控操作。
minitouch 通过 Unix Socket 直接向 Android 设备的输入子系统发送触控事件，
比 `adb shell input` 命令更快、更精确。支持点击、长按、滑动和多点触控。
使用正态分布随机化触控坐标和速度，模拟自然的用户操作行为。
需要先通过 ADB 将 minitouch 推送至设备并建立 Socket 连接。
"""
import asyncio
import json
import socket
import threading
import time
from functools import wraps
from typing import List

import websockets
from adbutils.errors import AdbError

from module.base.decorator import Config, cached_property, del_cached_property, has_cached_property
from module.base.timer import Timer
from module.base.utils import *
from module.device.connection import Connection
from module.device.method.utils import RETRY_TRIES, handle_adb_error, handle_unknown_host_service, retry_sleep
from module.exception import EmulatorNotRunningError, RequestHumanTakeover, ScriptError
from module.logger import logger


def random_normal_distribution(a, b, n=5):
    output = np.mean(np.random.uniform(a, b, size=n))
    return output


def random_theta():
    theta = np.random.uniform(0, 2 * np.pi)
    return np.array([np.sin(theta), np.cos(theta)])


def random_rho(dis):
    return random_normal_distribution(-dis, dis)


def insert_swipe(p0, p3, speed=15, min_distance=10):
    """
    在起点和终点之间插入路径点，首先生成一条三次贝塞尔曲线。
    First generate a cubic bézier curve
    Args:
        p0: 起点坐标。
        p3: 终点坐标。
        speed: 平均移动速度，像素/10ms。
        min_distance: 最小点间距。

    Returns:
        路径点列表。

    Examples:
        > insert_swipe((400, 400), (600, 600), speed=20)
        [[400, 400], [406, 406], [416, 415], [429, 428], [444, 442], [462, 459], [481, 478], [504, 500], [527, 522],
        [545, 540], [560, 557], [573, 570], [584, 582], [592, 590], [597, 596], [600, 600]]
    """
    p0 = np.array(p0)
    p3 = np.array(p3)

    distance = np.linalg.norm(p3 - p0)

    # Случайные контрольные точки кривой Безье
    p1 = 2 / 3 * p0 + 1 / 3 * p3 + random_theta() * random_rho(distance * 0.1)
    p2 = 1 / 3 * p0 + 2 / 3 * p3 + random_theta() * random_rho(distance * 0.1)

    # Случайные значения `t` на кривой Безье: реже в середине, плотнее по краям
    segments = max(int(distance / speed) + 1, 5)
    lower = random_normal_distribution(-85, -60)
    upper = random_normal_distribution(80, 90)
    theta = np.arange(lower + 0., upper + 0.0001, (upper - lower) / segments)
    ts = np.sin(theta / 180 * np.pi)
    ts = np.sign(ts) * abs(ts) ** 0.9
    ts = (ts - min(ts)) / (max(ts) - min(ts))

    # Генерируем кубическую кривую Безье
    points = []
    prev = (-100, -100)
    for t in ts:
        point = p0 * (1 - t) ** 3 + 3 * p1 * t * (1 - t) ** 2 + 3 * p2 * t ** 2 * (1 - t) + p3 * t ** 3
        point = point.astype(int).tolist()
        if np.linalg.norm(np.subtract(point, prev)) < min_distance:
            continue

        points.append(point)
        prev = point

    # Удаляем слишком близкие точки
    if len(points[1:]):
        distance = np.linalg.norm(np.subtract(points[1:], points[0]), axis=1)
        mask = np.append(True, distance > min_distance)
        points = np.array(points)[mask].tolist()
        if len(points) <= 1:
            points = [p0, p3]
    else:
        points = [p0, p3]

    return points


class Command:
    def __init__(
            self,
            operation: str,
            contact: int = 0,
            x: int = 0,
            y: int = 0,
            ms: int = 10,
            pressure: int = 100,
            mode: int = 0,
            text: str = ''
    ):
        """
        minitouch 命令，参考 https://github.com/openstf/minitouch#writable-to-the-socket

        Args:
            operation: 操作类型，c/r/d/m/u/w。
            contact: 触点索引。
            x: X 坐标。
            y: Y 坐标。
            ms: 等待时间（毫秒）。
            pressure: 压力值。
            mode: 模式。
            text: 文本内容。
        """
        self.operation = operation
        self.contact = contact
        self.x = x
        self.y = y
        self.ms = ms
        self.pressure = pressure
        self.mode = mode
        self.text = text

    def to_minitouch(self) -> str:
        """转换为写入 minitouch socket 的字符串。"""
        if self.operation == 'c':
            return f'{self.operation}\n'
        elif self.operation == 'r':
            return f'{self.operation}\n'
        elif self.operation == 'd':
            return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'm':
            return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'u':
            return f'{self.operation} {self.contact}\n'
        elif self.operation == 'w':
            return f'{self.operation} {self.ms}\n'
        else:
            return ''

    def to_maatouch_sync(self):
        if self.operation == 'c':
            return f'{self.operation}\n'
        elif self.operation == 'r':
            if self.mode:
                return f'{self.operation} {self.mode}\n'
            else:
                return f'{self.operation}\n'
        elif self.operation == 'd':
            if self.mode:
                return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure} {self.mode}\n'
            else:
                return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'm':
            if self.mode:
                return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure} {self.mode}\n'
            else:
                return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'u':
            if self.mode:
                return f'{self.operation} {self.contact} {self.mode}\n'
            else:
                return f'{self.operation} {self.ms}\n'
        elif self.operation == 'w':
            return f'{self.operation} {self.ms}\n'
        elif self.operation == 's':
            return f'{self.operation} {self.text}\n'
        else:
            return ''

    def to_atx_agent(self, max_x=1280, max_y=720) -> str:
        """
        转换为发送到 atx-agent 的字典格式，$DEVICE_URL/minitouch。
        参考 https://github.com/openatx/atx-agent#minitouch%E6%93%8D%E4%BD%9C%E6%96%B9%E6%B3%95
        """
        x, y = self.x / max_x, self.y / max_y
        if self.operation == 'c':
            out = dict(operation=self.operation)
        elif self.operation == 'r':
            out = dict(operation=self.operation)
        elif self.operation == 'd':
            out = dict(operation=self.operation, index=self.contact, pressure=self.pressure, xP=x, yP=y)
        elif self.operation == 'm':
            out = dict(operation=self.operation, index=self.contact, pressure=self.pressure, xP=x, yP=y)
        elif self.operation == 'u':
            out = dict(operation=self.operation, index=self.contact)
        elif self.operation == 'w':
            out = dict(operation=self.operation, milliseconds=self.ms)
        else:
            out = dict()
        return json.dumps(out)


class CommandBuilder:
    """构建 minitouch 命令字符串。

    可用于自定义操作::

        with safe_connection(_DEVICE_ID) as connection:
            builder = CommandBuilder()
            builder.down(0, 400, 400, 50)
            builder.commit()
            builder.move(0, 500, 500, 50)
            builder.commit()
            builder.move(0, 800, 400, 50)
            builder.commit()
            builder.up(0)
            builder.commit()
            builder.publish(connection)

    """
    DEFAULT_DELAY = 0.05
    max_x = 1280
    max_y = 720

    def __init__(
            self,
            device,
            contact=0,
            handle_orientation=True,
    ):
        """
        Args:
            device: 设备实例。
        """
        self.device = device
        self.commands = []
        self.delay = 0
        self.contact = contact
        self.handle_orientation = handle_orientation

    @property
    def orientation(self):
        if self.handle_orientation:
            return self.device.orientation
        else:
            return 0

    def convert(self, x, y):
        max_x, max_y = self.device.max_x, self.device.max_y
        orientation = self.orientation

        if orientation == 0:
            pass
        elif orientation == 1:
            x, y = 720 - y, x
            max_x, max_y = max_y, max_x
        elif orientation == 2:
            x, y = 1280 - x, 720 - y
        elif orientation == 3:
            x, y = y, 1280 - x
            max_x, max_y = max_y, max_x
        else:
            raise ScriptError(f'Недопустимая ориентация устройства: {orientation}')

        self.max_x, self.max_y = max_x, max_y
        if not self.device.config.DEVICE_OVER_HTTP:
            # Максимальные координаты X и Y могут (хотя обычно не должны) совпадать с размером дисплея
            x, y = int(x / 1280 * max_x), int(y / 720 * max_y)
        else:
            # В HTTP-режиме max_x и max_y по умолчанию равны 1280 и 720; масштабирование под размер дисплея пропускаем
            x, y = int(x), int(y)
        return x, y

    def commit(self):
        """添加 minitouch 命令：'c\n'。"""
        self.commands.append(Command(
            'c'
        ))
        return self

    def reset(self, mode=0):
        """添加 minitouch 命令：'r\n'。"""
        self.commands.append(Command(
            'r', mode=mode
        ))
        return self

    def wait(self, ms=10):
        """添加 minitouch 命令：'w <ms>\n'。"""
        self.commands.append(Command(
            'w', ms=ms
        ))
        self.delay += ms
        return self

    def up(self, mode=0):
        """添加 minitouch 命令：'u <contact>\n'。"""
        self.commands.append(Command(
            'u', contact=self.contact, mode=mode
        ))
        return self

    def down(self, x, y, pressure=100, mode=0):
        """添加 minitouch 命令：'d <contact> <x> <y> <pressure>\n'。"""
        x, y = self.convert(x, y)
        self.commands.append(Command(
            'd', x=x, y=y, contact=self.contact, pressure=pressure, mode=mode
        ))
        return self

    def move(self, x, y, pressure=100, mode=0):
        """添加 minitouch 命令：'m <contact> <x> <y> <pressure>\n'。"""
        x, y = self.convert(x, y)
        self.commands.append(Command(
            'm', x=x, y=y, contact=self.contact, pressure=pressure, mode=mode
        ))
        return self

    def clear(self):
        """清空当前命令列表。"""
        self.commands = []
        self.delay = 0
        return self

    def to_minitouch(self) -> str:
        out = ''.join([command.to_minitouch() for command in self.commands])
        self._check_empty(out)
        return out

    def to_maatouch_sync(self) -> str:
        out = ''.join([command.to_maatouch_sync() for command in self.commands])
        self._check_empty(out)
        return out

    def to_atx_agent(self) -> List[str]:
        out = [command.to_atx_agent(self.max_x, self.max_y) for command in self.commands]
        self._check_empty(out)
        return out

    def send(self):
        return self.device.minitouch_send(builder=self)

    def _check_empty(self, text=None):
        """
        检查命令列表是否为空。有效的命令列表必须包含除提交和等待之外的操作。

        Returns:
            命令列表是否为空。
        """
        empty = True
        for command in self.commands:
            if command.operation not in ['c', 'w', 's']:
                empty = False
                break
        if empty:
            logger.warning(f'Список команд пуст; отправка может привести к неожиданному поведению: {text}')
        return empty


class MinitouchNotInstalledError(Exception):
    pass


class MinitouchOccupiedError(Exception):
    pass


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Minitouch):
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Необрабатываемая ошибка
            except RequestHumanTakeover:
                break
            # При остановке службы ADB
            except ConnectionResetError as e:
                logger.error(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, '_minitouch_builder')
            # Эмулятор выключен
            except ConnectionAbortedError as e:
                logger.error(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, '_minitouch_builder')
            # MinitouchNotInstalledError: от minitouch получены пустые данные
            except MinitouchNotInstalledError as e:
                logger.error(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.install_uiautomator2()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, '_minitouch_builder')
            # MinitouchOccupiedError: истекло время подключения к minitouch
            except MinitouchOccupiedError as e:
                logger.error(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    self.restart_atx()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, '_minitouch_builder')
            # Ошибка ADB
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                        if self._minitouch_port:
                            self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                        del_cached_property(self, '_minitouch_builder')
                elif handle_unknown_host_service(e):
                    def init():
                        self.adb_start_server()
                        self.adb_reconnect()
                        if self._minitouch_port:
                            self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                        del_cached_property(self, '_minitouch_builder')
                else:
                    break
            except BrokenPipeError as e:
                logger.error(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    del_cached_property(self, '_minitouch_builder')
            # Необрабатываемая ошибка — обязательно пробрасываем выше, чтобы запустить перезапуск эмулятора
            except EmulatorNotRunningError:
                raise
            # Неизвестное исключение, возможно повреждение изображения
            except Exception as e:
                logger.exception(str(f'[Устройство — minitouch] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in ['_minitouch_builder']:
            logger.critical(f'[Устройство — minitouch] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError

        logger.critical(f'[Устройство — minitouch] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class Minitouch(Connection):
    _minitouch_port: int = 0
    _minitouch_client: socket.socket = None
    _minitouch_process = None
    _minitouch_socket_file = None
    _minitouch_pid: int
    _minitouch_ws: websockets.WebSocketClientProtocol
    max_x: int
    max_y: int
    _minitouch_init_thread = None

    @cached_property
    @retry
    def _minitouch_builder(self):
        self.minitouch_init()
        return CommandBuilder(self)

    @property
    def minitouch_builder(self):
        # Ждём завершения потока инициализации
        if self._minitouch_init_thread is not None:
            self._minitouch_init_thread.join()
            del self._minitouch_init_thread
            self._minitouch_init_thread = None

        return self._minitouch_builder

    def early_minitouch_init(self):
        """
        在 Alas 实例开始截图时启动线程初始化 minitouch 连接。
        这将加速首次点击约 0.05 秒。
        """
        if has_cached_property(self, '_minitouch_builder'):
            return

        def early_minitouch_init_func():
            _ = self._minitouch_builder

        thread = threading.Thread(target=early_minitouch_init_func, daemon=True)
        self._minitouch_init_thread = thread
        thread.start()

    def _close_minitouch_transport(self):
        if self._minitouch_socket_file is not None:
            try:
                self._minitouch_socket_file.close()
            except Exception as e:
                logger.debug(f'[Устройство — minitouch] Ошибка закрытия потока протокола: {e}')
            self._minitouch_socket_file = None
        if self._minitouch_client is not None:
            try:
                self._minitouch_client.close()
            except Exception as e:
                logger.debug(f'[Устройство — minitouch] Ошибка закрытия клиента: {e}')
            self._minitouch_client = None
        if self._minitouch_process is not None:
            try:
                self._minitouch_process.close()
            except Exception as e:
                logger.debug(f'[Устройство — minitouch] Ошибка закрытия процесса: {e}')
            self._minitouch_process = None

    def release_resource(self):
        self._close_minitouch_transport()
        super().release_resource()

    @Config.when(DEVICE_OVER_HTTP=False)
    def minitouch_init(self):
        logger.hr('[Устройство — minitouch] Инициализация')
        max_x, max_y = 1280, 720
        max_contacts = 2
        max_pressure = 50

        self._close_minitouch_transport()

        self.get_orientation()

        # adb shell с stream=True удерживает процесс minitouch после закрытия
        # команды shell; фоновой команды через `&` недостаточно на Android.
        self._minitouch_process = self.adb.shell(
            [self.config.MINITOUCH_FILEPATH_REMOTE],
            stream=True,
        )
        self._minitouch_port = self.adb_forward("localabstract:minitouch")

        retry_timeout = Timer(2).start()
        while 1:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(('127.0.0.1', self._minitouch_port))
            self._minitouch_client = client

            # Получить служебные строки minitouch.
            socket_out = client.makefile()
            self._minitouch_socket_file = socket_out

            # v <version>
            # Версия протокола, обычно 1; использовать её не требуется
            try:
                out = socket_out.readline().replace("\n", "").replace("\r", "")
            except socket.timeout:
                self._close_minitouch_transport()
                raise MinitouchOccupiedError(
                    '[Устройство — minitouch] Истекло время подключения; вероятно, уже установлено другое соединение'
                )
            logger.info(out)

            # ^ <max-contacts> <max-x> <max-y> <max-pressure>
            out = socket_out.readline().replace("\n", "").replace("\r", "")
            logger.info(out)
            try:
                _, max_contacts, max_x, max_y, max_pressure, *_ = out.split(" ")
                break
            except ValueError:
                socket_out.close()
                self._minitouch_socket_file = None
                client.close()
                if retry_timeout.reached():
                    self._close_minitouch_transport()
                    raise MinitouchNotInstalledError(
                        '[Устройство — minitouch] Получены пустые данные; вероятно, minitouch не установлен'
                    )
                else:
                    # minitouch может запускаться не так быстро
                    self.sleep(1)
                    continue

        # self.max_contacts = max_contacts
        self.max_x = int(max_x)
        self.max_y = int(max_y)
        # self.max_pressure = max_pressure

        # $ <pid>
        out = socket_out.readline().replace("\n", "").replace("\r", "")
        logger.info(out)
        _, pid = out.split(" ")
        self._minitouch_pid = int(pid)

        logger.info(
            '[Устройство — minitouch] Порт: {}, PID: {}'.format(self._minitouch_port, self._minitouch_pid)
        )
        logger.info(
            '[Устройство — minitouch] max_contact: {}; max_x: {}; max_y: {}; max_pressure: {}'.format(max_contacts, max_x, max_y, max_pressure)
        )

    @Config.when(DEVICE_OVER_HTTP=False)
    def minitouch_send(self, builder: CommandBuilder):
        content = builder.to_minitouch()
        # logger.info("send operation: {}".format(content.replace("\n", "\\n")))
        byte_content = content.encode('utf-8')
        self._minitouch_client.sendall(byte_content)
        self._minitouch_client.recv(0)
        time.sleep(self.minitouch_builder.delay / 1000 + builder.DEFAULT_DELAY)
        builder.clear()

    @cached_property
    def _minitouch_loop(self):
        return asyncio.new_event_loop()

    def _minitouch_loop_run(self, event):
        """
        运行异步事件循环。

        Args:
            event: 异步函数。

        Raises:
            MinitouchOccupiedError: 连接被占用时抛出。
        """
        try:
            return self._minitouch_loop.run_until_complete(event)
        except websockets.ConnectionClosedError as e:
            # ConnectionClosedError: no close frame received or sent
            # ConnectionClosedError: sent 1011 (unexpected error) keepalive ping timeout; no close frame received
            logger.error(str(f'[Устройство — minitouch] Ошибка цикла управления: {e}'))
            raise MinitouchOccupiedError(
                '[Устройство — minitouch] Соединение закрыто; вероятно, уже установлено другое соединение'
            )

    @Config.when(DEVICE_OVER_HTTP=True)
    def minitouch_init(self):
        logger.hr('[Устройство — minitouch] Инициализация')
        self.max_x, self.max_y = 1280, 720
        self.get_orientation()

        logger.info('[Устройство — minitouch] Остановка службы minitouch')
        s = self.u2.service('minitouch')
        s.stop()
        while 1:
            if not s.running():
                break
            self.sleep(0.05)

        logger.info('[Устройство — minitouch] Запуск службы minitouch')
        s.start()
        while 1:
            if s.running():
                break
            self.sleep(0.05)

        # 'ws://127.0.0.1:7912/minitouch'
        url = re.sub(r"^https?://", 'ws://', self.serial) + '/minitouch'
        logger.attr('Адрес minitouch', url)

        async def connect():
            ws = await websockets.connect(url)
            # Запускаем службу @minitouch
            logger.info(await ws.recv())
            # Подключаемся к unix:@minitouch
            logger.info(await ws.recv())
            return ws

        self._minitouch_ws = self._minitouch_loop_run(connect())

    @Config.when(DEVICE_OVER_HTTP=True)
    def minitouch_send(self, builder: CommandBuilder):
        content = builder.to_atx_agent()

        async def send():
            for row in content:
                # logger.info("send operation: {}".format(row.replace("\n", "\\n")))
                await self._minitouch_ws.send(row)

        self._minitouch_loop_run(send())
        time.sleep(builder.delay / 1000 + builder.DEFAULT_DELAY)
        builder.clear()

    @retry
    def click_minitouch(self, x, y):
        builder = self.minitouch_builder
        builder.down(x, y).commit()
        builder.up().commit()
        builder.send()

    @retry
    def long_click_minitouch(self, x, y, duration=1.0):
        duration = int(duration * 1000)
        builder = self.minitouch_builder
        builder.down(x, y).commit().wait(duration)
        builder.up().commit()
        builder.send()

    @retry
    def swipe_minitouch(self, p1, p2):
        points = insert_swipe(p0=p1, p3=p2)
        builder = self.minitouch_builder

        builder.down(*points[0]).commit().wait(10)
        builder.send()

        for point in points[1:]:
            builder.move(*point).commit().wait(10)
        builder.send()

        builder.up().commit()
        builder.send()

    @retry
    def drag_minitouch(self, p1, p2, point_random=(-10, -10, 10, 10)):
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        points = insert_swipe(p0=p1, p3=p2, speed=20)
        builder = self.minitouch_builder

        builder.down(*points[0]).commit().wait(10)
        builder.send()

        for point in points[1:]:
            builder.move(*point).commit().wait(10)
        builder.send()

        builder.move(*p2).commit().wait(140)
        builder.move(*p2).commit().wait(140)
        builder.send()

        builder.up().commit()
        builder.send()

    def island_swipe_hold_minitouch(self, p1, p2, hold_time):
        points = insert_swipe(p0=p1, p3=p2)
        builder = self.minitouch_builder
        builder.down(*points[0]).commit().wait(10)
        builder.send()
        for point in points[1:]:
            builder.move(*point).commit().wait(10)
        builder.wait(hold_time)
        builder.send()
        builder.up().commit()
        builder.send()
