"""Модуль обработки пользовательского интерфейса системы постройки.

Предоставляет операции с интерфейсом постройки (Gacha/Build), включая проверку загрузки ресурсов страницы,
навигацию по боковым вкладкам (лёгкое/тяжёлое/особое/временное строительство),
переключение пулов постройки и обработку результатов взаимодействия с интерфейсом.
"""

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.gacha.assets import *
from module.logger import logger
from module.ui.navbar import Navbar
from module.ui.page import page_build
from module.ui.ui import UI

GACHA_LOAD_ENSURE_BUTTONS = [SHOP_MEDAL_CHECK, BUILD_SUBMIT_ORDERS, BUILD_SUBMIT_WW_ORDERS, BUILD_FINISH_ORDERS, BUILD_WW_CHECK]


class GachaUI(UI):
    def gacha_load_ensure(self, skip_first_screenshot=True):
        """
        Ожидание завершения загрузки ресурсов страницы постройки.

        После переключения боковой панели требуется некоторое время для полной загрузки,
        аналогично процессу загрузки страницы логистики гильдии.
        Завершение загрузки определяется по появлению целевых кнопок в цикле снимков экрана.

        Args:
            skip_first_screenshot: Пропускать ли первый снимок экрана, повторно используя снимок из предыдущего цикла.

        Returns:
            Загрузились ли ресурсы.
        """
        ensure_timeout = Timer(3, count=6).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения — достаточно появления любой целевой кнопки
            results = [self.appear(button) for button in GACHA_LOAD_ENSURE_BUTTONS]
            if any(results):
                return True

            # Ошибка по тайм-ауту — ресурсы не успели загрузиться
            if ensure_timeout.reached():
                logger.warning('[Строительство — UI] Тайм-аут ожидания загрузки ресурсов; загрузка не завершена')
                return False

    @cached_property
    def _gacha_side_navbar(self):
        """
        Получение левой боковой панели навигации страницы постройки.

        Боковая панель временного строительства содержит 5 пунктов, а постоянного строительства — 4 пункта.

        Pages: page_build

        Распределение вкладок:
            Временное строительство (5 пунктов): Постройка, Лимитированная постройка, Обмен, Магазин, Списание
            Постоянное строительство (4 пункта): Постройка, Обмен, Магазин, Списание
        """
        gacha_side_navbar = ButtonGrid(
            origin=(21, 126), delta=(0, 98),
            button_shape=(60, 80), grid_shape=(1, 5),
            name='GACHA_SIDE_NAVBAR')

        return Navbar(grids=gacha_side_navbar,
                      active_color=(247, 255, 173), active_threshold=221,
                      inactive_color=(140, 162, 181), inactive_threshold=221)

    def gacha_side_navbar_ensure(self, upper=None, bottom=None):
        """
        Обеспечение переключения боковой панели навигации на указанную вкладку и завершения загрузки.

        Pages: page_build

        Args:
            upper: Порядковый номер вкладки сверху вниз.
                Временное | Постоянное
                    1     -> Постройка
                    2|N/A -> Лимитированная постройка (только временное)
                    3|2   -> Обмен
                    4|3   -> Магазин
                    5|4   -> Списание
            bottom: Порядковый номер вкладки снизу вверх.
                Временное | Постоянное
                    5|4   -> Постройка
                    4|N/A -> Лимитированная постройка (только временное)
                    3     -> Обмен
                    2     -> Магазин
                    1     -> Списание

        Returns:
            Успешно ли переключена боковая панель навигации.
        """
        retire_upper = 5 if self._gacha_side_navbar.get_total(main=self) == 5 else 4
        if upper == retire_upper or bottom == 1:
            logger.warning('[Строительство — UI] Переход на страницу списания не поддерживается')
            return False

        if self._gacha_side_navbar.set(self, upper=upper, bottom=bottom) \
                and self.gacha_load_ensure():
            return True
        return False

    @cached_property
    def _construct_bottom_navbar(self):
        """
        Получение нижней панели навигации по вкладкам страницы постройки.

        Внизу страницы временного строительства находится 4 вкладки, постоянного — 3 вкладки.

        Pages: page_build

        Распределение вкладок:
            Временное строительство (4 вкладки): Событие, Лёгкое, Тяжёлое, Особое
            Постоянное строительство (3 вкладки): Лёгкое, Тяжёлое, Особое
        """
        construct_bottom_navbar = ButtonGrid(
            origin=(262, 615), delta=(209, 0),
            button_shape=(70, 49), grid_shape=(4, 1),
            name='CONSTRUCT_BOTTOM_NAVBAR')

        return Navbar(grids=construct_bottom_navbar,
                      active_color=(247, 227, 148),
                      inactive_color=(189, 231, 247))

    @cached_property
    def _exchange_bottom_navbar(self):
        """
        Получение нижней панели навигации по вкладкам страницы обмена.

        Внизу страницы обмена находится 2 вкладки.

        Pages: page_build

        Распределение вкладок:
            2 вкладки: Корабли, Предметы
        """
        exchange_bottom_navbar = ButtonGrid(
            origin=(569, 637), delta=(208, 0),
            button_shape=(70, 49), grid_shape=(2, 1),
            name='EXCHANGE_BOTTOM_NAVBAR')

        return Navbar(grids=exchange_bottom_navbar,
                      active_color=(247, 227, 148),
                      inactive_color=(189, 231, 247))

    def _gacha_bottom_navbar(self, is_build=True):
        """
        Возврат соответствующей нижней панели навигации в зависимости от типа текущей страницы.

        Для страницы постройки возвращает нижнюю панель постройки, для страницы обмена — панель обмена.

        Args:
            is_build: Является ли страница экраном постройки. True возвращает панель постройки, False — панель обмена.

        Returns:
            Экземпляр Navbar нижней панели соответствующей страницы.
        """
        if is_build:
            return self._construct_bottom_navbar
        else:
            return self._exchange_bottom_navbar

    def gacha_bottom_navbar_ensure(self, left=None, right=None, is_build=True):
        """
        Обеспечение переключения нижней панели навигации на указанную вкладку и завершения загрузки.

        Pages: page_build

        Args:
            left: Порядковый номер вкладки слева направо.
                Панель постройки:
                    Временное | Постоянное
                    1|N/A -> Событие
                    2|1   -> Лёгкое
                    3|2   -> Тяжёлое
                    4|3   -> Особое
                Панель обмена:
                    1     -> Корабли
                    2     -> Предметы
            right: Порядковый номер вкладки справа налево.
                Панель постройки:
                    Временное | Постоянное
                    4|N/A -> Событие
                    3     -> Лёгкое
                    2     -> Тяжёлое
                    1     -> Особое
                Панель обмена:
                    2     -> Корабли
                    1     -> Предметы
            is_build: Является ли страница экраном постройки.

        Returns:
            Успешно ли переключена нижняя панель навигации.
        """
        gacha_bottom_navbar = self._gacha_bottom_navbar(is_build)
        if is_build and gacha_bottom_navbar.get_total(main=self) == 3:
            if left == 1 or right == 4:
                logger.info('[Строительство — UI] Ограниченный пул недоступен; переключение на лёгкое строительство')
                left = 1
                right = None
            if left == 4:
                left = 3

        if gacha_bottom_navbar.set(self, left=left, right=right) \
                and self.gacha_load_ensure():
            return True
        return False

    def ui_goto_gacha(self):
        """
        Переход на страницу постройки.

        Pages: out: *, in: page_build
        """
        self.ui_ensure(page_build)
