"""Исполнитель покупок в магазине, предоставляющий базовую логику выбора товаров, проверки запасов и подтверждения покупки.
Используется во всех типах магазинов, поддерживает OCR подсчёта запасов и переработку при отставке кораблей.
"""

import re

import cv2

from module.base.timer import Timer
from module.exception import ScriptError
from module.logger import logger
from module.ocr.ocr import Digit, DigitCounter
from module.retire.retirement import Retirement
from module.shop.assets import *
from module.shop.base import ShopBase
from module.shop.shop_select_globals import *
from module.ui.assets import SHOP_BACK_ARROW


class StockCounter(DigitCounter):
    """OCR-счётчик запаса, используемый для распознавания количества запасов в интерфейсе выбора магазина.

    Предобработка переводит изображение в оттенки серого и инвертирует; постобработка исправляет
    типичные ошибки OCR (например, '55' исправляется на '5/5', '1515' — на '15/15').
    """

    def pre_process(self, image):
        """Предобработка OCR: преобразование в оттенки серого и инвертирование.

        Args:
            image: Входное изображение

        Returns:
            np.array: Инвертированное полутоновое изображение
        """
        r, g, b = cv2.split(image)
        image = cv2.max(cv2.max(r, g), b)

        return 255 - image

    def after_process(self, result):
        """Постобработка OCR: исправление типичных ошибок распознавания.

        Преобразует две подряд идущие цифры в формат 'X/Y' (например, '55' -> '5/5'),
        а четыре подряд идущие цифры — в формат 'XX/YY' (например, '1515' -> '15/15').
        """
        result = super().after_process(result)

        if re.match(r'^\d\d$', result):
            # 55 -> 5/5
            new = f'{result[0]}/{result[1]}'
            logger.info(f'[Магазин — покупка] Результат счётчика запасов {result} исправлен на {new}')
            result = new
        if re.match(r'^\d{4,}$', result):
            # 1515 -> 15/15
            new = f'{result[0:2]}/{result[2:4]}'
            logger.info(f'[Магазин — покупка] Результат счётчика запасов {result} исправлен на {new}')
            result = new

        return result


SHOP_SELECT_PR = [SHOP_SELECT_PR1, SHOP_SELECT_PR2, SHOP_SELECT_PR3]
OCR_SHOP_SELECT_STOCK = StockCounter(SHOP_SELECT_STOCK)

OCR_SHOP_AMOUNT = Digit(SHOP_AMOUNT, letter=(239, 239, 239), name='OCR_SHOP_AMOUNT')


