"""
Метод создания снимков экрана DroidCast.

Выполняет создание снимков экрана через службу трансляции экрана DroidCast, применяется в сценариях,
когда ADB screencap недоступен.
DroidCast — это служба создания снимков экрана, работающая на устройстве Android и предоставляющая изображения через HTTP-интерфейс.
Поддерживает два режима: DroidCast и DroidCast_raw (первый возвращает изображения PNG/JPEG,
второй напрямую передает необработанные пиксельные данные для максимальной производительности).
Требует предварительной передачи и запуска APK DroidCast на устройстве.
"""
import time
import typing as t
from functools import wraps

import cv2
import numpy as np
import requests
from adbutils.errors import AdbError

from module.base.decorator import cached_property, del_cached_property
from module.base.timer import Timer
from module.device.method.uiautomator_2 import ProcessInfo, Uiautomator2
from module.device.method.utils import (
    ImageTruncated, PackageNotInstalled, RETRY_TRIES, handle_adb_error, handle_unknown_host_service, retry_sleep)
from module.exception import EmulatorNotRunningError, RequestHumanTakeover
from module.logger import logger


class DroidCastVersionIncompatible(Exception):
    pass


DROIDCAST_RAW_MAIN_CLASS = 'ink.mol.droidcast_raw.Main'


