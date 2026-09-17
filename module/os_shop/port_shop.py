"""Модуль портовых магазинов Operation Siren.

Управляет просмотром и покупкой товаров в портовых магазинах Operation Siren.
Предоставляет загрузку шаблонов валют, позиционирование сетки товаров, поиск предметов и полное сканирование,
обеспечивая автоматическую покупку заданных предметов в портовых магазинах.
"""
from typing import List

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.template import Template
from module.logger import logger
from module.map_detection.utils import Points
from module.os_handler.map_event import MapEventHandler
from module.os_handler.os_status import OSStatus
from module.os_shop.item import OSShopItem as Item, OSShopItemGrid as ItemGrid
from module.os_shop.selector import Selector
from module.os_shop.ui import OSShopUI, OS_SHOP_SCROLL
from module.statistics.utils import load_folder


class PortShop(OSStatus, OSShopUI, Selector, MapEventHandler):
    """Класс операций в портовом магазине.

    Предоставляет распознавание товаров, позиционирование сетки, поиск предметов и полное сканирование.
    """

    @cached_property
    def TEMPLATES(self) -> List[Template]:
        """Загрузить шаблоны иконок валют.

        Загружает изображения шаблонов доступной валюты и валюты распроданных позиций для сопоставления.
        """
        TEMPLATES = []
        coins = load_folder('./assets/shop/os_cost')
        coins_sold_out = load_folder('./assets/shop/os_cost_sold_out')
        for c in coins.values():
            TEMPLATES.append(Template(c))
        for c in coins_sold_out.values():
            TEMPLATES.append(Template(c))
        return TEMPLATES

    def _get_os_shop_cost(self) -> list:
        """Получить координаты верхнего левого угла каждой иконки валюты.

        Определяет позиции всех иконок валюты на экране через сопоставление шаблонов.

        Returns:
            list: Список позиций иконок валюты, сгруппированных по Y-координате.
        """
        image = self.image_crop((360, 320, 410, 700))
        result = sum([template.match_multi(image) for template in self.TEMPLATES], [])
        logger.attr('Расположение иконок валюты', f'{result}')
        return Points([(0., m.area[1]) for m in result]).group(threshold=5)

    @cached_property
    def os_shop_items(self) -> ItemGrid:
        """Получить конфигурацию сетки товаров магазина."""
        os_shop_items = ItemGrid(
            grids=None, templates={}, amount_area=(77, 77, 96, 96),
            counter_area=(70, 167, 134, 186), price_area=(52, 132, 130, 165)
        )
        os_shop_items.load_template_folder('./assets/shop/os')
        os_shop_items.load_cost_template_folder('./assets/shop/os_cost')
        return os_shop_items

    def _get_os_shop_grid(self) -> ButtonGrid:
        """Рассчитать сетку магазина по позициям иконок валюты.

        Динамически формирует сетку товаров на основе распознанного числа строк и позиций иконок валюты.

        Returns:
            ButtonGrid: Конфигурация сетки товаров магазина.
        """
        costs = self._get_os_shop_cost()
        row = len(costs)
        y = 0
        delta_y = 0

        if row == 1:
            y = 320 + costs[0][1] - 130
        elif row == 2:
            y = 320 + min(costs[0][1], costs[1][1]) - 130
            delta_y = abs(costs[0][1] - costs[1][1])

        return ButtonGrid(
            origin=(356, y), delta=(160, delta_y), button_shape=(98, 98), grid_shape=(5, row), name='OS_SHOP_GRID')

    def os_shop_get_items(self, shop_index=False, scroll_pos=False) -> List[Item]:
        """Получить товары магазина на текущем экране.

        Распознаёт название, количество, тип стоимости, цену и счетчик каждого товара.

        Args:
            shop_index: Индекс магазина для привязки товара.
            scroll_pos: Позиция прокрутки для привязки положения товара.

        Returns:
            list[Item]: Список распознанных товаров либо пустой список при их отсутствии.
        """
        self.os_shop_items.grids = self._get_os_shop_grid()
        if self.config.SHOP_EXTRACT_TEMPLATE:
            self.os_shop_items.extract_template(self.device.image, './assets/shop/os')
        self.os_shop_items.predict(self.device.image, counter=True, shop_index=shop_index, scroll_pos=scroll_pos)
        shop_items = self.os_shop_items.items

        if len(shop_items):
            min_row = self.os_shop_items.grids[0, 0].area[1]
            row = [str(item) for item in shop_items if item.button[1] == min_row]
            logger.info(f'[Магазин Операции «Сирена» — порт] Магазин+, строка 1: {row}')
            row = [str(item) for item in shop_items if item.button[1] != min_row]
            logger.info(f'[Магазин Операции «Сирена» — порт] Магазин+, строка 2: {row}')
            return shop_items
        else:
            logger.info('[Магазин Операции «Сирена» — порт] В магазине+ предметы не найдены')

        return []

    def os_shop_get_items_to_buy(self, name, price) -> Item:
        """Найти товар для покупки по названию и цене.

        Обрабатывает возможную задержку загрузки магазина и повторяет попытку подтверждения данных.

        Args:
            name: Название товара.
            price: Цена товара.

        Returns:
            Item: Найденный товар либо None.
        """
        items = self.os_shop_get_items()
        for _ in range(2):
            if not len(items) or any(not item.is_known_item() for item in items):
                logger.warning('[Магазин Операции «Сирена» — порт] Магазин+ или список предметов пуст, выполняется подтверждение')
                self.device.sleep((0.3, 0.5))
                self.device.screenshot()
                items = self.os_shop_get_items()
                continue
            else:
                _items = [item for item in items if item.name == name and item.price == price]
                if len(_items):
                    return _items.pop()

        return None

    def scan_all(self) -> List[Item]:
        """Отсканировать товары на всех страницах магазина.

        Обходит 4 вкладки магазина, выполняя прокрутку и сканирование всех товаров
        с дедупликацией через множество.

        Returns:
            list[Item]: Полный список отсканированных товаров.
        """
        items = []
        # Используем set для хранения ключей уже просканированных товаров и дедупликации за O(1)
        scanned_keys = set()
        self.device.click_record.clear()

        for i in range(4):
            logger.hr(f'Сканирование магазина Операции «Сирена»+ {i}')
            self.os_shop_side_navbar_ensure(upper=i + 1)
            pre_pos, cur_pos = self.init_slider()

            while True:
                pre_pos = self.pre_scroll(pre_pos, cur_pos)

                _items = []
                for _ in range(3):
                    _items = self.os_shop_get_items(i, cur_pos)
                    if not len(_items) or any(not item.is_known_item() for item in _items):
                        logger.warning('[Магазин Операции «Сирена» — порт] Магазин+ или список предметов пуст, выполняется подтверждение')
                        self.device.sleep((0.3, 0.5))
                        self.device.screenshot()
                        continue
                    else:
                        logger.info(f'[Магазин Операции «Сирена» — порт] В магазине {i + 1} на позиции {cur_pos:.2f} найдено предметов: {len(_items)} шт.')
                        break
                # Всегда добавляем товары, даже если итоговый список содержит неизвестные позиции
                # Так удаётся просканировать все известные товары
                for item in _items:
                    key = (item.name, item.price, item.shop_index)
                    if key not in scanned_keys:
                        scanned_keys.add(key)
                        items.append(item)

                if OS_SHOP_SCROLL.at_bottom(main=self):
                    logger.info('[Магазин Операции «Сирена» — порт] Прокрутка магазина+ достигла конца, сканирование остановлено')
                    break
                else:
                    OS_SHOP_SCROLL.next_page(main=self, page=0.5, skip_first_screenshot=False)
                    cur_pos = OS_SHOP_SCROLL.cal_position(main=self)
                    continue
            self.device.click_record.clear()

        return items
