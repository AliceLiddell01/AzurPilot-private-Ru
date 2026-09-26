"""
Метод IPC-взаимодействия с эмулятором MuMu.

Взаимодействует с эмулятором MuMu напрямую через межпроцессное взаимодействие (IPC),
обеспечивая высокую производительность создания снимков экрана и сенсорного управления.
Загружает библиотеку nemu_ipc DLL эмулятора MuMu через ctypes в обход уровня ADB, обращаясь к внутренним
интерфейсам эмулятора с минимальной задержкой без сетевой передачи данных. Поддерживает создание снимков, нажатия, свайпы и клавиши.
Доступен только для платформы Windows при поддержке интерфейса IPC эмулятором MuMu.
"""
import ctypes
import json
import os
import sys
import time
from functools import wraps

import cv2
import numpy as np

from module.base.decorator import cached_property, del_cached_property, has_cached_property
from module.base.timer import Timer
from module.base.utils import ensure_time
from module.config.deep import deep_get
from module.device.env import IS_WINDOWS
from module.device.method.minitouch import insert_swipe, random_rectangle_point
from module.device.method.pool import JobTimeout, WORKER_POOL
from module.device.method.utils import RETRY_TRIES, retry_sleep
from module.device.platform import Platform
from module.exception import EmulatorNotRunningError, RequestHumanTakeover
from module.logger import logger


class NemuIpcIncompatible(Exception):
    pass


class NemuIpcError(Exception):
    pass


class CaptureStd:
    """
    Перехват stdout и stderr для библиотек Python и C.
    Справочно: https://stackoverflow.com/questions/5081657/how-do-i-prevent-a-c-shared-library-to-print-on-stdout-in-python/17954769

    ```
    with CaptureStd() as capture:
        # Фактический вывод подавляется
        print('whatever')
    # Перехваченный вывод доступен в capture.stdout
    print(f'Got stdout: "{capture.stdout}"')
    print(f'Got stderr: "{capture.stderr}"')
    ```
    """

    def __init__(self):
        self.stdout = b''
        self.stderr = b''

    def _redirect_stdout(self, to):
        sys.stdout.close()
        os.dup2(to, self.fdout)
        sys.stdout = os.fdopen(self.fdout, 'w')

    def _redirect_stderr(self, to):
        sys.stderr.close()
        os.dup2(to, self.fderr)
        sys.stderr = os.fdopen(self.fderr, 'w')

    def __enter__(self):
        self.fdout = sys.stdout.fileno()
        self.fderr = sys.stderr.fileno()
        self.reader_out, self.writer_out = os.pipe()
        self.reader_err, self.writer_err = os.pipe()
        self.old_stdout = os.dup(self.fdout)
        self.old_stderr = os.dup(self.fderr)

        file_out = os.fdopen(self.writer_out, 'w')
        file_err = os.fdopen(self.writer_err, 'w')
        self._redirect_stdout(to=file_out.fileno())
        self._redirect_stderr(to=file_err.fileno())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._redirect_stdout(to=self.old_stdout)
        self._redirect_stderr(to=self.old_stderr)
        os.close(self.old_stdout)
        os.close(self.old_stderr)

        self.stdout = self.recvall(self.reader_out)
        self.stderr = self.recvall(self.reader_err)
        os.close(self.reader_out)
        os.close(self.reader_err)

    @staticmethod
    def recvall(reader, length=1024) -> bytes:
        fragments = []
        while 1:
            chunk = os.read(reader, length)
            if chunk:
                fragments.append(chunk)
            else:
                break
        output = b''.join(fragments)
        return output