def build_droidcast_raw_argv(remote_apk: str) -> list[str]:
    """Собрать argv DroidCast_raw без shell-операторов."""
    return [
        f'CLASSPATH={remote_apk}',
        'app_process',
        '/',
        DROIDCAST_RAW_MAIN_CLASS,
    ]


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (DroidCast):
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
                logger.error(str(f'[Устройство — DroidCast] Ошибка повторной попытки: {e}'))

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
                logger.error(str(f'[Устройство — DroidCast] Ошибка повторной попытки: {e}'))

                def init():
                    self.detect_package()
            # DroidCast не запущен
            # requests.exceptions.ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))
            # ReadTimeout: HTTPConnectionPool(host='127.0.0.1', port=20482): Read timed out. (read timeout=3)
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                logger.error(str(f'[Устройство — DroidCast] Ошибка повторной попытки: {e}'))

                def init():
                    self.droidcast_init()
            # Несовместимая версия DroidCast
            except DroidCastVersionIncompatible as e:
                logger.error(str(f'[Устройство — DroidCast] Ошибка повторной попытки: {e}'))

                def init():
                    self.droidcast_init()
            # Данные изображения обрезаны
            except ImageTruncated as e:
                from module.device.method.utils import handle_image_truncated
                handle_image_truncated(self, e)

                def init():
                    pass
            # Необрабатываемая ошибка — обязательно пробрасываем выше, чтобы запустить перезапуск эмулятора
            except EmulatorNotRunningError:
                raise
            # Неизвестное исключение
            except Exception as e:
                logger.exception(str(f'[Устройство — DroidCast] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in ['screenshot_droidcast', 'screenshot_droidcast_raw']:
            logger.critical(f'[Устройство — DroidCast] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError
        logger.critical(f'[Устройство — DroidCast] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


class DroidCast(Uiautomator2):
    """
    Метод создания снимков DroidCast, https://github.com/rayworks/DroidCast
    DroidCast_raw — модифицированная версия DroidCast, отправляющая исходный растр и PNG, https://github.com/Torther/DroidCastS
    """

    _droidcast_port: int = 0
    droidcast_width: int = 0
    droidcast_height: int = 0

    @cached_property
    def droidcast_session(self):
        session = requests.Session()
        session.trust_env = False  # Игнорируем прокси
        self._droidcast_port = self.adb_forward('tcp:53516')
        return session

    """
    Ссылки на доступные API в исходном коде:
    https://github.com/Torther/DroidCast_raw/blob/DroidCast_raw/app/src/main/java/ink/mol/droidcast_raw/KtMain.kt
    Доступные эндпоинты:
    - /screenshot
        Получение растрового изображения RGB565
    - /preview
        Получение снимка экрана в формате PNG
    """

    def droidcast_url(self, url='/preview'):
        if self.is_mumu_over_version_356:
            w, h = self.droidcast_width, self.droidcast_height
            if self.orientation == 0:
                return f'http://127.0.0.1:{self._droidcast_port}{url}?width={w}&height={h}'
            elif self.orientation == 1:
                return f'http://127.0.0.1:{self._droidcast_port}{url}?width={h}&height={w}'
            else:
                # logger.warning('DroidCast receives invalid device orientation')
                pass

        return f'http://127.0.0.1:{self._droidcast_port}{url}'

    def droidcast_raw_url(self, url='/screenshot'):
        if self.is_mumu_over_version_356:
            w, h = self.droidcast_width, self.droidcast_height
            if self.orientation == 0:
                return f'http://127.0.0.1:{self._droidcast_port}{url}?width={w}&height={h}'
            elif self.orientation == 1:
                return f'http://127.0.0.1:{self._droidcast_port}{url}?width={h}&height={w}'
            else:
                # logger.warning('DroidCast receives invalid device orientation')
                pass

        return f'http://127.0.0.1:{self._droidcast_port}{url}'

    def droidcast_init(self):
        logger.hr('[Устройство — DroidCast] Инициализация DroidCast')
        self.droidcast_stop()
        self._droidcast_update_resolution()

        logger.info('[Устройство — DroidCast] Отправка APK DroidCast')
        self.adb_push(self.config.DROIDCAST_FILEPATH_LOCAL, self.config.DROIDCAST_FILEPATH_REMOTE)

        logger.info('[Устройство — DroidCast] Запуск APK DroidCast')
        # DroidCast_raw-release-1.1.apk
        # Runner сам добавляет безопасные shell redirection и background.
        # adb shell CLASSPATH=/data/local/tmp/DroidCast_raw.apk app_process / ink.mol.droidcast_raw.Main
        resp = self.u2_shell_background(
            build_droidcast_raw_argv(self.config.DROIDCAST_FILEPATH_REMOTE)
        )
        logger.info(resp)
        del_cached_property(self, 'droidcast_session')
        _ = self.droidcast_session

        if self.config.DROIDCAST_VERSION == 'DroidCast':
            logger.attr('Адрес DroidCast', self.droidcast_url())
            self.droidcast_wait_startup()
        elif self.config.DROIDCAST_VERSION == 'DroidCast_raw':
            logger.attr('Исходный адрес DroidCast', self.droidcast_raw_url())
            self.droidcast_wait_startup()
        else:
            logger.error(f'Неизвестная версия DROIDCAST: {self.config.DROIDCAST_VERSION}')

    def _droidcast_update_resolution(self):
        if self.is_mumu_over_version_356:
            logger.info('[Устройство — DroidCast] Обновление разрешения DroidCast')
            w, h = self.resolution_uiautomator2(cal_rotation=False)
            self.get_orientation()
            # 720, 1280
            # В mumu12 > 3.5.6 устройство всегда считается портретным
            self.droidcast_width, self.droidcast_height = w, h
            logger.info(f'Разрешение DroidCast: {(w, h)}')

    @retry
    def screenshot_droidcast(self):
        self.config.DROIDCAST_VERSION = 'DroidCast'
        if self.is_mumu_over_version_356:
            if not self.droidcast_width or not self.droidcast_height:
                self._droidcast_update_resolution()

        resp = self.droidcast_session.get(self.droidcast_url(), timeout=3)

        if resp.status_code == 404:
            raise DroidCastVersionIncompatible('Сервер DroidCast не поддерживает /preview')
        image = resp.content
        image = np.frombuffer(image, np.uint8)
        if image is None:
            raise ImageTruncated('Пустое изображение после чтения из буфера')
        if image.shape == (1843200,):
            raise DroidCastVersionIncompatible('Запрошены снимки через `DroidCast`, но сервер работает как `DroidCast_raw`')
        if image.size < 500:
            logger.warning(f'[Устройство — DroidCast] Некорректный снимок экрана; получено {len(resp.content)} байт')

        image = cv2.imdecode(image, cv2.IMREAD_COLOR)
        if image is None:
            raise ImageTruncated('Пустое изображение после cv2.imdecode')

        cv2.cvtColor(image, cv2.COLOR_BGR2RGB, dst=image)
        if image is None:
            raise ImageTruncated('Пустое изображение после cv2.cvtColor')

        if self.is_mumu_over_version_356:
            if self.orientation == 1:
                image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)

        return image

    @retry
    def screenshot_droidcast_raw(self):
        self.config.DROIDCAST_VERSION = 'DroidCast_raw'
        shape = (720, 1280)
        if self.is_mumu_over_version_356:
            if not self.droidcast_width or not self.droidcast_height:
                self._droidcast_update_resolution()
            if self.droidcast_height and self.droidcast_width:
                shape = (self.droidcast_height, self.droidcast_width)

        rotate = self.is_mumu_over_version_356 and self.orientation == 1

        resp = self.droidcast_session.get(self.droidcast_raw_url(), timeout=3)
        image = resp.content
        # DroidCast_raw возвращает bitmap RGB565

        # Не допускаем TypeError в np.frombuffer из-за пустого содержимого
        if image is None or len(image) == 0:
            raise ImageTruncated('Пустые данные изображения от DroidCast_raw')

        # DroidCast вернул короткое сообщение об ошибке вместо исходных данных bitmap
        # Например, b':(  Failed to generate the screenshot on device / emulator: ...'
        # Выбрасываем ConnectionError, чтобы обработчик повторных попыток сразу запустил droidcast_init
        if len(image) < 500:
            logger.warning(f'[Устройство — DroidCast] Некорректный снимок экрана; получено {len(image)} байт')
            raise requests.exceptions.ConnectionError(f'[Устройство — DroidCast] Ошибка службы; получено {len(image)} байт')

        try:
            arr = np.frombuffer(image, dtype=np.uint16)
            if rotate:
                arr = arr.reshape(shape)
                # arr = cv2.rotate(arr, cv2.ROTATE_90_CLOCKWISE)
                # Немного быстрее?
                arr = cv2.transpose(arr)
                cv2.flip(arr, 1, dst=arr)
            else:
                arr = arr.reshape(shape)
        except ValueError as e:
            # Пробуем загрузить в формате `DroidCast`
            image = np.frombuffer(image, np.uint8)
            if image is not None:
                image = cv2.imdecode(image, cv2.IMREAD_COLOR)
                if image is not None:
                    raise DroidCastVersionIncompatible(
                        'Запрошены снимки через `DroidCast_raw`, но сервер работает как `DroidCast`')
            # ValueError: cannot reshape array of size 0 into shape (720,1280)
            raise ImageTruncated(str(e)+'\nЕсли разрешение эмулятора отличается от 1280x720, установите разрешение 1280x720')

        # Преобразуем RGB565 в RGB888
        # https://blog.csdn.net/happy08god/article/details/10516871

        # r = (arr & 0b1111100000000000) >> (11 - 3)
        # g = (arr & 0b0000011111100000) >> (5 - 2)
        # b = (arr & 0b0000000000011111) << 3
        # r |= (r & 0b11100000) >> 5
        # g |= (g & 0b11000000) >> 6
        # b |= (b & 0b11100000) >> 5
        # r = r.astype(np.uint8)
        # g = g.astype(np.uint8)
        # b = b.astype(np.uint8)
        # image = cv2.merge([r, g, b])

        # Функционально то же, что код выше, но занимает около 2,7 мс вместо 16 мс.
        # Обратите внимание: cv2.convertScaleAbs в 5 раз быстрее cv2.multiply, а cv2.add в 8 раз быстрее cv2.convertScaleAbs
        # Обратите внимание: cv2.convertScaleAbs выполняет округление
        tmp = np.empty_like(arr)
        cv2.bitwise_and(arr, 0b1111100000000000, dst=tmp)
        r = cv2.convertScaleAbs(tmp, alpha=0.0040283203125)  # 0.00390625 * 1.03125
        cv2.bitwise_and(arr, 0b0000011111100000, dst=tmp)
        g = cv2.convertScaleAbs(tmp, alpha=0.126953125)  # 0.125 * 1.015625
        cv2.bitwise_and(arr, 0b0000000000011111, dst=tmp)
        b = cv2.convertScaleAbs(tmp, alpha=8.25)  # 8 * 1.03125

        image = cv2.merge([r, g, b])

        return image

    def droidcast_wait_startup(self):
        """Ожидать завершения запуска DroidCast."""
        timeout = Timer(10).start()
        while 1:
            self.sleep(0.25)
            if timeout.reached():
                break

            try:
                resp = self.droidcast_session.get(self.droidcast_url('/'), timeout=3)
                # Маршрут `/` недоступен, но 404 означает, что запуск завершён
                if resp.status_code == 404:
                    logger.attr('Состояние DroidCast', 'в сети')
                    return True
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
                logger.attr('Состояние DroidCast', 'не в сети')

        logger.warning('[Устройство — DroidCast] Истёк тайм-аут запуска DroidCast; служба считается запущенной')
        return False

    def droidcast_uninstall(self):
        """
        Остановить процесс DroidCast и удалить APK DroidCast.
        DroidCast не устанавливается в систему полноценно, а запускается через класс Java; удаление означает удаление файла.
        """
        self.droidcast_stop()
        logger.info('[Устройство — DroidCast] Удаление DroidCast')
        self.adb_shell(["rm", self.config.DROIDCAST_FILEPATH_REMOTE])

    def _iter_droidcast_proc(self) -> t.Iterable[ProcessInfo]:
        """Перечислить все процессы DroidCast."""
        processes = self.proc_list_uiautomator2()
        for proc in processes:
            if 'com.rayworks.droidcast.Main' in proc.cmdline:
                yield proc
            if 'com.torther.droidcasts.Main' in proc.cmdline:
                yield proc
            if 'ink.mol.droidcast_raw.Main' in proc.cmdline:
                yield proc

    def droidcast_stop(self):
        """Остановить процессы DroidCast."""
        logger.info('[Устройство — DroidCast] Остановка DroidCast')
        for proc in self._iter_droidcast_proc():
            logger.info(f'[Устройство — DroidCast] Завершение процесса PID={proc.pid}')
            self.adb_shell(['kill', '-s', 9, proc.pid])
