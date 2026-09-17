"""Диспетчер магазинов наград, координирующий задачи покупок в магазинах различных типов.
Включает два режима работы: высокочастотные покупки и разовые покупки.
"""

from module.shop.assets import *
from module.shop.shop_core import CoreShop_250814
from module.shop.shop_general import GeneralShop_250814
from module.shop.shop_guild import GuildShop_250814
from module.shop.shop_medal import MedalShop2_250814
from module.shop.shop_merit import MeritShop_250814
from module.shop.ui import ShopUI
from module.logger import logger


class RewardShop(ShopUI):
    """Диспетчер магазинов наград.

    Pages: in: page_shop
    """

    def run_frequent(self):
        """Запустить высокочастотную задачу покупок в магазине.

        Pages: in: page_shop (вкладка общего магазина)

        Обрабатывает только общий магазин для часто выполняемых покупок.
        """
        self.ui_goto_shop()
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_GENERAL, main=self)
        if self.config.GeneralShop_Enable:
            GeneralShop_250814(self.config, self.device).run()
        else:
            logger.info('[Магазин — общий] Общий магазин отключён; пропуск')

        self.config.task_delay(server_update=True)

    def run_once(self):
        """Запустить разовую задачу покупок в магазинах.

        Pages: in: page_shop (вкладки заслуг, гильдии, ядра, медалей)

        Последовательно обрабатывает магазин заслуг, магазин гильдии, магазин ядра и магазин медалей.
        """
        # Магазин снабжения.
        self.ui_goto_shop()
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_MERIT, main=self)
        if self.config.MeritShop_Enable:
            MeritShop_250814(self.config, self.device).run()
        else:
            logger.info('[Магазин — снабжение] Магазин снабжения отключён; пропуск')

        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_GENERAL, main=self)
        self.shop_tab_250814.set(TAB_GUILD, main=self)
        if self.config.GuildShop_Enable:
            GuildShop_250814(self.config, self.device).run()
        else:
            logger.info('[Магазин — гильдия] Магазин гильдии отключён; пропуск')

        # Лимитированный магазин ядер, ежемесячный магазин ядер, медали и прототипы.
        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_MONTHLY, main=self)
        self.shop_tab_250814.set(TAB_CORE_MONTHLY, main=self)
        if self.config.CoreShop_Enable:
            CoreShop_250814(self.config, self.device).run()
        else:
            logger.info('[Магазин — ядра] Магазин ядер отключён; пропуск')

        self.device.click_record_clear()
        self.shop_nav_250814.set(NAV_MONTHLY, main=self)
        self.shop_tab_250814.set(TAB_MEDAL, main=self)
        if self.config.MedalShop2_Enable:
            MedalShop2_250814(self.config, self.device).run()
        else:
            logger.info('[Магазин — медали] Магазин медалей отключён; пропуск')

        self.config.task_delay(server_update=True)


if __name__ == '__main__':
    self = RewardShop('alas')
    self.device.screenshot()
    self.run_once()