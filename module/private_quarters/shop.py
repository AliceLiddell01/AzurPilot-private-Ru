"""Основная логика магазина личных покоев.

Организует полный процесс покупок в магазине личных покоев, включая фильтрацию товаров,
сканирование полок, проверку баланса и покупку по списку. Через Filter и PQShopItemGrid
реализует классификацию и выбор товаров с учетом приоритетов.

Pages:
    in: PRIVATE_QUARTERS_SHOP
"""
import re

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.filter import Filter
from module.logger import logger
from module.private_quarters.clerk import PQShopClerk
from module.private_quarters.status import PQStatus, OCR_SHOP_PRICE
from module.statistics.item import ItemGrid

FILTER_REGEX = re.compile(
    '^(gift|furn|misc'
    ')'

    '(sirius'
    '|cake|roses'
    ')'

    '([1-9]+)?$',
    flags=re.IGNORECASE)
FILTER_ATTR = ('group', 'sub_genre', 'tier')
FILTER = Filter(FILTER_REGEX, FILTER_ATTR)


class PQShopItemGrid(ItemGrid):
    def predict(self, image, name=True, amount=True, cost=False, price=False, tag=False):
        """Распознать список товаров и проставить атрибуты group/sub_genre/tier для фильтрации.

        Извлекает атрибуты group, sub_genre, tier из имени товара с помощью регулярного выражения.

        Args:
            image: Изображение скриншота.
            name (bool): Распознавать ли название.
            amount (bool): Распознавать ли количество.
            cost (bool): Распознавать ли затраты.
            price (bool): Распознавать ли цену.
            tag (bool): Распознавать ли тег.

        Returns:
            list[Item]: Список товаров с добавленными атрибутами фильтрации.
        """
        super().predict(image, name, amount, cost, price, tag)

        for item in self.items:
            # Инициализируем значения по умолчанию
            item.group, item.sub_genre, item.tier = None, None, None

            # Быстро заполняем атрибуты фильтра через регулярное выражение
            name = item.name
            result = re.search(FILTER_REGEX, name)
            if result:
                item.group, item.sub_genre, item.tier = \
                    [group.lower()
                     if group is not None else None
                     for group in result.groups()]
            else:
                continue

        return self.items


class PQShop(PQShopClerk, PQStatus):
    gems = 0
    shop_template_folder = './assets/shop/private_quarters'

    @cached_property
    def shop_filter(self):
        """Сформировать строку фильтрации товаров на основе конфигурации.

        Returns:
            str: Условие фильтрации, например 'GiftRoses > GiftCake'.
        """
        list_filter = []
        if self.config.PrivateQuarters_BuyRoses:
            list_filter.append('GiftRoses')
        if self.config.PrivateQuarters_BuyCake:
            list_filter.append('GiftCake')

        return ' > '.join(list_filter).strip()

    @cached_property
    def shop_grid(self):
        """Сетка товаров магазина (4 колонки, 1 строка).

        Returns:
            ButtonGrid: Сетка кнопок товаров.
        """
        shop_grid = ButtonGrid(
            origin=(290, 215), delta=(230, 0), button_shape=(96, 96), grid_shape=(4, 1),
            name='PRIVATE_QUARTERS_BUTTON_GRID_ITEM')
        return shop_grid

    @cached_property
    def shop_private_quarters_items(self):
        """Сетка товаров магазина личных покоев с сопоставлением шаблонов и OCR цен.

        Returns:
            PQShopItemGrid: Экземпляр сетки товаров.
        """
        shop_grid = self.shop_grid
        shop_private_quarters_items = PQShopItemGrid(shop_grid, templates={},
                                                     cost_area=(-52, 330, -26, 353), price_area=(-26, 331, 36, 357))
        shop_private_quarters_items.price_ocr = OCR_SHOP_PRICE
        shop_private_quarters_items.load_template_folder(self.shop_template_folder)
        shop_private_quarters_items.load_cost_template_folder('./assets/shop/private_quarters_cost')
        return shop_private_quarters_items

    def shop_items(self):
        """Получить экземпляр сетки товаров магазина.

        При языковых различиях серверов ориентироваться на подход @Config в shop_guild/medal.

        Returns:
            PQShopItemGrid: Экземпляр сетки товаров.
        """
        return self.shop_private_quarters_items

    def shop_currency(self):
        """Распознать через OCR валюту магазина (монеты и алмазы) и обновить внутреннее состояние.

        Pages:
            in: Страница магазина личных покоев
        """
        self._currency = self.status_get_gold_coins()
        self.gems = self.status_get_gems()
        logger.info(f'[Личные покои — магазин] Монеты: {self._currency}, самоцветы: {self.gems}')

    def shop_check_item(self, item):
        """Проверить, доступен ли товар для покупки (достаточен ли баланс).

        Розы требуют 24 000+ монет, торт требует 210+ алмазов.

        Args:
            item: Проверяемый товар.

        Returns:
            bool: Доступен ли для покупки.
        """
        if self.config.PrivateQuarters_BuyRoses:
            if item.sub_genre == 'roses':
                if 24000 > self._currency:
                    return False
                return True

        if self.config.PrivateQuarters_BuyCake:
            if item.sub_genre == 'cake':
                if 210 > self.gems:
                    return False
                return True

        return False

    def shop_get_item_to_buy(self, items):
        """Выбрать из списка товаров первый доступный для покупки.

        Args:
            items (list[Item]): Список товаров.

        Returns:
            Item: Товар для покупки, либо None, если ничего недоступно.
        """
        # Загружаем условия фильтра, применяем фильтрацию и возвращаем первый результат
        FILTER.load(self.shop_filter)
        filtered = FILTER.apply(items, self.shop_check_item)

        if not filtered:
            return None
        logger.attr('Порядок товаров', ' > '.join([str(item) for item in filtered]))

        return filtered[0]
