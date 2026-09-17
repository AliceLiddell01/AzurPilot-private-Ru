"""Обработчик общего магазина, поддерживающий покупку товаров за монеты и алмазы.
Поддерживает структуру интерфейса от 2025-08-14, настраивается разрешение на использование алмазов.
"""

from module.base.decorator import cached_property
from module.logger import logger
from module.shop.base import ShopItemGrid, ShopItemGrid_250814
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus
from module.shop.ui import ShopUI
from module.ui.page import page_meowfficer


class GeneralShop_250814(ShopClerk, ShopUI, ShopStatus):
    """Обработчик общего магазина (новый интерфейс от 2025-08-14).

    Pages: in: page_shop (вкладка общего магазина)

    Поддерживает покупку за монеты и алмазы, с настройкой разрешения на трату алмазов.
    """

    gems = 0
    shop_template_folder = './assets/shop/general'

    @cached_property
    def shop_filter(self):
        """Получить строку фильтра общего магазина.

        Returns:
            str: Строка фильтра
        """
        return self.config.GeneralShop_Filter.strip()

    # Новый UI от 2025-08-14
    @cached_property
    def shop_general_items(self):
        """Загрузить шаблоны и конфигурацию товаров общего магазина.

        Returns:
            ShopItemGrid_250814: Объект сетки товаров магазина
        """
        shop_grid = self.shop_grid

        shop_general_items = ShopItemGrid_250814(
            shop_grid,
            templates={},
            template_area=(25, 20, 82, 72),
            amount_area=(42, 50, 65, 65),
            cost_area=(-12, 115, 60, 155),
            price_area=(14, 121, 85, 150),
        )
        shop_general_items.load_template_folder(self.shop_template_folder)
        shop_general_items.load_cost_template_folder('./assets/shop/cost')
        return shop_general_items

    def shop_items(self):
        """Единый интерфейс получения сетки товаров магазина.

        Все магазины используют общее имя свойства. При языковых различиях серверов
        см. использование @Config в shop_guild/medal.

        Returns:
            ShopItemGrid_250814: Сетка товаров магазина
        """
        return self.shop_general_items

    currency_rechecked = 0

    def shop_currency(self):
        """OCR-распознавание количества валюты общего магазина (монеты и алмазы).

        Определяет текущий баланс монет и алмазов через проверку статуса и записывает в лог.

        Returns:
            int: Количество монет
        """
        while 1:
            self._currency = self.status_get_gold_coins()
            self.gems = self.status_get_gems()
            logger.info(f'[Магазин — валюта] Монеты: {self._currency}, гемы: {self.gems}')

            if self.currency_rechecked >= 3:
                logger.warning('[Магазин — валюта] Не удалось исправить ошибку валюты общего магазина; пропуск')
                break

            break

        return self._currency

    def shop_check_item(self, item):
        """Проверить доступность товара для покупки (исходя из баланса валюты).

        Args:
            item: Проверяемый объект товара

        Returns:
            bool: Доступен ли для покупки
        """
        if item.cost == 'Coins':
            if item.price > self._currency:
                return False
            return True

        if self.config.GeneralShop_UseGems:
            if item.cost == 'Gems':
                if item.price > self.gems:
                    return False
                return True

        return False

    def shop_check_custom_item(self, item):
        """Проверить, удовлетворяет ли кастомный товар особым условиям покупки.

        Обрабатывает товары, требующие специальной проверки: автоматическая покупка
        товаров при превышении порога ConsumeCoins или покупка ящиков скинов снаряжения.

        Args:
            item: Проверяемый объект товара

        Returns:
            bool: Является ли товар подходящим кастомным товаром
        """
        consume_coins = self.config.GeneralShop_ConsumeCoins
        # Проверка порога: должен быть > 0 и <= 600000
        # Типобезопасность гарантируется нормализацией в run() → _validate_config_values()
        if consume_coins > 0 and consume_coins <= 600000:
            if self._currency >= consume_coins:
                if item.cost == 'Coins':
                    return True

        if self.config.GeneralShop_BuySkinBox:
            if (not item.is_known_item()) and item.amount == 1 and item.cost == 'Coins' and item.price == 7000:
                # Ящик внешнего вида снаряжения нельзя распознать сопоставлением шаблонов: цвет и внешний вид постоянно меняются
                logger.info(f'[Магазин — товар] Товар {item} считается ящиком внешнего вида снаряжения')
                if self._currency >= item.price:
                    return True

        return False

    @staticmethod
    def _normalize_threshold(value, name):
        """Проверить и скорректировать значение порога в конфигурации.

        Проверяет, является ли значение допустимым int (исключая bool) и лежит ли в диапазоне 0–600000;
        при некорректности возвращает 0 и записывает предупреждение. Допустимый диапазон: 0 — отключить функцию, 1–600000 — включить.

        Args:
            value: Проверяемое значение порога.
            name: Имя параметра конфигурации (для логирования).

        Returns:
            int: Нормализованный порог (0 или int в допустимом диапазоне).
        """
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 600000:
            return value
        logger.warning(f'[Магазин — конфигурация] {name}={value}: некорректное значение '
                       f'(ожидается целое число 0–600000); установлено 0 для отключения функции')
        return 0

    def _meowfficer_overflow_buy(self):
        """Автоматическая покупка ящиков Мяуфицеров при избытке монет.

        Когда количество монет превышает порог OverflowCoins, переходит на экран Мяуфицеров
        для покупки ящиков, пока баланс монет не опустится ниже порога или не будет достигнут суточный лимит 15 штук.
        Диапазон порога: 1–600000; выход за диапазон считается недопустимым и пропускает функцию.

        Pages: in: page_shop, out: page_main
        """
        overflow_coins = self.config.GeneralShop_OverflowCoins

        # Проверка порога: <= 0 означает, что функция отключена
        # Типобезопасность гарантируется нормализацией в run() → _validate_config_values()
        if overflow_coins <= 0:
            return

        # Повторно распознаём монеты через OCR: после покупок их количество могло измениться
        self.shop_currency()
        if self._currency <= overflow_coins:
            logger.info(f'[Магазин — избыток] Монеты {self._currency} <= порога избытка {overflow_coins}; покупка Мяуфицеров из избытка пропущена')
            return

        logger.hr('Покупка Мяуфицеров из избытка', level=1)
        logger.info(f'[Магазин — избыток] Монеты {self._currency} > порога избытка {overflow_coins}; запуск покупки Мяуфицеров из избытка')

        # Переходим на экран Мяуфицеров
        self.ui_goto(page_meowfficer)

        # Ждём полной загрузки интерфейса
        from module.meowfficer.buy import MeowfficerBuy
        meow = MeowfficerBuy(config=self.config, device=self.device)
        meow.wait_meowfficer_buttons()

        # Выполняем покупку ящиков при избытке монет
        meow.meow_overflow_buy(overflow_coins=overflow_coins)

        # Возвращаемся на главный экран
        self.ui_goto_main()

    def _validate_config_values(self):
        """Проверить и скорректировать значения конфигурации.

        Выявляет аномальные значения ConsumeCoins и OverflowCoins (вне диапазона 0–600000
        или некорректного типа, например bool из старых данных) и принудительно устанавливает их в 0.

        Допустимый диапазон: 0 — отключить функцию, 1–600000 — включить функцию с установкой порога.
        Значение обязано быть int: в Python bool является подклассом int (True > 0 всегда True),
        поэтому булевы значения из старых данных приводили бы к вечному срабатыванию покупки избытка.
        """
        self.config.GeneralShop_ConsumeCoins = self._normalize_threshold(
            self.config.GeneralShop_ConsumeCoins, 'ConsumeCoins')
        self.config.GeneralShop_OverflowCoins = self._normalize_threshold(
            self.config.GeneralShop_OverflowCoins, 'OverflowCoins')

    def run(self):
        """Запустить процесс покупки в общем магазине.

        Pages: in: page_shop (вкладка общего магазина)

        Покупает товары общего магазина по настройкам фильтра, поддерживает обновление ассортимента.
        После завершения покупок, если баланс монет превышает порог избытка, автоматически покупает ящики Мяуфицеров.
        """
        # Проверяем и исправляем некорректные значения конфигурации
        self._validate_config_values()

        if not self.shop_filter:
            return

        logger.hr('Общий магазин', level=1)

        # Выполняем покупки; при включённом обновлении делаем не более 2 попыток
        refresh = self.config.GeneralShop_Refresh
        for _ in range(2):
            success = self.shop_buy()
            if not success:
                break
            if refresh and self.shop_refresh():
                continue
            break

        # При избытке монет покупаем ящики Мяуфицеров
        self._meowfficer_overflow_buy()
