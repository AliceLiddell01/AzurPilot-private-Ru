"""
Модуль покупки мебели для общежития.

Автоматически обнаруживает и покупает временную мебель в магазине мебели общежития. Включает:
- Переход в магазин мебели общежития и просмотр сведений о мебели
- Распознавание OCR баланса монет мебели и цен для проверки достаточности средств
- Выбор покупки набора или всех предметов в соответствии с конфигурацией
- Поддержку регулярной автоматической проверки и покупки с фиксированным интервалом (по умолчанию 6 дней)
"""
from datetime import timedelta

from module.combat.assets import GET_SHIP
from module.config.time_source import now as current_time
from module.dorm.assets import *
from module.exercise.assets import EXERCISE_PREPARATION
from module.logger import logger
from module.ocr.ocr import Digit
from module.ui.assets import DORM_CHECK
from module.ui.ui import UI

OCR_FURNITURE_COIN = Digit(OCR_DORM_FURNITURE_COIN, letter=(107, 89, 82), threshold=128, alphabet='0123456789', name='OCR_FURNITURE_COIN')
OCR_FURNITURE_PRICE = Digit(OCR_DORM_FURNITURE_PRICE, letter=(255, 247, 247), threshold=64, alphabet='0123456789', name='OCR_FURNITURE_PRICE')

CHECK_INTERVAL = 6  # Check every 6 days
# Only for click
FURNITURE_BUY_BUTTON = {
    "all": DORM_FURNITURE_BUY_ALL,
    "set": DORM_FURNITURE_BUY_SET
}


