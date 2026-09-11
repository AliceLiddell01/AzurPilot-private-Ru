"""雷电模拟器 OpenGL 截图后端。通过 LDPlayer 的原生 OpenGL 接口
直接读取渲染缓冲区，实现低延迟高质量截图。"""

import ctypes
import os
import subprocess
import time
from dataclasses import dataclass
from functools import wraps

import cv2
import numpy as np

from module.base.decorator import cached_property
from module.device.env import IS_WINDOWS
from module.device.method.utils import RETRY_TRIES, get_serial_pair, retry_sleep
from module.device.platform import Platform
from module.exception import RequestHumanTakeover
from module.logger import logger


class LDOpenGLIncompatible(Exception):
    pass


class LDOpenGLError(Exception):
    pass


def bytes_to_str(b: bytes) -> str:
    for encoding in ['utf-8', 'gbk']:
        try:
            return b.decode(encoding)
        except UnicodeDecodeError:
            pass
    return str(b)


@dataclass
class DataLDPlayerInfo:
    # Индекс экземпляра эмулятора, начиная с 0
    index: int
    # Имя экземпляра
    name: str
    # Дескриптор окна верхнего уровня
    topWnd: int
    # Дескриптор привязанного окна
    bndWnd: int
    # Запущен ли экземпляр: 1 — да, 0 — нет
    sysboot: int
    # PID процесса экземпляра; -1, если не запущен
    playerpid: int
    # PID процесса vbox; -1, если не запущен
    vboxpid: int
    # Разрешение
    width: int
    height: int
    dpi: int

    def __post_init__(self):
        self.index = int(self.index)
        self.name = bytes_to_str(self.name)
        self.topWnd = int(self.topWnd)
        self.bndWnd = int(self.bndWnd)
        self.sysboot = int(self.sysboot)
        self.playerpid = int(self.playerpid)
        self.vboxpid = int(self.vboxpid)
        self.width = int(self.width)
        self.height = int(self.height)
        self.dpi = int(self.dpi)


class LDConsole:
    def __init__(self, ld_folder: str):
        """
        Args:
            ld_folder: 雷电模拟器安装路径，例如 E:/ProgramFiles/LDPlayer9，
                该目录下应包含 `ldconsole.exe`。
        """
        self.ld_console = os.path.abspath(os.path.join(ld_folder, './ldconsole.exe'))

    def subprocess_run(self, cmd, timeout=10):
        """
        Args:
            cmd (list):
            timeout (int):

        Returns:
            bytes:
        """
        cmd = [self.ld_console] + cmd
        logger.info(f'[Устройство — LDOpenGL] Выполнение команды: {cmd}')

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=False)
        except FileNotFoundError as e:
            logger.warning(f'[Устройство — LDOpenGL] Предупреждение при выполнении {cmd}: {str(e)}')
            raise LDOpenGLIncompatible(f'ld_folder does not have ldconsole.exe')
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            logger.warning(f'[Устройство — LDOpenGL] Истёк тайм-аут команды {cmd}, stdout={stdout}, stderr={stderr}')
        return stdout

    def list2(self):
        """
        > ldconsole.exe list2
        0,雷电模拟器,28053900,42935798,1,59776,36816,1280,720,240
        1,雷电模拟器-1,0,0,0,-1,-1,1280,720,240

        Returns:
            list[DataLDPlayerInfo]:
        """
        out = []
        data = self.subprocess_run(['list2'])
        for row in data.strip().split(b'\n'):
            row = row.strip()
            if not row:
                continue
            info = row.split(b',')
            # Проверяем количество полей
            if len(info) != 10:
                logger.warning(f'Сведения об экземпляре LDPlayer содержат менее 10 частей: «{row}»')
                continue
            # Формируем сведения
            try:
                info = DataLDPlayerInfo(*info)
            except Exception as e:
                logger.warning(f'Не удалось сформировать сведения об экземпляре LDPlayer «{row}»: {e}')
            out.append(info)
        return out


