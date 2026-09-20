"""Модуль управления жизненным циклом приложения.

Управляет запуском, остановкой, очисткой кэша приложения Android (Azur Lane),
а также получением иерархии UI (hierarchy) и запросами элементов через XPath.
Автоматически выбирает бэкенд ADB или uiautomator2 в зависимости от метода управления и типа эмулятора.
"""
from lxml import etree

from module.base.timer import Timer
from module.device.method.adb import Adb
from module.device.method.uiautomator_2 import Uiautomator2
from module.device.method.utils import HierarchyButton
from module.device.method.wsa import WSA
from module.exception import ScriptError
from module.logger import logger


class AppControl(Adb, WSA, Uiautomator2):
    """Диспетчер жизненного цикла приложения и иерархии UI.

    Объединяет бэкенды ADB, WSA и uiautomator2 через множественное наследование,
    автоматически направляя операции запуска, остановки и проверки состояния приложения.
    Предоставляет дамп иерархии UI и запросы элементов по XPath для проверки состояния интерфейса.

    Attributes:
        hierarchy (etree._Element): Дерево последней полученной иерархии UI.
        _app_u2_family (list[str]): Список методов управления, требующих бэкенд uiautomator2.
        _hierarchy_interval (Timer): Таймер интервала получения иерархии.
    """
    hierarchy: etree._Element
    _app_u2_family = ['uiautomator2', 'minitouch', 'scrcpy', 'MaaTouch', 'nemu_ipc']
    _hierarchy_interval = Timer(0.1)

    def app_current(self) -> str:
        """Возвращает имя пакета приложения, работающего на переднем плане.

        Выбирает способ получения в зависимости от метода управления:
        WSA использует бэкенд WSA, семейство uiautomator2 использует бэкенд uiautomator2,
        остальные используют ADB.

        Returns:
            str: Строка имени пакета приложения на переднем плане.
        """
        method = self.config.Emulator_ControlMethod
        if self.is_wsa:
            package = self.app_current_wsa()
        elif method in AppControl._app_u2_family:
            package = self.app_current_uiautomator2()
        else:
            package = self.app_current_adb()
        package = package.strip(' \t\r\n')
        return package

    def app_is_running(self) -> bool:
        """Проверяет, запущено ли целевое приложение (Azur Lane) на переднем плане.

        Определяется путём сравнения имени пакета на переднем плане с именем из конфигурации.

        Returns:
            bool: True, если приложение работает на переднем плане.
        """
        package = self.app_current()
        logger.debug(f'[Пакет приложения] {package}')
        return package == self.package

    def app_start(self):
        """Запускает целевое приложение (Azur Lane).

        Выбирает метод запуска в зависимости от типа устройства и настроек:
        устройства WSA указывают display=0, семейство uiautomator2 запускается через uiautomator2,
        остальные — через ADB am start.
        """
        method = self.config.Emulator_ControlMethod
        logger.info(f'[Устройство — приложение] Запуск приложения: {self.package}')
        if self.config.Emulator_Serial == 'wsa-0':
            self.app_start_wsa(display=0)
        elif method in AppControl._app_u2_family:
            self.app_start_uiautomator2()
        else:
            self.app_start_adb()

    def app_stop(self):
        """Останавливает целевое приложение (Azur Lane).

        В зависимости от метода управления выбирает uiautomator2 или ADB am force-stop.
        """
        method = self.config.Emulator_ControlMethod
        logger.info(f'[Устройство — приложение] Остановка приложения: {self.package}')
        if method in AppControl._app_u2_family:
            self.app_stop_uiautomator2()
        else:
            self.app_stop_adb()

    def app_clear(self):
        """Очищает каталог кэша целевого приложения.

        Удаляет файлы в /sdcard/Android/data/{package}/cache/ через ADB.
        """
        cache_path = f'/sdcard/Android/data/{self.package}/cache/*'
        logger.info(f'[Устройство — приложение] Очистка кэша приложения: {cache_path}')
        result = self.adb_shell(['rm', '-rf', cache_path], timeout=30)
        if result:
            logger.info(f'[Устройство — приложение] Результат очистки кэша приложения: {result}')

    def hierarchy_timer_set(self, interval=None):
        """Устанавливает минимальный интервал между запросами иерархии UI.

        Args:
            interval (int, float, optional): Интервал в секундах, None для значения по умолчанию 0.1 с.

        Raises:
            ScriptError: Если тип параметра интервала некорректен.
        """
        if interval is None:
            interval = 0.1
        elif isinstance(interval, (int, float)):
            # При ручной настройке в коде ограничение не применяется
            pass
        else:
            logger.warning(f'[Устройство — приложение] Неизвестный интервал получения иерархии: {interval}')
            raise ScriptError(f'[Устройство — приложение] Неизвестный интервал получения иерархии: {interval}')

        if interval != self._hierarchy_interval.limit:
            logger.info(f'[Устройство — приложение] Интервал получения иерархии установлен на {interval} с')
            self._hierarchy_interval.limit = interval

    def dump_hierarchy(self) -> etree._Element:
        """Возвращает текущую структуру иерархии UI интерфейса.

        Returns:
            etree._Element: Элемент иерархии UI, к которому можно применять XPath, например `self.hierarchy.xpath('//*[@text="Hermit"]')`.
        """
        self._hierarchy_interval.wait()
        self._hierarchy_interval.reset()

        method = self.config.Emulator_ControlMethod
        if method in AppControl._app_u2_family:
            self.hierarchy = self.dump_hierarchy_uiautomator2()
        else:
            self.hierarchy = self.dump_hierarchy_adb()
        return self.hierarchy

    def xpath_to_button(self, xpath: str) -> HierarchyButton:
        """
        Args:
            xpath (str):

        Returns:
            HierarchyButton:
                An object with methods and properties similar to Button.
                If element not found or multiple elements were found, return None.
        """
        return HierarchyButton(self.hierarchy, xpath)
