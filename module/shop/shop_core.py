"""Обработчик магазина ядра, управляющий фильтрацией и покупкой товаров за данные ядра.
Поддерживает структуру интерфейса от 2025-08-14, использует шаблонное сопоставление для распознавания товаров.
"""

from module.base.decorator import cached_property
from module.logger import logger
from module.shop.assets import *
from module.shop.base import ShopItemGrid, ShopItemGrid_250814
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus


class CoreShop_250814(ShopClerk, ShopStatus):
    """Обработчик магазина ядра (новый интерфейс от 2025-08-14).

    Pages: in: page_shop (вкладка магазина ядра)
    """

    shop_template_folder = './assets/shop/core'

    @cached_property
    def shop_filter(self):
        """Получить строку фильтра магазина ядра.

        Returns:
            str: Строка фильтра
        """
        return self.config.CoreShop_Filter.strip()

    # Новый UI от 2025-08-14.
    @cached_property
    def shop_core_items(self):
        """Загрузить шаблоны и конфигурацию товаров магазина ядра.

        Returns:
            ShopItemGrid_250814: Объект сетки товаров магазина
        """
        shop_grid = self.shop_grid
        shop_core_items = ShopItemGrid_250814(
            shop_grid,
            templates={},
            template_area=(25, 20, 82, 72),
            amount_area=(42, 50, 65, 65),
            cost_area=(-12, 115, 60, 155),
            price_area=(18, 121, 85, 150),
        )
        shop_core_items.load_template_folder(self.shop_template_folder)
        shop_core_items.load_cost_template_folder('./assets/shop/cost')
        return shop_core_items

    def shop_items(self):
        """Единый интерфейс получения сетки товаров магазина.

        Все магазины используют общее имя свойства. При языковых различиях серверов
        см. использование @Config в shop_guild/medal.

        Returns:
            ShopItemGrid_250814: Сетка товаров магазина
        """
        return self.shop_core_items

    def shop_currency(self):
        """OCR-распознавание количества валюты магазина ядра.

        Определяет текущий баланс данных ядра через проверку статуса и записывает в лог.

        Returns:
            int: Количество данных ядра
        """
        self._currency = self.status_get_core()
        logger.info(f'[Магазин — ядра] Данные ядра: {self._currency}')
        return self._currency

    def shop_interval_clear(self):
        """Сбросить интервалы нажатий для кнопок интерфейса покупки.

        Сбрасывает состояние interval для кнопки подтверждения количества покупки.
        """
        super().shop_interval_clear()
        self.interval_clear(SHOP_BUY_CONFIRM_AMOUNT)

    def shop_buy_handle(self, item):
        """Обработать интерфейс покупки в магазине ядра.

        Распознаёт и обрабатывает интерфейс ввода количества покупки.

        Args:
            item: Объект покупаемого товара

        Returns:
            bool: Обнаружен и обработан ли интерфейс покупки
        """
        if self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
            self.shop_buy_amount_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_AMOUNT)
            return True

        return False

    def run(self):
        """Запустить процесс покупки в магазине ядра.

        Pages: in: page_shop (вкладка магазина ядра)

        Покупает товары магазина ядра в соответствии с конфигурацией фильтра.
        """
        if not self.shop_filter:
            return

        logger.hr('[Магазин — ядра] Магазин ядер', level=1)

        # Выполняем покупку.
        self.shop_buy()
