"""Модуль операций с интерфейсом магазина Operation Siren.

Предоставляет базовые операции взаимодействия с интерфейсом магазина Operation Siren,
включая проверку загрузки страницы, навигацию по боковой панели (магазин Акаши / портовые магазины),
управление адаптивной полосой прокрутки, защиту кликов по безопасной зоне и обработку зависаний.
"""
from typing import Tuple
from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import random_rectangle_vector
from module.exception import GameStuckError
from module.os_shop.assets import OS_SHOP_CHECK, OS_SHOP_SAFE_AREA, OS_SHOP_SCROLL_AREA
from module.logger import logger
from module.ui.navbar import Navbar
from module.ui.scroll import AdaptiveScroll
from module.ui.ui import UI

# Настройка магазина Операции «Сирена»+ и полосы прокрутки
OS_SHOP_SCROLL = AdaptiveScroll(
    OS_SHOP_SCROLL_AREA.button,
    parameters={
        'height': 255 - 99,
        'prominence': 40,
    },
    name="OS_SHOP_SCROLL"
)
OS_SHOP_SCROLL.drag_threshold = 0.1
OS_SHOP_SCROLL.edge_threshold = 0.1


class OSShopUI(UI):
    """Класс операций пользовательского интерфейса магазина Operation Siren.

    Предоставляет методы проверки загрузки страницы, навигации по боковой панели и управления прокруткой.
    """

    def os_shop_load_ensure(self, skip_first_screenshot=True):
        """Убедиться, что страница магазина полностью загружена.

        После переключения боковой панели необходимо дождаться полной загрузки интерфейса.

        Args:
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Returns:
            bool: True при успешной загрузке страницы.

        Raises:
            GameStuckError: Если время ожидания появления магазина истекло.
        """
        ensure_timeout = Timer(3, count=6).start()
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения
            if self.appear(OS_SHOP_CHECK):
                return True
            else:
                logger.warning('Магазин Операции «Сирена»+ не появился, повторная попытка')

            # Обработка исключительной ситуации
            if ensure_timeout.reached():
                raise GameStuckError('Истекло время ожидания появления магазина Операции «Сирена»+')

    @cached_property
    def _os_shop_side_navbar(self):
        """Получить компонент навигации боковой панели магазина.

        Боковая панель включает 4 порта:
            NY (Нью-Йорк), Liverpool (Ливерпуль), Gibraltar (Гибралтар), St. Petersburg (Санкт-Петербург).
        """
        os_shop_side_navbar = ButtonGrid(
            origin=(44, 266), delta=(0, 87),
            button_shape=(231, 46), grid_shape=(1, 4),
            name='OS_SHOP_SIDE_NAVBAR')

        return Navbar(grids=os_shop_side_navbar,
                      active_color=(43, 94, 248), active_threshold=221,
                      inactive_color=(12, 58, 86), inactive_threshold=221)

    def os_shop_side_navbar_ensure(self, upper=None, bottom=None):
        """Переключить боковую панель на указанную страницу.

        Args:
            upper: Индекс сверху вниз:
                1: NY (Нью-Йорк)
                2: Liverpool (Ливерпуль)
                3: Gibraltar (Гибралтар)
                4: St. Petersburg (Санкт-Петербург)
            bottom: Индекс снизу вверх:
                4: NY (Нью-Йорк)
                3: Liverpool (Ливерпуль)
                2: Gibraltar (Гибралтар)
                1: St. Petersburg (Санкт-Петербург)

        Pages:
            in: PORT_SUPPLY_CHECK
            out: PORT_SUPPLY_CHECK
        """
        logger.info(f'Боковая панель магазина Операции «Сирена»+ переключена на {upper or bottom}')
        self.os_shop_load_ensure()
        self._os_shop_side_navbar.set(self, upper=upper, bottom=bottom)

    def init_slider(self) -> Tuple[float, float]:
        """Инициализировать позицию полосы прокрутки.

        Убеждается в наличии полосы прокрутки и переводит её в крайнее верхнее положение.

        Returns:
            Tuple[float, float]: (предыдущая позиция, текущая позиция), изначально (-1.0, 0.0).

        Raises:
            GameStuckError: Если прокрутка не удалась.
        """
        if not OS_SHOP_SCROLL.appear(main=self):
            logger.warning('Полоса прокрутки магазина Операции «Сирена»+ не появилась, попытка восстановления')
            self.rescue_slider()
        retry = Timer(0, count=3)
        retry.start()
        while not OS_SHOP_SCROLL.at_top(main=self):
            logger.info('Полоса прокрутки магазина Операции «Сирена»+ не вверху, попытка прокрутки')
            OS_SHOP_SCROLL.set_top(main=self)
            if retry.reached():
                raise GameStuckError('Не удалось перетащить полосу прокрутки магазина Операции «Сирена»+')
        return -1.0, 0.0

    def rescue_slider(self, distance=200):
        """Восстановить отображение полосы прокрутки.

        Если полоса прокрутки скрыта, вызывает её повторное появление с помощью свайпа.

        Args:
            distance: Расстояние свайпа в пикселях, по умолчанию 200.
        """
        detection_area = (1130, 230, 1170, 710)
        direction_vector = (0, distance)
        p1, p2 = random_rectangle_vector(
            direction_vector, box=detection_area, random_range=(-10, -40, 10, 40), padding=10)
        self.device.drag(p1, p2, segments=2, shake=(25, 0), point_random=(0, 0, 0, 0), shake_random=(-5, 0, 5, 0))
        self.device.click(OS_SHOP_SAFE_AREA)
        self.device.screenshot()

    def pre_scroll(self, pre_pos, cur_pos) -> float:
        """Предварительно обработать действие прокрутки.

        При сбое прокрутки пытается восстановить полосу и повторить попытку.

        Args:
            pre_pos: Предыдущая позиция.
            cur_pos: Текущая позиция.

        Returns:
            float: Позиция после прокрутки.

        Raises:
            GameStuckError: Если повторные попытки прокрутки завершились неудачей.
        """
        if pre_pos == cur_pos:
            logger.warning('Не удалось перетащить полосу прокрутки магазина Операции «Сирена»+')
            if not OS_SHOP_SCROLL.appear(main=self):
                logger.warning('Полоса прокрутки магазина Операции «Сирена»+ не появилась, попытка восстановления')
                self.rescue_slider()
                OS_SHOP_SCROLL.set(cur_pos, main=self)
            retry = Timer(0, count=3)
            retry.start()
            while True:
                logger.warning('Не удалось перетащить полосу прокрутки магазина Операции «Сирена»+, повторная попытка')
                OS_SHOP_SCROLL.next_page(main=self, page=0.5, skip_first_screenshot=False)
                cur_pos = OS_SHOP_SCROLL.cal_position(main=self)
                if pre_pos != cur_pos:
                    logger.info(f'Полоса прокрутки магазина Операции «Сирена»+ перемещена в {cur_pos}')
                    return cur_pos
                if retry.reached():
                    raise GameStuckError('Не удалось перетащить полосу прокрутки магазина Операции «Сирена»+')
        else:
            return cur_pos