class IScreenShotClass:
    def __init__(self, ptr):
        self.ptr = ptr

        # Определяем внутри класса, поскольку ctypes.WINFUNCTYPE доступен только в Windows
        cap_type = ctypes.WINFUNCTYPE(ctypes.c_void_p)
        release_type = ctypes.WINFUNCTYPE(None)
        self.class_cap = cap_type(1, "IScreenShotClass_Cap")
        # Удерживаем ссылку, чтобы при __del__ IScreenShotClass_Cap не оказался пустым
        self.class_release = release_type(2, "IScreenShotClass_Release")

    def cap(self):
        return self.class_cap(self.ptr)

    def __del__(self):
        self.class_release(self.ptr)


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (NemuIpcImpl):
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Не обрабатывается
            except RequestHumanTakeover:
                break
            # Не обрабатывается
            except LDOpenGLIncompatible as e:
                logger.error(str(f'[Устройство — LDOpenGL] Ошибка повторной попытки: {e}'))
                break
            # LDOpenGLError
            except LDOpenGLError as e:
                logger.error(str(f'[Устройство — LDOpenGL] Ошибка повторной попытки: {e}'))

                def init():
                    pass
            # Неизвестное исключение; возможно повреждено изображение
            except Exception as e:
                logger.exception(str(f'[Устройство — LDOpenGL] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        logger.critical(f'[Устройство — LDOpenGL] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class LDOpenGLImpl:
    def __init__(self, ld_folder: str, instance_id: int):
        """
        Args:
            ld_folder: 雷电模拟器安装路径，例如 E:/ProgramFiles/LDPlayer9
            instance_id: 模拟器实例 ID，从 0 开始
        """
        ldopengl_dll = os.path.abspath(os.path.join(ld_folder, './ldopengl64.dll'))
        logger.info(
            f'[Устройство — LDOpenGL] Инициализация: каталог LDPlayer={ld_folder}, библиотека LDOpenGL={ldopengl_dll}, ID экземпляра={instance_id}'
        )
        # Загружаем DLL
        try:
            self.lib = ctypes.WinDLL(ldopengl_dll)
        except OSError as e:
            logger.error(str(f'[Устройство — LDOpenGL] Ошибка инициализации backend: {e}'))
            if not os.path.exists(ldopengl_dll):
                raise LDOpenGLIncompatible(
                    f'ldopengl_dll={ldopengl_dll} не существует; '
                    f'для ldopengl требуется LDPlayer >= 9.0.78. Проверьте версию'
                )
            else:
                raise LDOpenGLIncompatible(
                    f'ldopengl_dll={ldopengl_dll} существует, '
                    f'но не может быть загружен'
                )
        # После загрузки DLL получаем сведения; наличие DLL таким образом служит проверкой версии
        self.console = LDConsole(ld_folder)
        self.info = self.get_player_info_by_index(instance_id)

        self.lib.CreateScreenShotInstance.restype = ctypes.c_void_p

        # Получаем экземпляр для снимков экрана
        instance_ptr = ctypes.c_void_p(self.lib.CreateScreenShotInstance(instance_id, self.info.playerpid))
        self.screenshot_instance = IScreenShotClass(instance_ptr)

    def get_player_info_by_index(self, instance_id: int):
        """
        Args:
            instance_id:

        Returns:
            DataLDPlayerInfo:

        Raises:
            LDOpenGLError:
        """
        for info in self.console.list2():
            if info.index == instance_id:
                logger.info(f'[Устройство — LDOpenGL] Найден экземпляр LDPlayer: {info}')
                if not info.sysboot:
                    raise LDOpenGLError('[Устройство — LDPlayer] Попытка подключения к экземпляру LDPlayer, но эмулятор не запущен')
                return info
        raise LDOpenGLError(f'[Устройство — LDPlayer] Не найден экземпляр с индексом {instance_id}')

    @retry
    def screenshot(self):
        """
        Returns:
            np.ndarray: BGR 色彩空间的图像数组。
                注意图像是上下颠倒的。
        """
        width, height = self.info.width, self.info.height

        img_ptr = self.screenshot_instance.cap()
        # ValueError: обращение к нулевому указателю
        if img_ptr is None:
            raise LDOpenGLError('[Устройство — LDOpenGL] Указатель изображения равен null')

        img = ctypes.cast(img_ptr, ctypes.POINTER(ctypes.c_ubyte * (height * width * 3))).contents

        image = np.ctypeslib.as_array(img).reshape((height, width, 3))
        return image

    @staticmethod
    def serial_to_id(serial: str):
        """
        从 serial 推断实例 ID。
        例如:
            "127.0.0.1:5555" -> 0
            "127.0.0.1:5557" -> 1
            "emulator-5554" -> 0

        Returns:
            int: instance_id，推断失败时返回 None
        """
        serial, _ = get_serial_pair(serial)
        if serial is None:
            return None
        try:
            port = int(serial.split(':')[1])
        except (IndexError, ValueError):
            return None
        if 5555 <= port <= 5555 + 32:
            return int((port - 5555) // 2)
        return None


class LDOpenGL(Platform):
    @cached_property
    def ldopengl(self):
        """
        初始化 ldopengl 实现。
        """
        # В первую очередь используем уже имеющиеся настройки
        if self.config.EmulatorInfo_path:
            folder = os.path.abspath(os.path.join(self.config.EmulatorInfo_path, '../'))
            index = LDOpenGLImpl.serial_to_id(self.serial)
            if index is not None:
                try:
                    return LDOpenGLImpl(
                        ld_folder=folder,
                        instance_id=index,
                    )
                except (LDOpenGLIncompatible, LDOpenGLError) as e:
                    logger.error(str(f'[Устройство — LDOpenGL] Ошибка получения снимка экрана: {e}'))
                    logger.error('[Устройство — LDOpenGL] Некорректные сведения об эмуляторе')

        # Ищем экземпляр эмулятора
        # Например: E:/ProgramFiles/LDPlayer9/dnplayer.exe
        # Путь установки: E:/ProgramFiles/LDPlayer9
        if self.emulator_instance is None:
            logger.error('[Устройство — LDOpenGL] LDOpenGL недоступен: экземпляр эмулятора не найден')
            raise RequestHumanTakeover
        try:
            return LDOpenGLImpl(
                ld_folder=self.emulator_instance.emulator.abspath('./'),
                instance_id=self.emulator_instance.LDPlayer_id,
            )
        except (LDOpenGLIncompatible, LDOpenGLError) as e:
            logger.error(str(f'[Устройство — LDOpenGL] Ошибка получения снимка экрана: {e}'))
            logger.error('[Устройство — LDOpenGL] Не удалось инициализировать LDOpenGL')
            raise RequestHumanTakeover

    def ldopengl_available(self) -> bool:
        if not IS_WINDOWS:
            return False
        if not self.is_ldplayer_bluestacks_family:
            return False
        logger.attr('Сведения об эмуляторе', self.config.EmulatorInfo_Emulator)
        if self.config.EmulatorInfo_Emulator not in ['LDPlayer9', 'LDPlayer14']:
            return False

        try:
            _ = self.ldopengl
        except RequestHumanTakeover:
            return False
        return True

    def screenshot_ldopengl(self):
        image = self.ldopengl.screenshot()

        # Порядок пикселей в данных указателя отличается: положительное направление оси y вверх, поэтому сначала отражаем по вертикали
        image = cv2.flip(image, 0)

        # Обработка ориентации унифицирована в _handle_orientated_image() из screenshot.py, чтобы избежать повторного поворота

        # Преобразуем цветовое пространство из BGR в RGB
        cv2.cvtColor(image, cv2.COLOR_BGR2RGB, dst=image)
        return image