class BuyFurniture(UI):
    """
    Обработчик покупки мебели, отвечающий за автоматическое обнаружение и покупку временной мебели.

    Наследует UI, использует OCR для распознавания баланса монет мебели и цен,
    автоматически совершая покупку при обнаружении временной мебели. Поддерживает режимы покупки набором и покупки всех предметов.

    Attributes:
        CHECK_INTERVAL (int): Интервал проверки в днях, по умолчанию 6 дней.
        FURNITURE_BUY_BUTTON (dict): Сопоставление кнопок покупки, содержит варианты "all" и "set".
    """

    def enter_first_furniture_details_page(self, skip_first_screenshot=False):
        """
        Pages:
            in: page_dorm or DORM_FURNITURE_SHOP_ENTER(furniture shop page)
            out: 
        """
        self.interval_clear((DORM_CHECK, DORM_FURNITURE_DETAILS_ENTER,
                             DORM_FURNITURE_SHOP_FIRST,))
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Enter furniture shop page from page_dorm, only need to enter once
            if self.appear(DORM_CHECK, offset=(20, 20), interval=3):
                self.device.click(DORM_FURNITURE_SHOP_ENTER)
                self.interval_reset([GET_SHIP, EXERCISE_PREPARATION])
                continue

            if self.appear(DORM_FURNITURE_SHOP_FIRST_SELECTED, offset=(20, 20)):
                self.interval_reset([GET_SHIP, EXERCISE_PREPARATION])
                # Enter furniture details page from furniture shop page
                if self.appear(DORM_FURNITURE_DETAILS_ENTER, offset=(20, 20), interval=3):
                    self.device.click(DORM_FURNITURE_DETAILS_ENTER)
                    continue
            # After buy furniture, current furniture in store not first on list below.
            # Re select the first piece of furniture on left side of furniture list below.
            elif self.appear(DORM_FURNITURE_SHOP_FIRST, offset=(20, 20), interval=3):
                self.device.click(DORM_FURNITURE_SHOP_FIRST)
                self.interval_reset([GET_SHIP, EXERCISE_PREPARATION])
                continue

            if self.appear(DORM_FURNITURE_DETAILS_QUIT, offset=(20, 20)):
                break

            if self.ui_additional(get_ship=False):
                self.interval_clear(DORM_CHECK)
                continue

    def furniture_shop_quit(self, skip_first_screenshot=False):
        """
        Pages:
            in: DORM_FURNITURE_DETAILS_ENTER (furniture shop page)
            out: page_dorm
        """
        self.interval_clear(DORM_FURNITURE_DETAILS_ENTER)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.appear(DORM_CHECK, offset=(20, 20)):
                break

            if self.appear(DORM_FURNITURE_DETAILS_ENTER, offset=(20, 20), interval=3):
                self.device.click(DORM_FURNITURE_SHOP_QUIT)
                continue

    def furniture_details_page_quit(self, skip_first_screenshot=False):
        """
        Pages:
            in: DORM_FURNITURE_DETAILS_QUIT (furniture details page)
            out: DORM_FURNITURE_DETAILS_ENTER (furniture shop page)
        """
        self.interval_clear(DORM_FURNITURE_DETAILS_QUIT)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.appear(DORM_FURNITURE_DETAILS_ENTER, offset=(20, 20)):
                break

            if self.appear(DORM_FURNITURE_DETAILS_QUIT, offset=(20, 20), interval=3):
                self.device.click(DORM_FURNITURE_DETAILS_QUIT)
                continue

    def furniture_payment_enter(self, buy_button: Button, skip_first_screenshot=False):
        """
        Args:
            buy_button(Button): It can only be
                                DORM_FURNITURE_BUY_SET or DORM_FURNITURE_BUY_ALL

        Page:
            in: DORM_FURNITURE_DETAILS_QUIT (furniture details page)
            out: DORM_FURNITURE_BUY_CONFIRM (furniture payment page)
        """
        self.interval_clear(DORM_FURNITURE_DETAILS_QUIT)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DORM_FURNITURE_BUY_CONFIRM):
                break

            if self.appear(DORM_FURNITURE_DETAILS_QUIT, interval=3):
                self.device.click(buy_button)

    def buy_furniture_confirm(self, skip_first_screenshot=False):
        """
        Click DORM_FURNITURE_BUY_CONFIRM and back to furniture details page

        Pages:
            in: DORM_FURNITURE_BUY_CONFIRM (furniture payment page)
            out: DORM_FURNITURE_DETAILS_QUIT (furniture details page)
        """
        self.interval_clear(DORM_FURNITURE_BUY_CONFIRM)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(DORM_FURNITURE_DETAILS_QUIT):
                break

            if self.appear(DORM_FURNITURE_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.device.click(DORM_FURNITURE_BUY_CONFIRM)

    def buy_furniture_once(self, buy_option: str):
        """
        Args:
            buy_option(str): It can only be "all" or "set"
                             depend on config.BuyFurniture_BuyOption
        Returns:
            bool: True if furniture coin > furniture price, else False

        Pages:
            in: DORM_FURNITURE_DETAILS_QUIT (furniture details page)
        """
        buy_button: Button = FURNITURE_BUY_BUTTON[buy_option]
        coin = OCR_FURNITURE_COIN.ocr(self.device.image)

        self.furniture_payment_enter(buy_button, skip_first_screenshot=True)
        price = OCR_FURNITURE_PRICE.ocr(self.device.image)

        # Successful or failed buy will have popup and back to furniture details page,
        # produce the result from furniture coin compare to furniture price.
        if coin >= price > 0:
            logger.info(f"[Общежитие — мебель] Монет мебели достаточно; покупка {buy_option}")
            buy_successful = True
        else:
            logger.info(f"[Общежитие — мебель] Недостаточно монет мебели; покупки завершены")
            buy_successful = False
        self.buy_furniture_confirm(skip_first_screenshot=True)
        self.furniture_details_page_quit(skip_first_screenshot=True)
        return buy_successful

    def _buy_furniture_run(self):
        """
        Enter first furniture details page and check furniture is time-limited,
        if appear countdown, buy this furniture.
        Return:
            bool: True if Successfully bought,
                  False if Failed buy
        """
        self.enter_first_furniture_details_page()
        if self.match_template_color(DORM_FURNITURE_COUNTDOWN, offset=(20, 20)):
            logger.info("[Общежитие — мебель] Найдена доступная временная мебель")

            if self.buy_furniture_once(self.config.BuyFurniture_BuyOption):
                logger.info("[Общежитие — мебель] Поиск следующего временного предмета мебели")
                return True  # continue
            else:
                return False  # break
        else:
            logger.info("[Общежитие — мебель] Временная мебель не найдена")
            return False  # break

    def buy_furniture_run(self):
        """
        Pages:
            in: DORM_FURNITURE_DETAILS_ENTER (furniture shop page)
            out: page_dorm
        """
        logger.info("[Общежитие — мебель] Начало покупки мебели")
        while 1:
            if self._buy_furniture_run():
                continue
            else:
                break
        # Quit to page_dorm
        logger.info("[Общежитие — мебель] Возврат на страницу общежития")
        self.furniture_details_page_quit(skip_first_screenshot=True)
        self.furniture_shop_quit(skip_first_screenshot=True)
        self.config.BuyFurniture_LastRun = current_time().replace(microsecond=0)

    def run(self):
        """
        Run Buy Furniture
        """
        logger.attr("Последний запуск", self.config.BuyFurniture_LastRun)
        logger.attr("Интервал проверки", CHECK_INTERVAL)

        time_run = self.config.BuyFurniture_LastRun + timedelta(days=CHECK_INTERVAL)
        logger.info(f"[Общежитие — мебель] Время запуска задачи: {time_run}")

        if current_time().replace(microsecond=0) < time_run:
            logger.info("[Общежитие — мебель] Время запуска ещё не наступило; пропуск")
            return

        self.buy_furniture_run()