class CaptureNemuIpc(CaptureStd):
    instance = None

    def is_capturing(self):
        """
        Перехватывать только на самом верхнем уровне обертки, предотвращая вложенный перехват.
        Если перехват уже выполняется, текущий экземпляр не производит действий.
        """
        cls = self.__class__
        return isinstance(cls.instance, cls) and cls.instance != self

    def __enter__(self):
        if self.is_capturing():
            return self

        super().__enter__()
        CaptureNemuIpc.instance = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_capturing():
            return

        CaptureNemuIpc.instance = None
        super().__exit__(exc_type, exc_val, exc_tb)

        self.check_stdout()
        self.check_stderr()

    def check_stdout(self):
        if not self.stdout:
            return
        logger.info(f'[Устройство — NemuIpc] stdout NemuIpc: {self.stdout}')

    def check_stderr(self):
        if not self.stderr:
            return
        logger.error(f'[Устройство — NemuIpc] stderr NemuIpc: {self.stderr}')

        # Вызвана старая версия MuMu12
        # Проверено на 3.4.0
        # b'nemu_capture_display rpc error: 1783\r\n'
        # Проверено на 3.7.3
        # b'nemu_capture_display rpc error: 1745\r\n'
        if b'error: 1783' in self.stderr or b'error: 1745' in self.stderr:
            raise NemuIpcIncompatible(
                f'Для NemuIpc требуется MuMu12 версии >= 3.8.13. Проверьте версию')
        # Некорректный contact_id
        # b'nemu_capture_display cannot find rpc connection\r\n'
        if b'cannot find rpc connection' in self.stderr:
            raise NemuIpcError(self.stderr)
        # Эмулятор остановлен
        # b'nemu_capture_display rpc error: 1722\r\n'
        # MuMuVMMSVC.exe остановлен
        # b'nemu_capture_display rpc error: 1726\r\n'
        # Известного способа обработки пока нет
        if b'error: 1722' in self.stderr or b'error: 1726' in self.stderr:
            raise NemuIpcError('[Устройство — NemuIpc] Экземпляр эмулятора, вероятно, завершил работу')


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (NemuIpcImpl):
        """
        init = None
        for _ in range(RETRY_TRIES):
            # При повторной попытке увеличиваем тайм-аут
            if func.__name__ == 'screenshot':
                timeout = retry_sleep(_)
                if timeout > 0:
                    kwargs['timeout'] = timeout
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Не подлежит обработке
            except RequestHumanTakeover:
                break
            # Не подлежит обработке
            except NemuIpcIncompatible as e:
                logger.error(str(f'[Устройство — NemuIpc] Ошибка повторной попытки: {e}'))
                break
            # Тайм-аут вызова функции
            except JobTimeout:
                logger.warning(f'[Устройство — NemuIpc] Истёк тайм-аут вызова {func.__name__}(); повторная попытка: {_}')

                def init():
                    pass
            # NemuIpcError
            except NemuIpcError as e:
                logger.error(str(f'[Устройство — NemuIpc] Ошибка повторной попытки: {e}'))

                def init():
                    self.reconnect()
            # Не подлежит обработке — исключение нужно пробросить выше, чтобы инициировать перезапуск эмулятора
            except EmulatorNotRunningError:
                raise
            # Неизвестное исключение, возможно повреждено изображение
            except Exception as e:
                logger.exception(str(f'[Устройство — NemuIpc] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in ['connect_with_retry', 'screenshot', 'down', 'up']:
            logger.critical(f'[Устройство — NemuIpc] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError

        logger.critical(f'[Устройство — NemuIpc] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class NemuIpcImpl:
    def __init__(self, nemu_folder: str, instance_id: int, display_id: int = 0):
        """
        Args:
            nemu_folder: Путь к каталогу установки MuMu12, например E:/ProgramFiles/MuMuPlayer-12.0.
            instance_id: Идентификатор экземпляра эмулятора, начиная с 0.
            display_id: Всегда 0, если не включено поддержание работы в фоновом режиме.
        """
        self.nemu_folder: str = nemu_folder
        self.instance_id: int = instance_id
        self.display_id: int = display_id

        # Пытаемся загрузить DLL из нескольких путей
        list_dll = [
            # MuMuPlayer12
            os.path.abspath(os.path.join(nemu_folder, './shell/sdk/external_renderer_ipc.dll')),
            # MuMuPlayer12 5.0
            os.path.abspath(os.path.join(nemu_folder, './nx_device/12.0/shell/sdk/external_renderer_ipc.dll')),
            # MuMuPlayer12 6.0
            os.path.abspath(os.path.join(nemu_folder, './nx_main/sdk/external_renderer_ipc.dll')),
        ]
        self.lib = None
        for ipc_dll in list_dll:
            if not os.path.exists(ipc_dll):
                continue
            try:
                self.lib = ctypes.CDLL(ipc_dll)
                break
            except OSError as e:
                logger.error(str(f'[Устройство — NemuIpc] Ошибка инициализации backend: {e}'))
                logger.error(f'Файл ipc_dll={ipc_dll} существует, но его не удалось загрузить')
                continue
        if self.lib is None:
            # Не найдено
            raise NemuIpcIncompatible(
                f'Для NemuIpc требуется MuMu12 версии >= 3.8.13. Проверьте версию. '
                f'Ни один из следующих путей не существует: {list_dll}')
        # Успешно
        logger.info(
            f'[Устройство — NemuIpc] Инициализация: каталог MuMu={nemu_folder}, библиотека IPC={ipc_dll}, ID экземпляра={instance_id}, ID дисплея={display_id}'
        )
        self.connect_id: int = 0
        self.width = 0
        self.height = 0

    def connect(self, on_thread=True):
        if self.connect_id > 0:
            return

        if on_thread:
            connect_id = self.run_func(
                self.lib.nemu_connect,
                self.nemu_folder, self.instance_id
            )
        else:
            connect_id = self.lib.nemu_connect(self.nemu_folder, self.instance_id)
        if connect_id == 0:
            raise NemuIpcError(
                '[Устройство — NemuIpc] Не удалось подключиться. Проверьте путь nemu_folder и убедитесь, что эмулятор запущен'
            )

        self.connect_id = connect_id
        # logger.info(f'NemuIpc connected: {self.connect_id}')

    @retry
    def connect_with_retry(self, on_thread=True):
        self.connect(on_thread=on_thread)

    def disconnect(self):
        if self.connect_id == 0:
            return

        self.run_func(
            self.lib.nemu_disconnect,
            self.connect_id
        )

        # logger.info(f'NemuIpc disconnected: {self.connect_id}')
        self.connect_id = 0

    def reconnect(self):
        self.disconnect()
        self.connect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    @staticmethod
    def run_func(func, *args, on_thread=True, timeout=0.5):
        """
        Args:
            func: Вызываемая синхронная функция.
            *args:
            on_thread: Если True, запускать func в отдельном потоке.
            timeout:

        Raises:
            JobTimeout: Вызывается при превышении времени ожидания выполнения функции.
            NemuIpcIncompatible:
            NemuIpcError
        """
        if on_thread:
            # nemu_ipc иногда зависает по тайм-ауту, поэтому запускаем его в отдельном потоке
            job = WORKER_POOL.start_thread_soon(func, *args)
            result = job.get_or_kill(timeout)
        else:
            result = func(*args)

        err = False
        if func.__name__ == '_screenshot':
            pass
        elif func.__name__ == 'nemu_connect':
            if result == 0:
                err = True
        else:
            if result > 0:
                err = True
        # Получаем фактическую информацию об ошибке из стандартного вывода
        if err:
            logger.warning(f'Не удалось выполнить {func.__name__}, result={result}')
            with CaptureNemuIpc():
                func(*args)

        return result

    def get_resolution(self, on_thread=True):
        """
        Получить разрешение эмулятора; устанавливает `self.width` и `self.height`.
        """
        if self.connect_id == 0:
            self.connect()

        width_ptr = ctypes.pointer(ctypes.c_int(0))
        height_ptr = ctypes.pointer(ctypes.c_int(0))
        nullptr = ctypes.POINTER(ctypes.c_int)()

        ret = self.run_func(
            self.lib.nemu_capture_display,
            self.connect_id, self.display_id, 0, width_ptr, height_ptr, nullptr,
            on_thread=on_thread
        )
        if ret > 0:
            raise NemuIpcError('[Устройство — NemuIpc] Вызов nemu_capture_display завершился ошибкой при получении разрешения')
        self.width = width_ptr.contents.value
        self.height = height_ptr.contents.value

    def _screenshot(self):
        if self.connect_id == 0:
            self.connect(on_thread=False)
        self.get_resolution(on_thread=False)

        width_ptr = ctypes.pointer(ctypes.c_int(self.width))
        height_ptr = ctypes.pointer(ctypes.c_int(self.height))
        length = self.width * self.height * 4
        pixels_pointer = ctypes.pointer((ctypes.c_ubyte * length)())

        ret = self.lib.nemu_capture_display(
            self.connect_id, self.display_id, length, width_ptr, height_ptr, pixels_pointer,
        )
        if ret > 0:
            raise NemuIpcError('[Устройство — NemuIpc] Вызов nemu_capture_display завершился ошибкой при создании снимка экрана')

        # Возвращаем pixels_pointer вместо image, чтобы не передавать объект изображения через job
        return pixels_pointer

    @retry
    def screenshot(self, timeout=0.5):
        """
        Args:
            timeout: Время ожидания вызова nemu_ipc (в секундах).
                Динамически увеличивается декоратором `@retry`.

        Returns:
            np.ndarray: Массив изображения в цветовом пространстве RGBA.
                Обратите внимание: изображение перевернуто по вертикали.
        """
        if self.connect_id == 0:
            self.connect()

        pixels_pointer = self.run_func(self._screenshot, timeout=timeout)

        # image = np.ctypeslib.as_array(pixels_pointer, shape=(self.height, self.width, 4))
        image = np.ctypeslib.as_array(pixels_pointer.contents).reshape((self.height, self.width, 4))
        return image

    def convert_xy(self, x, y):
        """
        Преобразовать стандартные координаты ADB в координаты Nemu.
        Перед вызовом этого метода необходимо обновить `self.height`.

        Returns:
            int, int
        """
        x, y = int(x), int(y)
        x, y = self.height - y, x
        return x, y

    @retry
    def down(self, x, y):
        """
        Нажатие касания; последовательные нажатия воспринимаются как свайп.
        """
        if self.connect_id == 0:
            self.connect()
        if self.height == 0:
            self.get_resolution()

        x, y = self.convert_xy(x, y)

        ret = self.run_func(
            self.lib.nemu_input_event_touch_down,
            self.connect_id, self.display_id, x, y
        )
        if ret > 0:
            raise NemuIpcError('[Устройство — NemuIpc] Вызов nemu_input_event_touch_down завершился ошибкой')

    @retry
    def up(self):
        """
        Отпускание касания.
        """
        if self.connect_id == 0:
            self.connect()

        ret = self.run_func(
            self.lib.nemu_input_event_touch_up,
            self.connect_id, self.display_id
        )
        if ret > 0:
            raise NemuIpcError('[Устройство — NemuIpc] Вызов nemu_input_event_touch_up завершился ошибкой')

    @staticmethod
    def serial_to_id(serial: str):
        """
        Определить ID экземпляра по серийному номеру.
        Примеры:
            "127.0.0.1:16384" -> 0
            "127.0.0.1:16416" -> 1
            Порты от 16414 до 16418 -> 1

        Returns:
            int: instance_id, или None при неудачном определении.
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


