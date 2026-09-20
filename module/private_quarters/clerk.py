"""Логика продавца магазина личных покоев.

Инкапсулирует процесс покупки в магазине личных покоев, наследует ShopClerk и PQShopUI,
обеспечивает выполнение покупки товаров, сброс таймеров интервалов и сопутствующие операции.
Поддерживает покупку максимального количества и обработку диалогов подтверждения.

Pages:
    in: PRIVATE_QUARTERS_SHOP
"""
from module.base.timer import Timer
from module.logger import logger
from module.private_quarters.assets import *
from module.private_quarters.ui import PQShopUI
from module.shop.clerk import ShopClerk


class PQShopClerk(ShopClerk, PQShopUI):
    def shop_interval_clear(self):
        """Очистить интервальные таймеры кнопок магазина личных покоев.

        Подклассы могут переопределять этот метод для очистки интервалов определенных ассетов.
        """
        self.interval_clear([
            PRIVATE_QUARTERS_SHOP_CHECK,
            PRIVATE_QUARTERS_SHOP_AMOUNT_MAX,
            PRIVATE_QUARTERS_SHOP_CONFIRM_AMOUNT
        ])

    def shop_buy_execute(self, item, skip_first_screenshot=True):
        """Выполнить покупку одного товара.

        Клик по товару -> максимальное количество -> подтверждение покупки -> ожидание завершения.

        Args:
            item: Кнопка покупаемого товара.
            skip_first_screenshot (bool): Пропускать ли первый скриншот.

        Pages:
            in: Магазин личных покоев
            out: Магазин личных покоев
        """

        # Вспомогательная функция: определяет состояние интерфейса до и после подтверждения покупки
        def after_confirm_state():
            return (self.appear(PRIVATE_QUARTERS_SHOP_WEEKLY_ROSES_GET, offset=(20, 20)) or
                    self.appear(PRIVATE_QUARTERS_SHOP_WEEKLY_CAKES_GET, offset=(20, 20)))

        def after_purchase_state():
            return (not self.appear(PRIVATE_QUARTERS_SHOP_WEEKLY_ROSES_GET, offset=(20, 20)) and
                    not self.appear(PRIVATE_QUARTERS_SHOP_WEEKLY_CAKES_GET, offset=(20, 20)) and
                    self.appear(PRIVATE_QUARTERS_SHOP_CHECK))

        self.shop_interval_clear()
        PRIVATE_QUARTERS_SHOP_CHECK.clear_offset()

        for _ in self.loop():

            # Условие завершения: состояние подтверждения покупки
            if after_confirm_state():
                break

            if self.appear(PRIVATE_QUARTERS_SHOP_CHECK, interval=3):
                self.device.click(item)
                continue
            if self.appear_then_click(PRIVATE_QUARTERS_SHOP_AMOUNT_MAX, offset=(20, 20), interval=1):
                continue
            if self.appear_then_click(PRIVATE_QUARTERS_SHOP_CONFIRM_AMOUNT, offset=(20, 20), interval=1):
                continue

        click_timer = Timer(3, count=6)
        for _ in self.loop():
            # Условие завершения: состояние завершённой покупки
            if after_purchase_state():
                break

            if click_timer.reached() and after_confirm_state():
                self.device.click(PRIVATE_QUARTERS_SHOP_CHECK)
                click_timer.reset()
                continue

    def shop_buy(self):
        """Циклически сканировать и покупать доступные товары в магазине.

        Выполняет до 12 итераций: сканирует витрину, проверяет баланс и покупает первое подходящее совпадение.

        Returns:
            bool: Успешно ли завершено (True — все куплено или нечего покупать, False — недостаточно средств).

        Pages:
            in: Магазин личных покоев
            out: Магазин личных покоев
        """
        for _ in range(12):
            logger.hr('Покупки в магазине', level=2)
            # Сначала получаем список товаров, затем считываем валюту для более точного результата OCR
            items = self.shop_get_items()
            self.shop_currency()
            if self._currency <= 0:
                logger.warning(f'[Личные покои — продавец] Текущие средства: {self._currency}; покупки остановлены')
                return False

            item = self.shop_get_item_to_buy(items)
            if item is None:
                logger.info('[Личные покои — продавец] Покупки в магазине завершены')
                return True
            else:
                self.shop_buy_execute(item)

                # После покупки панель навигации сбрасывается в положение по умолчанию, поэтому позицию нужно восстановить
                self.shop_left_navbar_ensure(2)
                self.shop_bottom_navbar_ensure(2)

                continue

        logger.warning('[Личные покои — продавец] Куплено слишком много предметов; остановка')
        return True
