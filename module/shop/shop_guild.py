"""Обработчик магазина гильдии, выполняющий покупку эксклюзивных товаров за монеты гильдии.
Поддерживает структуру интерфейса от 2025-08-14, использует шаблонное сопоставление для распознавания товаров.
"""

from module.base.decorator import cached_property
from module.logger import logger
from module.shop.assets import *
from module.shop.base import ShopItemGrid, ShopItemGrid_250814
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus
from module.shop.ui import ShopUI


class GuildShop_250814(ShopClerk, ShopUI, ShopStatus):
    """Обработчик магазина гильдии (новый интерфейс от 2025-08-14).

    Pages: in: page_shop (вкладка магазина гильдии)
    """

    shop_template_folder = './assets/shop/guild'

    @cached_property
    def shop_filter(self):
        """Получить строку фильтра магазина гильдии.

        Returns:
            str: Строка фильтра
        """
        return self.config.GuildShop_Filter.strip()

    # Новый UI от 2025-08-14.
    @cached_property
    def shop_guild_items(self):
        """Загрузить шаблоны и конфигурацию товаров магазина гильдии.

        Returns:
            ShopItemGrid_250814: Объект сетки товаров магазина
        """
        shop_grid = self.shop_grid
        shop_guild_items = ShopItemGrid_250814(
            shop_grid,
            templates={},
            template_area=(25, 20, 82, 72),
            amount_area=(42, 50, 65, 65),
            cost_area=(-12, 115, 60, 155),
            price_area=(14, 121, 85, 150),
        )
        self.shop_template_folder = './assets/shop/guild'
        shop_guild_items.load_template_folder(self.shop_template_folder)
        shop_guild_items.load_cost_template_folder('./assets/shop/cost')
        return shop_guild_items

    def shop_items(self):
        """Единый интерфейс получения сетки товаров магазина.

        Все магазины используют общее имя свойства; при использовании @Config необходимо
        задавать уникальный псевдоним для переопределения.

        Returns:
            ShopItemGrid_250814: Сетка товаров магазина
        """
        return self.shop_guild_items

    def shop_currency(self):
        """OCR-распознавание количества валюты магазина гильдии.

        Определяет текущий баланс монет гильдии через проверку статуса и записывает в лог.

        Returns:
            int: Количество монет гильдии
        """
        self._currency = self.status_get_guild_coins()
        logger.info(f'[Магазин — гильдия] Монеты гильдии: {self._currency}')
        return self._currency

    def shop_interval_clear(self):
        """Сбросить интервалы нажатий для кнопок интерфейса покупки.

        Сбрасывает состояние interval для кнопки подтверждения выбора покупки.
        """
        super().shop_interval_clear()
        self.interval_clear(SHOP_BUY_CONFIRM_SELECT)

    def shop_buy_handle(self, item):
        """Обработать интерфейс покупки в магазине гильдии.

        Распознаёт и обрабатывает интерфейс подтверждения выбора при покупке.

        Args:
            item: Объект покупаемого товара

        Returns:
            bool: Обнаружен и обработан ли интерфейс покупки
        """
        if self.appear(SHOP_BUY_CONFIRM_SELECT, offset=(20, 20), interval=3):
            self.shop_buy_select_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_SELECT)
            return True

        return False

    def run(self):
        """Запустить процесс покупки в магазине гильдии.

        Pages: in: page_shop (вкладка магазина гильдии)

        Покупает товары магазина гильдии по настройкам фильтра, поддерживает обновление ассортимента.
        Обновление стоит 50 монет гильдии, ящик деталей T4 — 60 монет; если баланс меньше 110, обновление пропускается.
        """
        if not self.shop_filter:
            return

        logger.hr('[Магазин — гильдия] Магазин гильдии', level=1)

        # Выполняем покупку; при включённом обновлении делаем не более двух попыток.
        refresh = self.config.GuildShop_Refresh
        for _ in range(2):
            success = self.shop_buy()
            if not success:
                break
            if refresh:
                # Обновление стоит 50 монет, ящик деталей T4 — 60.
                if self._currency >= 110:
                    if self.shop_refresh():
                        continue
                else:
                    logger.info('[Магазин — гильдия] Монеты гильдии < 110; обновление пропущено')
            break