class NemuIpc(Platform):
    _screenshot_interval = Timer(0.1)

    @cached_property
    def nemu_ipc(self) -> NemuIpcImpl:
        """
        Инициализировать реализацию nemu ipc.
        """
        # В первую очередь используем существующие настройки
        if self.config.EmulatorInfo_path:
            folder = os.path.abspath(os.path.join(self.config.EmulatorInfo_path, '../../'))
            index = NemuIpcImpl.serial_to_id(self.serial)
            if index is not None:
                try:
                    return NemuIpcImpl(
                        nemu_folder=folder,
                        instance_id=index,
                        display_id=0
                    ).__enter__()
                except (NemuIpcIncompatible, NemuIpcError, JobTimeout) as e:
                    logger.error(str(f'[Устройство — NemuIpc] Ошибка получения снимка экрана: {e}'))
                    logger.error('[Устройство — NemuIpc] Некорректные сведения об эмуляторе')

        # Ищем экземпляр эмулятора
        # Например: E:\ProgramFiles\MuMuPlayer-12.0\shell\MuMuPlayer.exe
        # Путь установки: E:\ProgramFiles\MuMuPlayer-12.0
        if self.emulator_instance is None:
            logger.error('[Устройство — NemuIpc] NemuIpc недоступен: экземпляр эмулятора не найден')
            raise RequestHumanTakeover
        if 'MuMuPlayerGlobal' in self.emulator_instance.path:
            logger.info(f'[Устройство — NemuIpc] nemu_ipc недоступен в MuMuPlayerGlobal: {self.emulator_instance.path}')
            raise RequestHumanTakeover
        try:
            impl = NemuIpcImpl(
                nemu_folder=self.emulator_instance.emulator.abspath('../'),
                instance_id=self.emulator_instance.MuMuPlayer12_id,
                display_id=0
            )
            impl.connect_with_retry()
            return impl
        except (NemuIpcIncompatible, NemuIpcError, JobTimeout) as e:
            logger.error(str(f'[Устройство — NemuIpc] Ошибка получения снимка экрана: {e}'))
            logger.error('[Устройство — NemuIpc] Не удалось инициализировать NemuIpc')
            raise RequestHumanTakeover

    def nemu_ipc_available(self) -> bool:
        if not IS_WINDOWS:
            return False
        if not self.is_mumu_family:
            return False
        if self.nemud_player_version == '':
            # У версий >= 4.0 информация отсутствует в getprop
            # Пытаемся инициализировать nemu_ipc для окончательной проверки
            pass
        else:
            # Информация о версии есть: возможно, это MuMu6 или MuMu12 3.x
            if self.nemud_app_keep_alive == '':
                # Свойство пустое: возможно, это MuMu6 или MuMu12 < 3.5.6
                return False
        try:
            _ = self.nemu_ipc
        except RequestHumanTakeover:
            return False
        return True

    @staticmethod
    def check_mumu_app_keep_alive_400(file):
        """
        Проверить app_keep_alive в конфигурации эмулятора для версий >= 4.0.

        Args:
            file: E:/ProgramFiles/MuMuPlayer-12.0/vms/MuMuPlayer-12.0-1/config/customer_config.json

        Returns:
            bool: Успешно ли прочитан файл.
        """
        # Например: E:\ProgramFiles\MuMuPlayer-12.0\shell\MuMuPlayer.exe
        # Путь конфигурации: E:\ProgramFiles\MuMuPlayer-12.0\vms\MuMuPlayer-12.0-1\config\customer_config.json
        try:
            with open(file, mode='r', encoding='utf-8') as f:
                s = f.read()
                data = json.loads(s)
        except FileNotFoundError:
            logger.warning(f'[Устройство — NemuIpc] Не удалось выполнить check_mumu_app_keep_alive: файл {file} не существует')
            return False
        value = deep_get(data, keys='customer.app_keptlive', default=None)
        logger.debug('[Устройство — NemuIpc] customer.app_keptlive=%r', value)
        if str(value).lower() == 'true':
            # https://mumu.163.com/help/20230802/35047_1102450.html
            logger.critical('[Устройство — NemuIpc] Отключите «Сохранять работу в фоне» в настройках эмулятора MuMu')
            raise RequestHumanTakeover
        return True

    def check_mumu_app_keep_alive(self):
        if not self.is_mumu_over_version_400:
            return super().check_mumu_app_keep_alive()

        # В первую очередь используем существующие настройки
        if self.config.EmulatorInfo_path:
            index = NemuIpcImpl.serial_to_id(self.serial)
            if index is not None:
                file = os.path.abspath(os.path.join(
                    self.config.EmulatorInfo_path, f'../../vms/MuMuPlayer-12.0-{index}/configs/customer_config.json'))
                if self.check_mumu_app_keep_alive_400(file):
                    return True

        # Ищем экземпляр эмулятора
        if self.emulator_instance is None:
            logger.warning('[Устройство — NemuIpc] Не удалось выполнить check_mumu_app_keep_alive: emulator_instance имеет значение None')
            return False
        name = self.emulator_instance.name
        file = self.emulator_instance.mumu_vms_config('customer_config.json')
        if self.check_mumu_app_keep_alive_400(file):
            return True

        return False

    def nemu_ipc_release(self):
        if has_cached_property(self, 'nemu_ipc'):
            self.nemu_ipc.disconnect()
        del_cached_property(self, 'nemu_ipc')
        logger.info('[Устройство — NemuIpc] Ресурсы nemu_ipc освобождены')

    def screenshot_nemu_ipc(self):
        image = self.nemu_ipc.screenshot()

        image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
        cv2.flip(image, 0, dst=image)
        return image

    def click_nemu_ipc(self, x, y):
        down = ensure_time((0.010, 0.020))
        self.nemu_ipc.down(x, y)
        self.sleep(down)
        self.nemu_ipc.up()
        self.sleep(0.050 - down)

    def long_click_nemu_ipc(self, x, y, duration=1.0):
        self.nemu_ipc.down(x, y)
        self.sleep(duration)
        self.nemu_ipc.up()
        self.sleep(0.050)

    def swipe_nemu_ipc(self, p1, p2):
        points = insert_swipe(p0=p1, p3=p2)

        for point in points:
            self.nemu_ipc.down(*point)
            self.sleep(0.010)

        self.nemu_ipc.up()
        self.sleep(0.050)

    def drag_nemu_ipc(self, p1, p2, point_random=(-10, -10, 10, 10)):
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        points = insert_swipe(p0=p1, p3=p2, speed=20)

        for point in points:
            self.nemu_ipc.down(*point)
            self.sleep(0.010)

        self.nemu_ipc.down(*p2)
        self.sleep(0.140)
        self.nemu_ipc.down(*p2)
        self.sleep(0.140)

        self.nemu_ipc.up()
        self.sleep(0.050)
