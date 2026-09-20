"""Модуль снимков экрана устройства.

Управляет всеми бэкендами захвата экрана (ADB, ADB_nc, uiautomator2, aScreenCap, DroidCast,
scrcpy, nemu_ipc, ldopengl), обеспечивает снятие снимков, проверку разрешения, обнаружение чёрного экрана,
сохранение снимков и другие функции. Содержит фоновый поток кодирования для сериализации изображений
в Base64 для отображения в WebUI.
"""
import os
import time
from collections import deque
from PIL import Image
# Этот файл определяет логику обработки снимков экрана.
# Управляет различными способами захвата и содержит фоновый поток кодирования для сериализации изображений в Base64 для live-preview WebUI.
import base64
import threading
import queue as _queue

import cv2
import numpy as np

from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import get_color, image_size, limit_in, save_image, set_template_match_non_native_720p
from module.config.time_source import now as current_time
from module.device.method.adb import Adb
from module.device.method.ascreencap import AScreenCap
from module.device.method.droidcast import DroidCast
from module.device.method.ldopengl import LDOpenGL
from module.device.method.nemu_ipc import NemuIpc
from module.device.method.scrcpy import Scrcpy
from module.device.method.wsa import WSA
from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger

class Screenshot(Adb, WSA, DroidCast, AScreenCap, Scrcpy, NemuIpc, LDOpenGL):
    """Диспетчер снимков экрана устройства.

    Объединяет все бэкенды снимков через множественное наследование, автоматически
    распределяя вызовы по настроенному Emulator_ScreenshotMethod. Предоставляет единый
    интерфейс захвата, масштабирования разрешения, сглаживания дизеринга, проверки чёрного экрана,
    сохранения снимков и управления интервалом.

    Attributes:
        image (np.ndarray): Последний снимок экрана в формате RGB numpy array.
        _screen_size_checked (bool): Пройдена ли проверка разрешения экрана.
        _screen_black_checked (bool): Пройдена ли проверка на чёрный экран.
        _screenshot_interval (Timer): Таймер интервала снимков экрана.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    _screen_size_checked = False
    _screen_black_checked = False
    _minicap_uninstalled = False
    _screenshot_interval = Timer(0.1)
    _last_save_time = {}
    image: np.ndarray

    @cached_property
    def screenshot_methods(self):
        """Возвращает словарь соответствия имени метода захвата его реализации.

        Returns:
            dict[str, Callable]: Ключ — имя метода снимка (например, 'ADB', 'DroidCast'),
                значение — соответствующий метод захвата.
        """
        return {
            'ADB': self.screenshot_adb,
            'ADB_nc': self.screenshot_adb_nc,
            'uiautomator2': self.screenshot_uiautomator2,
            'aScreenCap': self.screenshot_ascreencap,
            'aScreenCap_nc': self.screenshot_ascreencap_nc,
            'DroidCast': self.screenshot_droidcast,
            'DroidCast_raw': self.screenshot_droidcast_raw,
            'scrcpy': self.screenshot_scrcpy,
            'nemu_ipc': self.screenshot_nemu_ipc,
            'ldopengl': self.screenshot_ldopengl,
        }

    @cached_property
    def screenshot_method_override(self) -> str:
        """Переопределение метода снимка; подклассы могут переопределить это свойство для принудительного использования метода.

        Returns:
            str: Имя переопределённого метода снимка; пустая строка означает использование метода из конфигурации.
        """
        return ''

    def screenshot(self):
        """Делает снимок экрана.

        Returns:
            np.ndarray: Изображение снимка экрана.
        """
        self._screenshot_interval.wait()
        self._screenshot_interval.reset()

        for _ in range(2):
            if self.screenshot_method_override:
                method = self.screenshot_method_override
            else:
                method = self.config.Emulator_ScreenshotMethod
            method = self.screenshot_methods.get(method, self.screenshot_adb)

            self.image = method()

            width, height = image_size(self.image)
            set_template_match_non_native_720p(width != 1280 or height != 720, resolution=(width, height))
            if width != 1280 or height != 720:
                self.image = self.resize_screenshot_to_720p(self.image)

            if self.config.Emulator_ScreenshotDedithering:
                # Эта операция занимает примерно 40–60 мс
                cv2.fastNlMeansDenoising(self.image, self.image, h=17, templateWindowSize=1, searchWindowSize=2)
            self.image = self._handle_orientated_image(self.image)

            if self.config.Error_SaveError:
                self.screenshot_deque.append({'time': current_time(), 'image': self.image})

            if self.check_screen_size() and self.check_screen_black():
                break
            else:
                continue

        return self.image

    @staticmethod
    def resize_screenshot_to_720p(image):
        """Нормализует снимок экрана к пространству ресурсов Alas 1280x720.

        Протестировано на разрешениях эмулятора MuMu 1600x900, 1920x1080, 2560x1440 и 3840x2160.
        Использует кубический даунскейлинг со смешиванием лёгкого размытия по Гауссу для максимального приближения к нативному 720p.
        """
        image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_CUBIC)
        blur = cv2.GaussianBlur(image, (0, 0), sigmaX=1.0, sigmaY=1.0)
        return cv2.addWeighted(image, 0.90, blur, 0.10, 0)

    @property
    def has_cached_image(self):
        """Проверяет наличие кэшированного снимка экрана.

        Returns:
            bool: True, если существует непустое кэшированное изображение.
        """
        return hasattr(self, 'image') and self.image is not None

    def _handle_orientated_image(self, image):
        """Обрабатывает поворот изображения снимка экрана.

        Args:
            image: Исходное изображение.

        Returns:
            Обработанное изображение.
        """
        width, height = image_size(self.image)
        if width == 1280 and height == 720:
            return image

        # Поворачиваем снимок только при разрешении, отличном от 1280x720
        if self.orientation == 0:
            pass
        elif self.orientation == 1:
            image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif self.orientation == 2:
            image = cv2.rotate(image, cv2.ROTATE_180)
        elif self.orientation == 3:
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        else:
            raise ScriptError(f'Недопустимая ориентация устройства: {self.orientation}')

        return image

    @cached_property
    def screenshot_deque(self):
        """Возвращает двустороннюю очередь для сохранения истории снимков при диагностике ошибок.

        Размер очереди задаётся параметром Error_ScreenshotLength и ограничен диапазоном 1~400.

        Returns:
            deque: Очередь словарей вида {'time': datetime, 'image': np.ndarray}.
        """
        try:
            length = int(self.config.Error_ScreenshotLength)
        except ValueError:
            logger.error(f'[Устройство — снимок] Error_ScreenshotLength={self.config.Error_ScreenshotLength} не является целым числом')
            raise RequestHumanTakeover
        # Ограничиваем диапазоном 1–400
        length = max(1, min(length, 400))
        return deque(maxlen=length)

    def save_screenshot(self, genre='items', interval=None, to_base_folder=False):
        """Сохраняет снимок экрана с использованием миллисекундной метки времени в качестве имени файла.

        Args:
            genre: Категория снимка экрана.
            interval: Минимальный интервал между сохранениями (в секундах). Сохранения внутри интервала пропускаются.
            to_base_folder: Сохранять ли в базовую директорию.

        Returns:
            bool: True при успешном сохранении.
        """
        now = time.time()
        if interval is None:
            interval = self.config.SCREEN_SHOT_SAVE_INTERVAL

        if now - self._last_save_time.get(genre, 0) > interval:
            fmt = 'png'
            file = '%s.%s' % (int(now * 1000), fmt)

            folder = self.config.SCREEN_SHOT_SAVE_FOLDER_BASE if to_base_folder else self.config.SCREEN_SHOT_SAVE_FOLDER
            folder = os.path.join(folder, genre)
            if not os.path.exists(folder):
                os.mkdir(folder)

            file = os.path.join(folder, file)
            self.image_save(file)
            self._last_save_time[genre] = now
            return True
        else:
            self._last_save_time[genre] = now
            return False

    def screenshot_last_save_time_reset(self, genre):
        """Сбрасывает метку времени сохранения для указанной категории снимков, разрешая немедленное сохранение.

        Args:
            genre (str): Имя категории снимков.
        """
        self._last_save_time[genre] = 0

    def screenshot_interval_set(self, interval=None):
        """Устанавливает интервал между снимками экрана.

        Args:
            interval: Минимальный интервал между двумя снимками (в секундах).
                None означает использование Optimization_ScreenshotInterval,
                'combat' означает использование Optimization_CombatScreenshotInterval.
        """
        if interval is None:
            origin = self.config.Optimization_ScreenshotInterval
            interval = limit_in(origin, 0.001, 0.3)
            if interval != origin:
                logger.warning(f'[Устройство — снимок] Optimization.ScreenshotInterval скорректирован: {origin} → {interval}')
                self.config.Optimization_ScreenshotInterval = interval
            # Для nemu_ipc допускаем более низкое значение по умолчанию
            if self.config.Emulator_ScreenshotMethod in ['nemu_ipc', 'ldopengl']:
                interval = limit_in(origin, 0.001, 0.2)
        elif interval == 'combat':
            origin = self.config.Optimization_CombatScreenshotInterval
            interval = limit_in(origin, 0.001, 1.0)
            if interval != origin:
                logger.warning(f'[Устройство — снимок] Optimization.CombatScreenshotInterval скорректирован: {origin} → {interval}')
                self.config.Optimization_CombatScreenshotInterval = interval
        elif isinstance(interval, (int, float)):
            # Значение, заданное вручную в коде, не ограничиваем
            pass
        else:
            logger.warning(f'[Устройство — снимок] Неизвестный интервал снимков экрана: {interval}')
            raise ScriptError(f'[Устройство — снимок] Неизвестный интервал снимков экрана: {interval}')
        # Интервал снимков для scrcpy не имеет смысла: видеопоток принимается непрерывно независимо от использования.
        if self.config.Emulator_ScreenshotMethod == 'scrcpy':
            interval = 0.1

        if interval != self._screenshot_interval.limit:
            logger.info(f'[Устройство — снимок] Интервал снимков экрана установлен на {interval} с')
            self._screenshot_interval.limit = interval

    def image_show(self, image=None):
        """Отображает изображение с помощью системного средства просмотра.

        Args:
            image (np.ndarray, optional): Отображаемое изображение, по умолчанию последний снимок.
        """
        if image is None:
            image = self.image
        Image.fromarray(image).show()

    def image_save(self, file=None):
        """Сохраняет последний снимок экрана в файл.

        Args:
            file (str, optional): Путь сохранения, по умолчанию используется имя с миллисекундной меткой времени.
        """
        if file is None:
            file = f'{int(time.time() * 1000)}.png'
        save_image(self.image, file)

    def check_screen_size(self):
        """Проверяет, равно ли разрешение экрана 1280x720.

        Перед вызовом необходимо сделать снимок экрана.
        """
        if self._screen_size_checked:
            return True

        orientated = False
        for _ in range(2):
            # Проверяем разрешение экрана
            width, height = image_size(self.image)
            logger.attr('Разрешение экрана', f'{width}x{height}')
            if width == 1280 and height == 720:
                self._screen_size_checked = True
                return True
            elif not orientated and (width == 720 and height == 1280):
                logger.info('[Устройство — снимок] Получен снимок с изменённой ориентацией; выполняется обработка')
                self.get_orientation()
                self.image = self._handle_orientated_image(self.image)
                orientated = True
                width, height = image_size(self.image)
                if width == 720 and height == 1280:
                    logger.info('[Устройство — снимок] Не удалось обработать снимок с изменённой ориентацией; выполнение временно продолжается')
                    return True
                else:
                    continue
            elif self.config.Emulator_Serial == 'wsa-0':
                self.display_resize_wsa(0)
                return False
            elif hasattr(self, 'app_is_running') and not self.app_is_running():
                logger.warning('[Устройство — снимок] Получен снимок с изменённой ориентацией, но игра не запущена')
                return True
            else:
                logger.error_context(
                    title='Разрешение экрана не поддерживается',
                    reason=f'Текущее разрешение снимка экрана — {width}x{height}; проект поддерживает только 1280x720.',
                    impact='Надёжное распознавание игрового интерфейса невозможно; задача будет остановлена.',
                    action='Установите разрешение эмулятора и окна игры 1280x720, затем повторно подключите устройство.',
                    level=50,
                )
                raise RequestHumanTakeover

    def check_screen_black(self):
        """Проверяет, не является ли снимок полностью чёрным (сбой эмулятора или блокировка экрана).

        Проверка выполняется при первом вызове, при успехе последующие вызовы сразу возвращают True.
        При обнаружении чёрного экрана предпринимается попытка удаления minicap или перезапуска служб.

        Returns:
            bool: True, если экран в норме; False при чёрном экране для инициации повторной попытки.
        """
        if self._screen_black_checked:
            return True
        # Проверяем цвет экрана: некоторые эмуляторы могут возвращать полностью чёрный снимок.
        color = get_color(self.image, area=(0, 0, 1280, 720))
        if sum(color) < 1:
            if self.config.Emulator_Serial == 'wsa-0':
                for _ in range(2):
                    display = self.get_display_id()
                    if display == 0:
                        return True
                logger.info(f'[Устройство — снимок] Игра запущена на дисплее {display}')
                logger.warning('[Устройство — снимок] Игра запущена не на дисплее 0; выполняется перезапуск')
                self.app_stop_uiautomator2()
                return False
            elif self.config.Emulator_ScreenshotMethod == 'uiautomator2':
                logger.warning(f'[Устройство — снимок] Получен полностью чёрный снимок эмулятора, цвет: {color}')
                logger.warning('[Устройство — снимок] Удаление minicap и повторная попытка')
                logger.warning('[Устройство — снимок] Получен полностью чёрный снимок. Обычно устройство заблокировано либо текущий метод снимка экрана не поддерживается эмулятором')
                self.uninstall_minicap()
                self._screen_black_checked = False
                return False
            else:
                logger.warning(f'[Устройство — снимок] Получен полностью чёрный снимок эмулятора, цвет: {color}')
                logger.warning(f'[Устройство — снимок] Метод `{self.config.Emulator_ScreenshotMethod}` может не работать с эмулятором `{self.serial}` либо эмулятор ещё не полностью запущен')
                if self.is_mumu_family:
                    if self.config.Emulator_ScreenshotMethod == 'DroidCast':
                        self.droidcast_stop()
                    else:
                        logger.warning('[Устройство — снимок] При использовании MuMu X обновитесь до версии >= 12.1.5.0')
                self._screen_black_checked = False
                return False
        else:
            self._screen_black_checked = True
            return True
