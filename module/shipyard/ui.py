"""Модуль операций с интерфейсом верфи, обрабатывающий навигацию и взаимодействие со страницами.

Включает выбор серии PR, чтение количества чертежей, OCR уровней разработки и исследований,
а также распознавание специфических элементов интерфейса для разных серверов.
"""

from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import area_pad
import module.config.server as server
from module.campaign.assets import OCR_COIN as CAMPAIGN_OCR_COIN
from module.handler.assets import LOGIN_ANNOUNCE
from module.logger import logger
from module.ocr.ocr import Digit
from module.shipyard.ui_globals import *
from module.ui.assets import SHIPYARD_CHECK
from module.ui.navbar import Navbar
from module.ui.page import page_main_white
from module.ui.ui import UI

OCR_COIN = Digit(
    CAMPAIGN_OCR_COIN,
    name='OCR_COIN',
    letter=(201, 201, 201) if server.server == 'jp' else (239, 239, 239),
    threshold=128,
)


class ShipyardNavbar(Navbar):
    def is_button_active(self, button, main):
        if main.image_color_count(button, color=(33, 113, 222), threshold=221, count=400):
            return True
        # Цвет области плеча Одина
        if main.image_color_count(button, color=(41, 85, 165), threshold=221, count=400):
            return True
        return False


