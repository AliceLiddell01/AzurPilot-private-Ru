"""Обработчик магазина заслуг, выполняющий покупку эксклюзивных товаров за очки заслуг.
Поддерживает структуру интерфейса от 2025-08-14, использует шаблонное сопоставление для распознавания товаров.
"""

from module.base.decorator import cached_property
from module.logger import logger
from module.shop.base import ShopItemGrid, ShopItemGrid_250814
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus
from module.shop.ui import ShopUI


class MeritShop_250814(ShopClerk, ShopUI, ShopStatus):
    """Обработчик магазина заслуг (новый интерфейс от 2025-08-14).

    Pages: in: page_shop (вкладка магазина заслуг)
    """

    shop_template_folder = './assets/shop/merit'

    @cached_property
    def shop_filter(self):
        """Получить строку фильтра магазина заслуг.

        Returns:
            str: Строка фильтра
        """
        return self.config.MeritShop_Filter.strip()

    # Новый UI от 2025-08-14.
    @cached_property
    def shop_merit_items(self):
        """Загрузить шаблоны и конфигурацию товаров магазина заслуг.

        Returns:
            ShopItemGrid: Объект сетки товаров магазина
        """
        shop_grid = self.shop_grid
        shop_merit_items = ShopItemGrid_250814(
            shop_grid,
            templates={},
            template_area=(25, 20, 82, 72),
            amount_area=(42, 50, 65, 65),
            cost_area=(-12, 115, 60, 155),
            price_area=(18, 121, 85, 150),
        )
        shop_merit_items.load_template_folder(self.shop_template_folder)
        shop_merit_items.load_cost_template_folder('./assets/shop/cost')
        return shop_merit_items

    def shop_items(self):
        """Единый интерфейс получения сетки товаров магазина.

        Все магазины используют общее имя свойства. При языковых различиях серверов
        см. использование @Config в shop_guild/medal.

        Returns:
            ShopItemGrid: Сетка товаров магазина
        """
        return self.shop_merit_items

    def shop_currency(self):
        """OCR-распознавание количества валюты магазина заслуг.

        Определяет текущий баланс заслуг через проверку статуса и записывает в лог.

        Returns:
            int: Количество очков заслуг
        """
        self._currency = self.status_get_merit()
        logger.info(f'[Магазин — заслуги] Заслуги: {self._currency}')
        return self._currency

    def run(self):
        """Запустить процесс покупки в магазине заслуг.

        Pages: in: page_shop (вкладка магазина заслуг)

        Покупает товары магазина заслуг по настройкам фильтра, поддерживает обновление ассортимента.
        """
        # Если фильтр пуст, сразу выходим.
        if not self.shop_filter:
            return

        # На момент вызова интерфейс магазина заслуг уже должен быть открыт.
        logger.hr('[Магазин — заслуги] Магазин заслуг', level=1)

        # Выполняем покупку; при включённом обновлении делаем не более двух попыток.
        refresh = self.config.MeritShop_Refresh
        for _ in range(2):
            success = self.shop_buy()
            if not success:
                break
            if refresh and self.shop_refresh():
                continue
            break
