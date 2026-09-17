"""Обработка навигации UI магазина: переключение вкладок и навигационных панелей.
Поддерживает нижнюю панель навигации и вкладки нового интерфейса от 2025-08-14.
"""

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.handler.assets import POPUP_CONFIRM
from module.logger import logger
from module.shop.assets import *
from module.ui.assets import ACADEMY_GOTO_MUNITIONS, SHOP_BACK_ARROW
from module.ui.navbar import Navbar
from module.ui.page import page_academy, page_munitions
from module.ui.switch import Switch
from module.ui.ui import UI


class ShopUI(UI):
    @cached_property
    def _shop_bottom_navbar(self):
        """
        Информация ниже основана на расположении после shop_swipe.
        У shop_bottom_navbar есть 5 вариантов:
            medal (медали)
            guild (гильдия)
            prototype (прототипы)
            core (ядро)
            merit (заслуги)
        """
        shop_bottom_navbar = ButtonGrid(
            origin=(399, 619), delta=(182, 0),
            button_shape=(56, 42), grid_shape=(5, 1),
            name='SHOP_BOTTOM_NAVBAR')

        return Navbar(grids=shop_bottom_navbar,
                      active_color=(33, 195, 239),
                      inactive_color=(181, 178, 181))

    def shop_bottom_navbar_ensure(self, left=None, right=None):
        """
        Убедиться в переходе на соответствующую страницу и её полной загрузке.
        Информация ниже основана на расположении после shop_swipe.

        Args:
            left (int): Зависит от положения панели навигации магазина
            right (int): Зависит от положения панели навигации магазина

        Returns:
            bool: Успешно ли установлена нижняя панель навигации
        """
        if self._shop_bottom_navbar.set(self, left=left, right=right):
            return True
        return False

    @cached_property
    def shop_nav_250814(self):
        """
        Переключатель верхней панели навигации магазина (версия 250814).

        Содержит пункты навигации «Общий» и «Ежемесячный»
        для переключения между основными категориями магазинов.

        Pages:
            in: page_munitions
        """
        switch = Switch('shop_nav_250814', is_selector=True, offset=(20, 20))
        switch.add_state(NAV_GENERAL, check_button=NAV_GENERAL)
        switch.add_state(NAV_MONTHLY, check_button=NAV_MONTHLY)
        return switch

    @cached_property
    def shop_tab_250814(self):
        """
        Переключатель вкладок категорий магазина (версия 250814).

        Содержит 9 вкладок: Общий, Заслуги, Гильдия, META, Награды,
        Ограниченное ядро, Ежемесячное ядро, Медали, Прототипы.

        Pages:
            in: page_munitions
        """
        switch = Switch('shop_tab_250814', is_selector=True, offset=(20, 20))
        switch.add_state(TAB_GENERAL, check_button=TAB_GENERAL)
        switch.add_state(TAB_MERIT, check_button=TAB_MERIT)
        switch.add_state(TAB_GUILD, check_button=TAB_GUILD)
        switch.add_state(TAB_META, check_button=TAB_META)
        switch.add_state(TAB_PRIZE, check_button=TAB_PRIZE)
        switch.add_state(TAB_CORE_LIMITED, check_button=TAB_CORE_LIMITED)
        switch.add_state(TAB_CORE_MONTHLY, check_button=TAB_CORE_MONTHLY)
        switch.add_state(TAB_MEDAL, check_button=TAB_MEDAL)
        switch.add_state(TAB_PROTOTYPE, check_button=TAB_PROTOTYPE)
        return switch

    def shop_refresh(self):
        """
        Выполнить операцию обновления магазина.

        Процесс: нажать кнопку обновления, дождаться всплывающего окна подтверждения,
        подтвердить и вернуть результат. У кнопки обновления есть два активных цветовых состояния;
        тёмный цвет означает недоступность обновления.

        Pages:
            in: page_munitions (SHOP_BACK_ARROW видна)

        Returns:
            bool: Успешно ли выполнено обновление
        """
        logger.info('[Магазин — UI] Обновление магазина')
        refreshed = False

        # Нажимаем кнопку обновления и ждём появления окна подтверждения
        for _ in self.loop():
            if self.appear(POPUP_CONFIRM, offset=(30, 30)):
                break
            # SHOP_REFRESH_CHECK — значок обновления
            # SHOP_REFRESH — значок обновления с фоном
            if self.appear(SHOP_REFRESH_CHECK, offset=(30, 30), interval=3):
                # У активного SHOP_REFRESH есть два цветовых состояния
                if self.image_color_count(SHOP_REFRESH.button, color=(49, 142, 207), threshold=221, count=50):
                    self.device.click(SHOP_REFRESH)
                    continue
                if self.image_color_count(SHOP_REFRESH.button, color=(54, 117, 161), threshold=221, count=50):
                    self.device.click(SHOP_REFRESH)
                    continue
                if self.image_color_count(SHOP_REFRESH.button, color=(52, 74, 94), threshold=221, count=50):
                    logger.info('[Магазин — UI] Обновление недоступно')
                    break
                # Не используем continue; обрабатываем как отсутствие совпадения SHOP_REFRESH
                self.interval_clear(SHOP_REFRESH)

        # Обрабатываем окно подтверждения и ждём возврата на главный экран магазина
        for _ in self.loop():
            if self.appear(SHOP_BACK_ARROW, offset=(30, 30)):
                break
            if self.appear(SHOP_BUY_CONFIRM_MISTAKE, interval=3, offset=(200, 200)):
                logger.warning('[Магазин — UI] Ошибка подтверждения обновления')
                self.ui_click(SHOP_CLICK_SAFE_AREA, appear_button=POPUP_CONFIRM, check_button=SHOP_BACK_ARROW,
                              offset=(20, 30), skip_first_screenshot=True)
                refreshed = False
                break
            if self.handle_popup_confirm('SHOP_REFRESH_CONFIRM'):
                refreshed = True
                continue

        self.handle_info_bar()
        return refreshed

    def ui_goto_shop(self):
        """
        Перейти к page_munitions (магазин припасов).
        Этот маршрут гарантирует, что вход осуществляется в общий магазин.

        Pages:
            in: Any
            out: page_munitions
        """
        if self.ui_get_current_page() == page_munitions:
            logger.info(f'[Магазин — UI] Уже открыта страница {page_munitions}')
            return

        self.ui_ensure(page_academy)

        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(page_munitions.check_button, offset=(20, 20)):
                break

            # Используем большой offset, поскольку камеру в академии можно перемещать
            if self.appear_then_click(ACADEMY_GOTO_MUNITIONS, offset=(200, 200), interval=5):
                continue
