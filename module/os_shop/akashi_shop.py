"""Модуль магазина Акаши в Operation Siren.

Управляет логикой покупки товаров в магазине Акаши (Akashi) в Operation Siren.
Предоставляет конфигурации сетки товаров для различных серверов (CN/EN/JP/TW),
распознавание предметов, принятие решений о покупке и циклы состояния покупки.
"""
from typing import List
from module.base.button import Button, ButtonGrid
from module.base.decorator import Config, cached_property
from module.logger import logger
from module.os_handler.map_event import MapEventHandler
from module.os_handler.os_status import OSStatus
from module.os_shop.selector import Selector
from module.os_shop.ui import OSShopUI
from module.os_shop.item import OSShopItem as Item, OSShopItemGrid as ItemGrid


class AkashiShop(OSStatus, OSShopUI, Selector, MapEventHandler):
    @cached_property
    @Config.when(SERVER='tw')
    def os_akashi_shop_items(self) -> ItemGrid:
        """Получить сетку товаров магазина Акаши (сервер TW).

        Returns:
            ItemGrid: Конфигурация сетки товаров магазина Акаши для TW.
        """
        shop_grid = ButtonGrid(
            origin=(233, 224), delta=(193, 228), button_shape=(98, 98), grid_shape=(4, 2), name='SHOP_GRID')
        shop_items = ItemGrid(
            shop_grid, templates={}, amount_area=(60, 74, 96, 95),
            counter_area=(85, 170, 134, 186), price_area=(52, 132, 132, 165)
        )
        shop_items.load_template_folder('./assets/shop/os')
        shop_items.load_cost_template_folder('./assets/shop/os_cost')
        return shop_items

    @cached_property
    @Config.when(SERVER='en')
    def os_akashi_shop_items(self) -> ItemGrid:
        """Получить сетку товаров магазина Акаши (международный сервер EN).

        Returns:
            ItemGrid: Конфигурация сетки товаров магазина Акаши для EN.
        """
        shop_grid = ButtonGrid(
            origin=(231, 222), delta=(190, 224), button_shape=(98, 98), grid_shape=(4, 2), name='SHOP_GRID')
        shop_items = ItemGrid(
            shop_grid, templates={}, amount_area=(60, 74, 96, 95),
            counter_area=(85, 170, 134, 186), price_area=(52, 132, 132, 165)
        )
        shop_items.load_template_folder('./assets/shop/os')
        shop_items.load_cost_template_folder('./assets/shop/os_cost')
        return shop_items

    @cached_property
    @Config.when(SERVER=None)
    def os_akashi_shop_items(self) -> ItemGrid:
        """Получить сетку товаров магазина Акаши (по умолчанию / сервер CN).

        Returns:
            ItemGrid: Конфигурация сетки товаров магазина Акаши по умолчанию.
        """
        shop_grid = ButtonGrid(
            origin=(233, 224), delta=(193.2, 228), button_shape=(98, 98), grid_shape=(4, 2), name='SHOP_GRID')
        shop_items = ItemGrid(
            shop_grid, templates={}, amount_area=(60, 74, 96, 95),
            counter_area=(85, 170, 134, 186), price_area=(52, 132, 132, 165)
        )
        shop_items.load_template_folder('./assets/shop/os')
        shop_items.load_cost_template_folder('./assets/shop/os_cost')
        return shop_items

    def os_shop_get_items_in_akashi(self) -> List[Item]:
        """Распознать все товары в магазине Акаши.

        Распознаёт товары на текущем экране через сопоставление шаблонов и логирует информацию по строкам.

        Returns:
            list[Item]: Список распознанных товаров, пустой список при их отсутствии.
        """
        if self.config.SHOP_EXTRACT_TEMPLATE:
            self.os_akashi_shop_items.extract_template(self.device.image, './assets/shop/os')
        self.os_akashi_shop_items.predict(self.device.image)

        items = self.os_akashi_shop_items.items
        if len(items):
            min_row = self.os_akashi_shop_items.grids[0, 0].area[1]
            row = [str(item) for item in items if item.button[1] == min_row]
            logger.info(f'Магазин Акаши, строка 1: {row}')
            row = [str(item) for item in items if item.button[1] != min_row]
            logger.info(f'Магазин Акаши, строка 2: {row}')
            return items
        else:
            logger.info('В магазине Акаши предметы не найдены')
            return []

    def os_shop_get_item_to_buy_in_akashi(self) -> Item:
        """Получить товар для покупки в магазине Акаши.

        Считывает количество монет, распознаёт товары магазина с учётом задержки загрузки
        и применяет фильтр для выбора подходящего товара.

        Returns:
            Item: Товар для покупки либо None, если подходящих товаров нет.
        """
        self.os_shop_get_coins()
        items = self.os_shop_get_items_in_akashi()
        # Товары магазина появляются не сразу, поэтому проверяем, действительно ли магазин пуст
        for _ in range(2):
            if not len(items) or any(not item.is_known_item() for item in items):
                logger.warning('Магазин Акаши или список предметов пуст, выполняется подтверждение')
                self.device.sleep((0.3, 0.5))
                self.device.screenshot()
                items = self.os_shop_get_items_in_akashi()
                continue
            else:
                items = self.items_filter_in_akashi_shop(items)
                if not len(items):
                    return None
                else:
                    return items.pop()

        return None
