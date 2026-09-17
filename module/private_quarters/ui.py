"""Навигация интерфейса магазина личных покоев.

Обеспечивает обнаружение страниц и управление панелями навигации магазина личных покоев,
включая нижнюю панель вкладок (Все / Подарки / Мебель / Разное) и левую панель комнат.
Переключение вкладок и проверка состояния реализованы через Navbar.

Pages:
    in: PRIVATE_QUARTERS_SHOP
"""
from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.shop.ui import ShopUI
from module.ui.navbar import Navbar


class PQShopUI(ShopUI):
    @cached_property
    def _shop_bottom_navbar(self):
        """Нижняя панель навигации магазина, содержащая 4 вкладки.

        Варианты:
            Все / Подарки / Мебель / Разное

        Returns:
            Navbar: Экземпляр нижней панели навигации.
        """
        shop_navgrid = ButtonGrid(
            origin=(465, 600), delta=(200, 0), button_shape=(20, 20), grid_shape=(4, 1),
            name='PRIVATE_QUARTERS_BOTTOM_BUTTON_GRID')

        return Navbar(shop_navgrid,
                      active_color=(186, 226, 245), inactive_color=(236, 237, 243),
                      active_count=350, inactive_count=350,
                      active_threshold=221, inactive_threshold=221,
                      name='PRIVATE_QUARTERS_BOTTOM_NAVBAR')

    def shop_bottom_navbar_ensure(self, left=None, right=None):
        """Переключить нижнюю вкладку магазина и дождаться загрузки страницы.

        Указывать либо left, либо right (не оба одновременно).

        Args:
            left (int): N-я вкладка слева (начиная с 1).
            right (int): N-я вкладка справа (начиная с 1).

        Returns:
            bool: Успешно ли переключена вкладка.
        """
        if self._shop_bottom_navbar.set(self, left=left, right=right):
            return True
        return False

    @cached_property
    def _shop_left_navbar(self):
        """Левая панель навигации магазина, содержащая 5 входов в комнаты.

        Варианты:
            Главная / Сириус / Носиро / Анкоридж / Нью-Джерси
        """
        shop_navgrid = ButtonGrid(
            origin=(152, 158), delta=(0, 105), button_shape=(15, 15), grid_shape=(1, 5),
            name='PRIVATE_QUARTERS_LEFT_BUTTON_GRID')

        return Navbar(shop_navgrid,
                      active_color=(255, 255, 255), inactive_color=(176, 245, 250),
                      active_count=200, inactive_count=200,
                      active_threshold=221, inactive_threshold=221,
                      name='PRIVATE_QUARTERS_LEFT_NAVBAR')

    def shop_left_navbar_ensure(self, upper=None, bottom=None):
        """Переключить вкладку комнаты в левой панели магазина и дождаться загрузки страницы.

        Указывать либо upper, либо bottom (не оба одновременно).

        Args:
            upper (int): N-я вкладка сверху (начиная с 1).
            bottom (int): N-я вкладка снизу (начиная с 1).

        Returns:
            bool: Успешно ли переключена вкладка.
        """
        if self._shop_left_navbar.set(self, upper=upper, bottom=bottom):
            return True
        return False