class ShipyardUI(UI):
    def _shipyard_cannot_strengthen(self):
        """
        Проверка, невозможно ли дальнейшее усиление корабля.

        В интерфейсе DEV или FATE определяет, достиг ли текущий корабль
        максимального усиления для текущего уровня без возможности дальнейшего расхода чертежей.

        Returns:
            bool: Появилось ли уведомление о невозможности усиления.
        """
        if self.appear(SHIPYARD_PROGRESS_DEV, offset=(20, 20)) \
                or self.appear(SHIPYARD_PROGRESS_FATE, offset=(20, 20)) \
                or self.appear(SHIPYARD_LEVEL_NOT_ENOUGH_FATE, offset=(20, 20)) \
                or self.appear(SHIPYARD_LEVEL_NOT_ENOUGH_DEV, offset=(20, 20)):
            logger.info('Корабль достиг максимального усиления для текущего уровня; дальнейший расход чертежей невозможен')
            return True
        return False

    def _shipyard_get_append(self):
        """
        Получение суффикса текущей стадии разработки.

        Returns:
            str: 'FATE' или 'DEV'.
        """
        if self.appear(SHIPYARD_IN_FATE, offset=(20, 20)):
            return 'FATE'
        else:
            return 'DEV'

    def _shipyard_get_total(self):
        """
        Получение текущего значения общего числа чертежей на экране.

        Интерфейс игры различается между сезонами PR, а раскладка кнопок
        на стадиях DEV/FATE не совпадает, поэтому область OCR определяется динамически.

        Returns:
            tuple: (кнопка плюс, кнопка минус, распознанное число).
        """
        # Здесь игровой UI довольно сложный: наличие DEV/FATE и кнопки MAX меняет раскладку.
        # С кнопкой MAX: | - |   0   | + | | MAX |
        # Без кнопки MAX: | - |       0       | + |
        # Динамически определяем и формируем новую область OCR.
        append = self._shipyard_get_append()
        ocr = globals()[f'OCR_SHIPYARD_TOTAL_{append}']
        minus = globals()[f'SHIPYARD_MINUS_{append}']
        plus = globals()[f'SHIPYARD_PLUS_{append}']
        self.wait_until_appear(minus, offset=(20, 20), skip_first_screenshot=True)
        self.wait_until_appear(plus, offset=(150, 20), skip_first_screenshot=True)
        area = ocr.buttons[0]
        ocr.buttons = [(minus.button[2] + 3, area[1], plus.button[0] - 3, area[3])]

        return plus, minus, ocr.ocr(self.device.image)

    def _shipyard_ensure_index(self, count, skip_first_screenshot=True):
        """
        Установка требуемого количества расходуемых чертежей.

        Аналогично ui_ensure_index пытается скорректировать количество расхода до count.
        Если интерфейс не позволяет израсходовать всё количество, сохраняет максимально допустимое.

        Args:
            count (int): Целевое количество для расхода
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            int: Оставшееся неизрасходованное количество чертежей, либо None при ошибке
        """
        if count < 0:
            logger.warning('[Верфь — UI] count < 0; продолжение невозможно')
            return None

        current = diff = 0
        for _ in range(3):
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            plus, minus, current = self._shipyard_get_total()
            if current == count:
                logger.info(f'Можно израсходовать все {count} чертежей')
                return 0

            diff = count - current
            button = plus if diff > 0 else minus
            self.device.multi_click(button, n=diff, interval=(0.3, 0.5))
            self.device.sleep((0.3, 0.5))

        logger.info(f'[Верфь — UI] В текущем интерфейсе невозможно израсходовать {count} чертежей')
        logger.info(f'Можно израсходовать не более {current} / {count} чертежей')
        return diff

    def _shipyard_get_bp_count(self, index=0):
        """
        Получение количества чертежей корабля в указанной позиции.

        Args:
            index (int): Позиция целевого корабля (начиная с 1)

        Returns:
            int: Распознанное через OCR количество чертежей
        """
        # index(config.SHIPYARD_INDEX) начинается с 1
        if index <= 0 or index > len(SHIPYARD_BP_COUNT_GRID.buttons):
            logger.warning(f'[Верфь — UI] Не удалось получить количество по индексу {index}')
            return -1

        result = OCR_SHIPYARD_BP_COUNT_GRID.ocr(self.device.image)

        return result[index - 1]

    def _shipyard_in_ui(self):
        """
        Проверка нахождения в интерфейсе верфи.

        Returns:
            bool: Находится ли сейчас в области интерфейса верфи.
        """
        if self.appear(SHIPYARD_CHECK, offset=(20, 20)):
            return True
        if self.appear(SHIPYARD_IN_DEV, offset=(20, 20)):
            return True
        if self.appear(SHIPYARD_IN_FATE, offset=(20, 20)):
            return True

        return False

    def _shipyard_set_series(self, series=1, skip_first_screenshot=True):
        """
        Выбор отображаемой серии исследований.

        Args:
            series (int): Номер целевой серии исследований
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            bool: Успешно ли переключена серия
        """
        if series <= 0 or series > len(SHIPYARD_SERIES_GRID.buttons):
            logger.warning(f'Серия исследований {series} недоступна для выбора')
            return False

        self.ui_click(SHIPYARD_SERIES_SELECT_ENTER, appear_button=self._shipyard_in_ui,
                      check_button=SHIPYARD_SERIES_SELECT_CHECK,
                      skip_first_screenshot=skip_first_screenshot)
        series_button = SHIPYARD_SERIES_GRID.buttons[series - 1]
        self.ui_click(series_button, appear_button=SHIPYARD_SERIES_SELECT_CHECK,
                      check_button=self._shipyard_in_ui,
                      skip_first_screenshot=skip_first_screenshot)

        return True

    @cached_property
    def _shipyard_bottom_navbar(self):
        """
        Нижняя панель навигации верфи для переключения кораблей внутри выбранной серии.

        Позиции зависят от индивидуального прогресса игрока; пользователь должен подтвердить индекс.
        """
        return ShipyardNavbar(
            grids=SHIPYARD_FACE_GRID,
            inactive_color=(49, 60, 82), inactive_threshold=221, inactive_count=50)

    def shipyard_bottom_navbar_ensure(self, left=None, right=None, skip_first_screenshot=True):
        """
        Гарантированный переход на страницу корабля по заданному индексу.

        Переключает нижнюю панель навигации по индексу и ожидает полного завершения анимации перехода.

        Args:
            left (int): Индекс целевого корабля
            right (int): Индекс целевого корабля (справа)
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            bool: Успешно ли установлена панель навигации
        """
        if left is None and right is not None:
            left = right
            right = None
        if left is not None:
            if left <= 0 or left > len(SHIPYARD_FACE_GRID.buttons):
                logger.warning(f'[Верфь — UI] Индекс навигации {left} недоступен для выбора')
                return False

        ensured = False
        if self._shipyard_bottom_navbar.set(self, left=left, right=right, skip_first_screenshot=skip_first_screenshot):
            ensured = True

        # После настройки панели навигации ждём полного завершения перехода интерфейса
        confirm_timer = Timer(1.5, count=3).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Завершение
            if self._shipyard_in_ui():
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

        return ensured

    def shipyard_set_focus(self, series=1, index=1, skip_first_screenshot=True):
        """
        Установка фокуса верфи на указанную серию и корабль.

        Args:
            series (int): Номер целевой серии исследований
            index (int): Индекс целевого корабля
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана

        Returns:
            bool: Успешно ли установлен фокус
        """
        if series > 2 and index > 5:
            logger.warning(f'[Верфь — UI] Для серии исследований {series} допустимы только индексы 1–5; невозможно установить фокус на {index}')
            return False
        return self._shipyard_set_series(series, skip_first_screenshot) \
               and self.shipyard_bottom_navbar_ensure(left=index, skip_first_screenshot=skip_first_screenshot)

    def _shipyard_get_ship(self, skip_first_screenshot=True):
        """
        Обработка экрана получения корабля с завершённым исследованием.

        Pages: in: SHIPYARD_RESEARCH_COMPLETE, out: SHIPYARD_CONFIRM_DEV

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана
        """
        from module.combat.assets import GET_SHIP

        confirm_timer = Timer(1, count=2).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(SHIPYARD_RESEARCH_COMPLETE,
                                      interval=1, offset=(20, 20)):
                confirm_timer.reset()
                continue

            if self.story_skip():
                confirm_timer.reset()
                continue

            if self.appear_then_click(GET_SHIP, interval=1):
                confirm_timer.reset()
                continue

            if self.handle_popup_confirm('LOCK_SHIP'):
                confirm_timer.reset()
                continue

            if self.appear(SHIPYARD_CONFIRM_DEV, offset=(20, 20)):
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

    def _shipyard_buy_confirm(self, text, skip_first_screenshot=True):
        """
        Обработка интерфейса использования/покупки чертежей.

        Pages: in: SHIPYARD_CONFIRM_DEV/FATE, out: интерфейс верфи

        Args:
            text (str): Идентификатор подтверждения всплывающего окна
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана
        """
        success = False
        append = self._shipyard_get_append()
        button = globals()[f'SHIPYARD_CONFIRM_{append}']
        ocr_timer = Timer(10, count=10).start()
        confirm_timer = Timer(1, count=2).start()
        self.interval_clear(button)

        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if ocr_timer.reached():
                # Не удалось определить обычный выход; откатываемся к проверке OCR
                logger.warning('[Верфь — UI] Не удалось определить обычный выход; переход к проверке OCR')
                _, _, current = self._shipyard_get_total()
                if not current:
                    logger.info('Операция подтверждена; установлен флаг выхода')
                    self.interval_reset(button)
                    success = True
                ocr_timer.reset()
                continue

            if self.appear_then_click(button, offset=(20, 20), interval=3):
                continue

            if self.handle_popup_confirm(text):
                self.interval_reset(button)
                ocr_timer.reset()
                confirm_timer.reset()
                continue

            if self.story_skip():
                self.interval_reset(button)
                success = True
                ocr_timer.reset()
                confirm_timer.reset()
                continue

            if self.handle_info_bar():
                self.interval_reset(button)
                success = True
                ocr_timer.reset()
                confirm_timer.reset()
                continue

            # При переходе из завершённого DEV в FATE появляется информация о FATE
            if self.appear_then_click(LOGIN_ANNOUNCE, offset=area_pad((-300, 127, -300, 127), pad=-50), interval=3):
                self.interval_reset(button)
                success = True
                ocr_timer.reset()
                confirm_timer.reset()
                continue

            # Завершение
            if success and self._shipyard_in_ui():
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

    def _shipyard_buy_enter(self):
        """
        Переход в интерфейс покупки чертежей.

        Проверяет завершение исследования текущего корабля (при завершении получает корабль),
        при наличии фазы FATE переходит в её интерфейс.

        Returns:
            bool: Успешен ли переход в интерфейс покупки
        """
        if self.appear(SHIPYARD_RESEARCH_INCOMPLETE, offset=(20, 20)) \
                or self.appear(SHIPYARD_RESEARCH_IN_PROGRESS, offset=(20, 20)):
            logger.warning('[Верфь — UI] Не удалось открыть экран покупки: исследование текущего корабля ещё не завершено')
            return False

        if self.appear(SHIPYARD_RESEARCH_COMPLETE, offset=(20, 20)):
            self._shipyard_get_ship()

        if self.appear(SHIPYARD_GO_FATE, offset=(20, 20)):
            self.device.click(SHIPYARD_GO_FATE)
            self.wait_until_appear(SHIPYARD_IN_FATE, offset=(20, 20))

        return True

    def _shipyard_get_coin(self):
        """
        Получение текущего количества монет.

        Returns:
            int: Количество монет
        """
        if self.ui_page_appear(page_main_white):
            return MAIN_OCR_COIN.ocr(self.device.image)
        else:
            return OCR_COIN.ocr(self.device.image)