class ShopClerk(ShopBase, Retirement):
    """Базовый обработчик покупок в магазине.

    Предоставляет общую логику выбора товара, ввода количества и подтверждения покупки.
    Подклассы переопределяют shop_buy_handle и shop_interval_clear для специфической обработки.
    """

    def shop_get_choice(self, item):
        """Получить значение конфигурации выбора для товара.

        По группе товара (pr/equipment и др.) и рангу считывает
        соответствующее значение настройки (например, номер серии PR, ранг снаряжения).

        Args:
            item: Объект товара со свойствами group и tier

        Returns:
            str: Выбранное значение из конфигурации

        Raises:
            ScriptError: Если параметр конфигурации не найден
        """
        group = item.group
        if group == 'pr':
            postfix = None
            for _ in range(3):
                if _:
                    self.device.sleep((0.3, 0.5))
                    self.device.screenshot()

                for idx, btn in enumerate(SHOP_SELECT_PR):
                    if self.appear(btn, offset=(20, 20)):
                        postfix = f'{idx + 1}'
                        break

                if postfix is not None:
                    break
                logger.warning('Не удалось определить серию PR; приложение может зависать или тормозить')
        else:
            postfix = f'_{item.tier.upper()}'

        ugroup = group.upper()
        # Новый UI магазина от 2025-08-14: при покупке PlateT4 класс нового UI имеет имя XXXShop_250814,
        # поэтому берём имя класса до символа "_"
        class_name = self.__class__.__name__.split("_")[0]
        try:
            return getattr(self.config, f'{class_name}_{ugroup}{postfix}')
        except Exception:
            logger.critical(f"[Магазин] Дядя, даже конфигурацию найти не можете? Нет никакого \'{class_name}_{ugroup}{postfix}\'! ❤")
            raise

    def shop_get_select(self, item):
        """Получить кнопку сетки выбора, соответствующую товару.

        По группе товара и выбранной настройке находит положение целевой кнопки в интерфейсе выбора.

        Args:
            item: Объект товара со свойством group

        Returns:
            Button: Целевая кнопка в интерфейсе выбора

        Raises:
            ScriptError: Если группа товара отсутствует в SELECT_ITEM_INFO_MAP
        """
        group = item.group
        if group not in SELECT_ITEM_INFO_MAP:
            logger.critical(f"[Магазин] Что ещё за группа товаров \'{group}\'? Дядя, вы из какого измерения? ❤")
            raise ScriptError

        # Получаем выбранный в конфигурации вариант товара
        choice = self.shop_get_choice(item)

        # Получаем соответствующую кнопку в интерфейсе выбора
        try:
            item_info = SELECT_ITEM_INFO_MAP[group]
            index = item_info['choices'][choice]
            if group == 'pr':
                for idx, btn in enumerate(SHOP_SELECT_PR):
                    if self.appear(btn, offset=(20, 20)):
                        series_key = f's{idx + 1}'
                        return item_info['grid'][series_key].buttons[index]
            else:
                return item_info['grid'].buttons[index]
        except Exception:
            logger.critical(f"[Магазин] В SELECT_ITEM_INFO_MAP такая серьёзная ошибка — дядя, вы что, тайком продали файлы ресурсов на выпивку? ❤")
            raise ScriptError

    def shop_buy_select_execute(self, item):
        """Выполнить операцию покупки с выбором (например, ящики снаряжения, чертежи и т.д.).

        В интерфейсе выбора нажимает на товар, считывает лимит запаса, настраивает количество и подтверждает.
        Использует ui_ensure_index для предотвращения превышения лимита запаса.

        Args:
            item: Покупаемый объект товара

        Returns:
            bool: Успешно ли выполнена покупка
        """
        select = self.shop_get_select(item)

        # Получаем лимит запаса; он может различаться между магазинами
        timeout = Timer(5, count=10).start()
        skip_first_screenshot = True
        limit = 0
        while 1:
            if timeout.reached():
                break
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            _, _, limit = OCR_SHOP_SELECT_STOCK.ocr(self.device.image)
            if limit:
                break

        if not limit:
            logger.critical(f"[Магазин] Пф-ф~ даже запас {item.name} посчитать не можете. Дядя, вам бы математику в детском саду повторить ❤")
            raise ScriptError

        # Периодически нажимаем, пока не появятся кнопки плюс/минус
        click_timer = Timer(3, count=6)
        select_offset = (500, 400)
        while 1:
            if click_timer.reached():
                self.device.click(select)
                click_timer.reset()

            self.device.screenshot()
            if self.appear(SELECT_MINUS, offset=select_offset) and self.appear(SELECT_PLUS, offset=select_offset):
                break
            else:
                continue

        # Вычисляем общее доступное количество покупок (валюта / цена за единицу)
        total = int(self._currency // item.price)
        diff = limit - total
        if diff > 0:
            limit = total

        # Оборачиваем OCR-функцию для ui_ensure_index, чтобы не купить больше доступного запаса
        def shop_buy_select_ensure_index(image):
            current, remain, _ = OCR_SHOP_SELECT_STOCK.ocr(image)
            if not current:
                group_case = item.group.title() if len(item.group) > 2 else item.group.upper()
                logger.info(f'{group_case} распродано; выход для предотвращения лишней покупки')
                return limit
            return remain

        self.ui_ensure_index(limit, letter=shop_buy_select_ensure_index, prev_button=SELECT_MINUS,
                             next_button=SELECT_PLUS,
                             skip_first_screenshot=True)
        self.device.click(SHOP_BUY_CONFIRM_SELECT)
        return True

    def shop_buy_amount_execute(self, item):
        """Выполнить операцию покупки с вводом количества (например, ящики деталей, учебники и т.д.).

        В интерфейсе ввода количества нажимает кнопку максимума, считывает лимит, настраивает количество и подтверждает.

        Args:
            item: Покупаемый объект товара

        Returns:
            bool: Успешно ли выполнена покупка

        Raises:
            ScriptError: Если OCR распознал количество 0
        """
        index_offset = (40, 20)

        # Используем OCR-приём из дока для точного определения области ввода количества
        self.appear(AMOUNT_MINUS, offset=index_offset)
        self.appear(AMOUNT_PLUS, offset=index_offset)
        area = OCR_SHOP_AMOUNT.buttons[0]
        OCR_SHOP_AMOUNT.buttons = [(AMOUNT_MINUS.button[2] + 3, area[1], AMOUNT_PLUS.button[0] - 3, area[3])]

        # Нажимаем кнопку максимума, получаем доступное количество и ждём стабилизации изображения
        self.appear_then_click(AMOUNT_MAX, offset=(50, 50))
        self.device.sleep((0.3, 0.5))
        timeout = Timer(5, count=10).start()
        limit = 0
        while 1:
            if timeout.reached():
                break
            self.device.screenshot()
            limit = OCR_SHOP_AMOUNT.ocr(self.device.image)
            if limit:
                break

        if not limit:
            logger.critical("[Магазин] OCR_SHOP_AMOUNT распознал 0. Дядя, неужели вы уже настолько разорились? ❤")
            raise ScriptError

        # Корректируем количество покупки (валюта / цена за единицу)
        total = int(self._currency // item.price)
        diff = limit - total
        if diff > 0:
            limit = total

        self.ui_ensure_index(limit, letter=OCR_SHOP_AMOUNT, prev_button=AMOUNT_MINUS, next_button=AMOUNT_PLUS,
                             skip_first_screenshot=True)
        self.device.click(SHOP_BUY_CONFIRM_AMOUNT)
        return True

    def shop_interval_clear(self):
        """Сбросить интервалы нажатий для кнопок интерфейса покупки.

        Подклассы могут переопределять этот метод для сброса интервалов специфических ассетов.
        """
        self.interval_clear(SHOP_BACK_ARROW)
        self.interval_clear(SHOP_BUY_CONFIRM)

    def shop_buy_handle(self, item):
        """Обработать интерфейс покупки (переопределяется в подклассах).

        Подклассы переопределяют метод в соответствии с особенностями своего магазина (выбор, количество и т.д.).

        Args:
            item: Покупаемый объект товара

        Returns:
            bool: Обнаружен и обработан ли интерфейс покупки
        """
        return False

    def shop_buy_execute(self, item, skip_first_screenshot=True):
        """Выполнить полный цикл состояний операции покупки.

        Через цикл состояний проходит весь путь от клика по товару до подтверждения покупки.
        Обрабатывает неожиданные ситуации: отставку кораблей, перекрытие, информационные полосы.

        Args:
            item: Покупаемый объект товара
            skip_first_screenshot: Пропускать ли первый снимок экрана
        """
        success = False
        self.shop_interval_clear()

        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(SHOP_BACK_ARROW, offset=(30, 30), interval=3):
                self.device.click(item)
                continue
            if self.appear_then_click(SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(SHOP_BACK_ARROW)
                continue
            if self.shop_buy_handle(item):
                self.interval_reset(SHOP_BACK_ARROW)
                continue
            if self.handle_retirement():
                self.interval_reset(SHOP_BACK_ARROW)
                continue
            if self.shop_obstruct_handle():
                self.interval_reset(SHOP_BACK_ARROW)
                success = True
                continue
            if self.info_bar_count():
                self.interval_reset(SHOP_BACK_ARROW)
                success = True
                continue

            # Условие завершения
            if success and self.appear(SHOP_BACK_ARROW, offset=(30, 30)):
                break

    def shop_buy(self):
        """Выполнить главный цикл покупок в магазине.

        Получает список товаров, считывает баланс через OCR, покупает по очереди, пока есть доступные товары и средства.
        Ограничено максимум 12 итерациями для предотвращения бесконечного цикла.

        Returns:
            bool: Успешно ли завершено (True — покупки завершены или недостаточно средств, False — баланс равен 0)
        """
        for _ in range(12):
            logger.hr('Покупки в магазине', level=2)
            # Сначала получаем список товаров: встроенная задержка позволяет OCR валюты отработать точнее
            items = self.shop_get_items()
            self.shop_currency()
            if self._currency <= 0:
                logger.warning(f'[Магазин — покупка] Текущие средства: {self._currency}; остановка')
                return False

            item = self.shop_get_item_to_buy(items)
            if item is None:
                logger.info('[Магазин — покупка] Покупки завершены')
                return True
            else:
                self.shop_buy_execute(item)
                continue

        logger.warning('Куплено слишком много товаров; остановка')
        return True
