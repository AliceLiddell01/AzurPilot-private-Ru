"""
Метод создания снимков экрана и ввода ADB.

Выполняет создание снимков экрана и сенсорные операции на устройстве через Android Debug Bridge (ADB).
Предоставляет методы захвата снимков экрана (`screenshot_adb`), получения иерархии XML (`dump_hierarchy`) и др.
Захват изображения экрана основан на команде `adb exec-out screencap -p`,
а сенсорные операции кликов и свайпов осуществляются через `adb shell input`.
Включает механизм автоматических повторных попыток при обрывах соединения ADB и усечении данных изображений.
"""
import re
import time
from functools import wraps

import cv2
import numpy as np
from adbutils.errors import AdbError
from lxml import etree

from module.base.decorator import Config
from module.config.server import DICT_PACKAGE_TO_ACTIVITY
from module.device.connection import Connection
from module.device.method.remove_warning import remove_screenshot_warning
from module.device.method.utils import (ImageTruncated, PackageNotInstalled, RETRY_TRIES, handle_adb_error,
                                        handle_unknown_host_service, retry_sleep)
from module.exception import EmulatorNotRunningError, RequestHumanTakeover, ScriptError
from module.logger import logger


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Adb):
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
            # Не обрабатывается — исключение нужно пробросить выше, чтобы перезапустить эмулятор
            except EmulatorNotRunningError:
                raise
            # Когда служба ADB остановлена
            except ConnectionResetError as e:
                logger.error(str(f'[Устройство — ADB] Ошибка повторной попытки: {e}'))

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
            # Приложение не установлено
            except PackageNotInstalled as e:
                logger.error(str(f'[Устройство — ADB] Ошибка повторной попытки: {e}'))

                def init():
                    self.detect_package()
            # Данные изображения обрезаны
            except ImageTruncated as e:
                from module.device.method.utils import handle_image_truncated
                handle_image_truncated(self, e)

                def init():
                    pass
            # Неизвестное исключение
            except Exception as e:
                logger.exception(str(f'[Устройство — ADB] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in [
            'screenshot_adb', 'screenshot_adb_nc',
            '_app_start_adb_am', '_app_start_adb_monkey',
        ]:
            logger.critical(f'[Устройство — ADB] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError
        logger.critical(f'[Устройство — ADB] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


def load_screencap(data):
    """
    Разобрать необработанные двоичные данные screencap в изображение.

    Args:
        data: Исходные двоичные данные вывода screencap.

    Returns:
        Преобразованное изображение RGB.
    """
    # Загружаем данные
    if data is None or len(data) < 12:
        raise ImageTruncated('Пустые или неполные данные screencap')

    header = np.frombuffer(data[0:12], dtype=np.uint32)
    channel = 4  # screencap передаёт изображение в формате RGBA
    width, height, _ = header  # Обычно 1280, 720, 1

    if data is None or len(data) == 0:
        raise ImageTruncated('Пустые данные изображения от screencap')

    image = np.frombuffer(data, dtype=np.uint8)
    if image is None or image.size == 0:
        raise ImageTruncated('Пустое изображение после чтения из буфера')

    try:
        image = image[-int(width * height * channel):].reshape(height, width, channel)
    except ValueError as e:
        # ValueError: cannot reshape array of size 0 into shape (720,1280,4)
        raise ImageTruncated(str(e))

    image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)
    if image is None:
        raise ImageTruncated('Пустое изображение после cv2.cvtColor')

    return image


class Adb(Connection):
    __screenshot_method = [0, 1, 2]
    __screenshot_method_fixed = [0, 1, 2]

    @staticmethod
    def __load_screenshot(screenshot, method):
        if method == 0:
            pass
        elif method == 1:
            screenshot = screenshot.replace(b'\r\n', b'\n')
        elif method == 2:
            screenshot = screenshot.replace(b'\r\r\n', b'\n')
        else:
            raise ScriptError(f'Неизвестный метод загрузки снимка экрана: {method}')

        screenshot = remove_screenshot_warning(screenshot)

        if screenshot is None or len(screenshot) == 0:
            raise ImageTruncated('Пустые данные снимка экрана в __load_screenshot')

        image = np.frombuffer(screenshot, np.uint8)
        if image is None or image.size == 0:
            raise ImageTruncated('Пустое изображение после чтения из буфера')

        image = cv2.imdecode(image, cv2.IMREAD_COLOR)
        if image is None:
            raise ImageTruncated('Пустое изображение после cv2.imdecode')

        cv2.cvtColor(image, cv2.COLOR_BGR2RGB, dst=image)
        if image is None:
            raise ImageTruncated('Пустое изображение после cv2.cvtColor')

        return image

    def __process_screenshot(self, screenshot):
        for method in self.__screenshot_method_fixed:
            try:
                result = self.__load_screenshot(screenshot, method=method)
                self.__screenshot_method_fixed = [method] + self.__screenshot_method
                return result
            except (OSError, ImageTruncated):
                continue

        self.__screenshot_method_fixed = self.__screenshot_method
        if len(screenshot) < 500:
            logger.warning(f'[Устройство — ADB] Некорректный снимок экрана; получено {len(screenshot)} байт')
        raise OSError(f'Не удалось загрузить снимок экрана')

    @retry
    @Config.when(DEVICE_OVER_HTTP=False)
    def screenshot_adb(self):
        data = self.adb_shell(['screencap', '-p'], stream=True)
        if len(data) < 500:
            logger.warning(f'[Устройство — ADB] Некорректный снимок экрана; получено {len(data)} байт')

        return self.__process_screenshot(data)

    @retry
    @Config.when(DEVICE_OVER_HTTP=True)
    def screenshot_adb(self):
        data = self.adb_shell(['screencap'], stream=True)
        data = remove_screenshot_warning(data)
        if len(data) < 500:
            logger.warning(f'[Устройство — ADB] Некорректный снимок экрана; получено {len(data)} байт')

        return load_screencap(data)

    @retry
    def screenshot_adb_nc(self):
        data = self.adb_shell_nc(['screencap'])
        data = remove_screenshot_warning(data)
        if len(data) < 500:
            logger.warning(f'[Устройство — ADB] Некорректный снимок экрана; получено {len(data)} байт')

        return load_screencap(data)

    @retry
    def click_adb(self, x, y):
        start = time.time()
        self.adb_shell(['input', 'tap', x, y])
        if time.time() - start <= 0.05:
            self.sleep(0.05)

    @retry
    def swipe_adb(self, p1, p2, duration=0.1):
        duration = int(duration * 1000)
        self.adb_shell(['input', 'swipe', *p1, *p2, duration])

    @retry
    def app_current_adb(self):
        """
        Получить имя пакета активного приложения на переднем плане (скопировано из uiautomator2).

        Returns:
            Имя пакета активного приложения.

        Raises:
            OSError: Вызывается, если не удалось определить приложение на переднем плане.

        Note:
            Функция reset_uiautomator зависит от этого метода, поэтому здесь нельзя использовать jsonrpc.
        """
        # Связанный issue: https://github.com/openatx/uiautomator2/issues/200
        # $ adb shell dumpsys window windows
        # Пример вывода:
        #   mCurrentFocus=Window{41b37570 u0 com.incall.apps.launcher/com.incall.apps.launcher.Launcher}
        #   mFocusedApp=AppWindowToken{422df168 token=Token{422def98 ActivityRecord{422dee38 u0 com.example/.UI.play.PlayActivity t14}}}
        # Регулярные выражения
        #   r'mFocusedApp=.*ActivityRecord{\w+ \w+ (?P<package>.*)/(?P<activity>.*) .*'
        #   r'mCurrentFocus=Window{\w+ \w+ (?P<package>.*)/(?P<activity>.*)\}')
        _focusedRE = re.compile(
            r'mCurrentFocus=Window{.*\s+(?P<package>[^\s]+)/(?P<activity>[^\s]+)\}'
        )
        m = _focusedRE.search(self.adb_shell(['dumpsys', 'window', 'windows']))
        if m:
            return m.group('package')

        # Пробуем: adb shell dumpsys activity top
        _activityRE = re.compile(
            r'ACTIVITY (?P<package>[^\s]+)/(?P<activity>[^/\s]+) \w+ pid=(?P<pid>\d+)'
        )
        output = self.adb_shell(['dumpsys', 'activity', 'top'])
        ms = _activityRE.finditer(output)
        ret = None
        for m in ms:
            ret = m.group('package')
        if ret:  # Берём последний результат
            return ret
        raise OSError('[Устройство] Не удалось определить активное приложение')

    @retry
    def _app_start_adb_monkey(self, package_name=None, allow_failure=False):
        """
        Запустить приложение с помощью команды monkey.

        Args:
            package_name: Имя пакета приложения (по умолчанию из конфигурации).
            allow_failure: Если True, не выбрасывать исключение PackageNotInstalled, а вернуть False.

        Returns:
            Успешно ли запущено приложение.

        Raises:
            PackageNotInstalled: Вызывается, если приложение не установлено и allow_failure=False.
        """
        if not package_name:
            package_name = self.package
        result = self.adb_shell([
            'monkey', '-p', package_name, '-c',
            'android.intent.category.LAUNCHER', '--pct-syskeys', '0', '1'
        ])
        if 'No activities found' in result:
            # ** No activities found to run, monkey aborted.
            if allow_failure:
                return False
            else:
                logger.error(result)
                raise PackageNotInstalled(package_name)
        elif 'inaccessible' in result:
            # /system/bin/sh: monkey: inaccessible or not found
            return False
        else:
            # Events injected: 1
            # ## Network stats: elapsed time=4ms (0ms mobile, 0ms wifi, 4ms not connected)
            return True

    @retry
    def _app_start_adb_am(self, package_name=None, activity_name=None, allow_failure=False):
        """
        Запустить приложение с помощью команды am start.

        Args:
            package_name: Имя пакета приложения (по умолчанию из конфигурации).
            activity_name: Имя Activity (по умолчанию из DICT_PACKAGE_TO_ACTIVITY).
            allow_failure: Если True, не выбрасывать исключение PackageNotInstalled, а вернуть False.

        Returns:
            Успешно ли запущено приложение.

        Raises:
            PackageNotInstalled: Вызывается, если приложение не установлено и allow_failure=False.
        """
        if not package_name:
            package_name = self.package
        if not activity_name:
            result = self.adb_shell(['dumpsys', 'package', package_name])
            res = re.search(r'android.intent.action.MAIN:\s+\w+ ([\w.\/]+) filter \w+\s+'
                            r'.*\s+Category: "android.intent.category.LAUNCHER"',
                            result)
            if res:
                # com.YoStarEN.AzurLane/com.manjuu.azurlane.PrePermissionActivity
                activity_name = res.group(1)
                try:
                    activity_name = activity_name.split('/')[-1]
                except IndexError:
                    logger.error(f'Не указано имя Activity: {activity_name}')
                    return False
            else:
                if allow_failure:
                    return False
                else:
                    logger.error(result)
                    raise PackageNotInstalled(package_name)

        cmd = ['am', 'start', '-a', 'android.intent.action.MAIN', '-c',
               'android.intent.category.LAUNCHER', '-n', f'{package_name}/{activity_name}']
        if self.is_local_network_device and self.is_waydroid:
            cmd += ['--windowingMode', '4']
        ret = self.adb_shell(cmd)
        # Недопустимая Activity
        # Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=... }
        # Error type 3
        # Error: Activity class {.../...} does not exist.
        if 'Error: Activity class' in ret:
            if allow_failure:
                return False
            else:
                logger.error(ret)
                return False
        # Уже запущено
        # Warning: Activity not started, intent has been delivered to currently running top-most instance.
        if 'Warning: Activity not started' in ret:
            logger.info('Activity приложения запущена')
            return True
        # Отказ в разрешении
        # Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=com.YoStarEN.AzurLane/com.manjuu.azurlane.MainActivity }
        # java.lang.SecurityException: Permission Denial: ...
        if 'Permission Denial' in ret:
            if allow_failure:
                return False
            else:
                logger.error(ret)
                logger.error('[Устройство — ADB] Отказ в разрешении при запуске приложения; вероятно, указана недопустимая Activity')
                return False
        # Запуск успешен
        # Starting: Intent...
        return True

    # Не используем декоратор @retry, поскольку _app_start_adb_am и _app_start_adb_monkey уже имеют @retry
    # @retry
    def app_start_adb(self, package_name=None, activity_name=None, allow_failure=False):
        """
        Запустить приложение, последовательно пробуя способы am start и monkey.

        Args:
            package_name: Имя пакета приложения; если None, берется из конфигурации.
            activity_name: Имя Activity; если None, берется из DICT_PACKAGE_TO_ACTIVITY;
                если по-прежнему None, запуск выполняется через monkey, а в случае сбоя — через am.
            allow_failure: Если True, не выбрасывать исключение PackageNotInstalled, а вернуть False.

        Returns:
            Успешно ли запущено приложение.

        Raises:
            PackageNotInstalled: Вызывается, если приложение не установлено и allow_failure=False.
        """
        if not package_name:
            package_name = self.package
        if not activity_name:
            activity_name = DICT_PACKAGE_TO_ACTIVITY.get(package_name)

        if activity_name:
            if self._app_start_adb_am(package_name, activity_name, allow_failure):
                return True
        if self._app_start_adb_monkey(package_name, allow_failure):
            return True
        if self._app_start_adb_am(package_name, activity_name, allow_failure):
            return True

        logger.error('Все попытки завершились неудачно')
        return False

    @retry
    def app_stop_adb(self, package_name=None):
        """Остановить приложение: am force-stop."""
        if not package_name:
            package_name = self.package
        self.adb_shell(['am', 'force-stop', package_name])

    @retry
    def dump_hierarchy_adb(self, temp: str = '/data/local/tmp/hierarchy.xml') -> etree._Element:
        """
        Экспортировать иерархию структуры UI через uiautomator dump.

        Args:
            temp: Путь к временному файлу на эмуляторе.

        Returns:
            Разобранная XML-иерархия структуры.
        """
        # Удаляем существующий файл
        # self.adb_shell(['rm', '/data/local/tmp/hierarchy.xml'])

        # Экспортируем иерархию
        for _ in range(2):
            response = self.adb_shell(['uiautomator', 'dump', '--compressed', temp])
            if 'hierchary' in response:
                # UI hierchary dumped to: /data/local/tmp/hierarchy.xml
                break
            else:
                # <None>
                # Нужно остановить uiautomator2
                self.app_stop_adb('com.github.uiautomator')
                self.app_stop_adb('com.github.uiautomator.test')
                continue

        # Читаем с устройства
        content = b''
        for chunk in self.adb.sync.iter_content(temp):
            if chunk:
                content += chunk
            else:
                break

        # Разбираем через lxml
        hierarchy = etree.fromstring(content)
        return hierarchy
