"""
Логика управления магазином события.

Отвечает за процесс покупки в магазине события: обнаружение сетки товаров,
поиск и фильтрацию товаров, выбор количества и подтверждение покупки.
Поддерживает валюты PT и UR-очков и наследует EventShopUI для навигации
по интерфейсу магазина.

Pages: in: EVENT_SHOP
"""
import cv2
import numpy as np

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import color_similarity_2d, crop
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_3, GET_SHIP
from module.exception import RequestHumanTakeover
from module.logger import logger
from module.map_detection.utils import Points
from module.shop.assets import (
    AMOUNT_MAX,
    AMOUNT_MINUS,
    AMOUNT_PLUS,
    SHOP_BUY_CONFIRM,
    SHOP_BUY_CONFIRM_AMOUNT,
    SHOP_CLICK_SAFE_AREA,
)
from module.shop.clerk import OCR_SHOP_AMOUNT
from module.shop_event.assets import *
from module.shop_event.item import (
    DELTA_ITEM,
    DELTA_PRICE_BACKGROUND,
    ITEM_SHAPE,
    PRICE_BACKGROUND_COLOR,
    PRICE_THRESHOLD,
    EventShopItemGrid,
)
from module.shop_event.notification_policy import apply_event_shop_notification_policy
from module.shop_event.ui import EVENT_SHOP_SCROLL, EventShopUI
from module.ui_white.assets import BACK_ARROW_WHITE
from module.webui.event_shop_priority import (
    PriorityRuntimeItems,
    confirm_event_shop_purchase,
    prepare_event_shop_runtime_items,
)

DETECT_AREA = (221, 194, 1049, 632)
SCANNER_OVERLAP_IMAGE_MEAN_DELTA = 6.0


class ItemNotFoundError(Exception):
    pass


class EventShopClerk(EventShopUI):
    pt_image = None
    urpt_image = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            if not bool(
                self.config.cross_get(
                    keys="EventShop.Scheduler.Sensitive", default=False
                )
            ):
                self.config.cross_set(
                    keys="EventShop.Scheduler.Sensitive",
                    value=True,
                )
            self.config.override(Scheduler_Sensitive=True)
            apply_event_shop_notification_policy(self.config)
            logger.info(
                "[Магазин события] Критический режим задачи закреплён: автоматический перезапуск после исключения запрещён"
            )
        except Exception as exc:
            logger.error(
                f"[Магазин события] Не удалось закрепить критический режим задачи: {exc}"
            )
            raise RequestHumanTakeover from exc

    @staticmethod
    def _same_scanner_row(left, right):
        """Сравнить наблюдаемые числовые факты строки, не доверяя имени шаблона."""
        return all(
            getattr(left, field, None) == getattr(right, field, None)
            for field in ("price", "count", "total_count", "cost")
        )

    @staticmethod
    def _same_scanner_image(left, right):
        """Подтвердить совпадение строки независимым визуальным наблюдением."""
        left_image = getattr(left, "image", None)
        right_image = getattr(right, "image", None)
        if not isinstance(left_image, np.ndarray) or not isinstance(right_image, np.ndarray):
            return False
        if not left_image.size or not right_image.size:
            return False
        if left_image.shape != right_image.shape or left_image.dtype != right_image.dtype:
            return False
        delta = float(np.mean(cv2.absdiff(left_image, right_image)))
        return delta <= SCANNER_OVERLAP_IMAGE_MEAN_DELTA

    @staticmethod
    def _scanner_row_has_visual_diversity(items):
        """Проверить, что ряд не состоит только из визуально одинаковых товаров."""
        images = [getattr(item, "image", None) for item in items]
        if len(images) < 2 or any(not isinstance(image, np.ndarray) or not image.size for image in images):
            return False
        first = images[0]
        for image in images[1:]:
            if first.shape != image.shape or first.dtype != image.dtype:
                return True
            delta = float(np.mean(cv2.absdiff(first, image)))
            if delta > SCANNER_OVERLAP_IMAGE_MEAN_DELTA:
                return True
        return False

    @classmethod
    def _scanner_overlap_proven(cls, old_row, new_row):
        """Дедуплицировать overlap только при числовом и визуальном доказательстве."""
        if not old_row or len(old_row) != len(new_row):
            return False
        if not cls._scanner_row_has_visual_diversity(old_row):
            return False
        if not cls._scanner_row_has_visual_diversity(new_row):
            return False
        return all(
            cls._same_scanner_row(old, new) and cls._same_scanner_image(old, new)
            for old, new in zip(old_row, new_row)
        )

    @staticmethod
    def _prefer_amount_max(current, target, maximum_hint):
        """Использовать MAX только когда это доказанно сокращает путь до цели."""
        try:
            current = int(current)
            target = int(target)
            maximum_hint = int(maximum_hint)
        except (TypeError, ValueError, OverflowError):
            return False
        if current <= 0 or target <= 0 or maximum_hint < target:
            return False
        direct_clicks = abs(target - current)
        max_clicks = 1 + abs(maximum_hint - target)
        return max_clicks < direct_clicks

    def _get_event_shop_grid(self):
        mask = color_similarity_2d(self.device.image, PRICE_BACKGROUND_COLOR)
        cv2.inRange(mask, PRICE_THRESHOLD, 255, dst=mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=8)
        mask = crop(mask,
                    (DETECT_AREA[0], DETECT_AREA[1] + DELTA_PRICE_BACKGROUND[1], DETECT_AREA[2], DETECT_AREA[3]),
                    copy=False)
        buttons = TEMPLATE_COST_PRICE_BACKGROUND.match_multi(mask, similarity=0.6)
        points = Points([(0., p.area[1]) for p in buttons]).group(threshold=5)

        row = len(points)
        if row == 2:
            y = min(points[0][1], points[1][1]) + DETECT_AREA[1]
            delta_y = abs(points[0][1] - points[1][1])
        elif row == 1:
            y = points[0][1] + DETECT_AREA[1]
            delta_y = 215
        else:
            logger.warning(f"[Магазин события — покупка] Некорректное число рядов: {row}; предполагается, что полоса прокрутки сверху")
            y = 1 + DETECT_AREA[1]  # Начальная позиция на 1 пиксель ниже области обнаружения.
            delta_y = 215

        shop_grid = ButtonGrid(
            origin=(DETECT_AREA[0] + DELTA_ITEM[0], y + DELTA_ITEM[1]),
            delta=(169, delta_y),
            button_shape=ITEM_SHAPE,
            grid_shape=(5, row),
            name="EVENT_SHOP_GRID",
        )
        return shop_grid

    @cached_property
    def event_shop_items(self):
        event_shop_items = EventShopItemGrid(grids=None, templates={})
        event_shop_items.load_template_folder('./assets/shop/event')
        return event_shop_items

    def event_shop_get_items(self, scroll_pos=None):
        self.event_shop_items.grids = self._get_event_shop_grid()
        if self.config.SHOP_EXTRACT_TEMPLATE:
            self.event_shop_items.extract_template(self.device.image, './assets/shop/event')
        self.event_shop_items.predict(self.device.image, name=True, amount=True, cost=False,
                                      price=True, tag=True, counter=True, scroll_pos=scroll_pos)
        shop_items = self.event_shop_items.items
        if len(shop_items):
            min_row = self.event_shop_items.grids[0, 0].area[1]
            row = [str(item) for item in shop_items if item.button[1] == min_row]
            logger.info(f'[Магазин события — покупка] Ряд 1: {row}')
            row = [str(item) for item in shop_items if item.button[1] != min_row]
            logger.info(f'[Магазин события — покупка] Ряд 2: {row}')
            return shop_items
        else:
            logger.info('Товары магазина не найдены')
            return []

    def scan_all(self):
        items = []
        self.device.click_record_clear()

        logger.hr('Сканирование магазина события', level=2)
        EVENT_SHOP_SCROLL.set_top(main=self)
        while 1:
            new_items = self.event_shop_get_items(scroll_pos=EVENT_SHOP_SCROLL.cal_position(main=self))
            if len(items):
                old_last_row = [item for item in items if item.button[1] == items[-1].button[1]]
                new_first_row = [item for item in new_items if item.button[1] == new_items[0].button[1]]
                new_second_row = [item for item in new_items if item.button[1] != new_items[0].button[1]]
                if self._scanner_overlap_proven(old_last_row, new_first_row):
                    logger.info('[Магазин события — покупка] Повторяющиеся товары пропущены')
                    items += new_second_row
                else:
                    items += new_items
            else:
                items += new_items
            if EVENT_SHOP_SCROLL.at_bottom(main=self):
                logger.info('Достигнут конец магазина события')
                break
            else:
                EVENT_SHOP_SCROLL.next_page(main=self, page=0.66)
                continue
        try:
            return prepare_event_shop_runtime_items(self.config, items)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[Магазин события — приоритеты] Не удалось подготовить план покупок: {exc}"
            )
            return PriorityRuntimeItems([], observation_items=items)

    def event_shop_buy_item(self, item_to_buy, amount=None):
        scroll_pos = item_to_buy.scroll_pos
        EVENT_SHOP_SCROLL.set(scroll_pos, main=self)
        items = self.event_shop_get_items()
        items = [item for item in items if item.name == item_to_buy.name
                 and item.count == item_to_buy.count and item.price == item_to_buy.price]
        if len(items) == 0:
            logger.error(f'[Магазин события — покупка] Товар {item_to_buy} не найден в позиции прокрутки {scroll_pos}')
            logger.warning(f'[Магазин события — покупка] Будет предпринята попытка повторного запуска задачи')
            raise ItemNotFoundError(f'Товар {item_to_buy} не найден в позиции прокрутки {scroll_pos}')
        elif len(items) > 1:
            logger.warning(f'[Магазин события — покупка] В позиции прокрутки {scroll_pos} найдено несколько товаров {item_to_buy}; покупается первый')
        item = items[0]
        try:
            item_count = max(int(item.count), 0)
            requested = item_count if amount is None else max(int(amount), 0)
        except (TypeError, ValueError, OverflowError):
            item_count = 0
            requested = 0
        full_purchase = item_count > 0 and requested >= item_count
        # Корабельный товар может иметь несколько единиц запаса, но покупается по одной.
        if getattr(item, 'is_ship', False):
            buy_times = item.count if amount is None else min(amount, item.count)
            for _ in range(buy_times):
                self.event_shop_buy_item_execute(item, amount=1)
        else:
            self.event_shop_buy_item_execute(item, amount=amount)
        remaining_after = max(item_count - min(requested, item_count), 0)
        try:
            confirm_event_shop_purchase(
                self.config,
                item,
                full_purchase=full_purchase,
                remaining_after=remaining_after,
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[Магазин события — приоритеты] Покупка выполнена, но состояние приоритета не обновлено: {exc}"
            )

    def event_shop_buy_item_execute(self, item, amount):
        self.event_shop_handle_obstruct()
        executed = False
        amount_handled = False
        timer = Timer(2, count=4).start()
        for _ in self.loop():
            if self.handle_popup_confirm("meta_buy_confirm"):
                timer.reset()
                continue
            if self.appear(AMOUNT_MAX, offset=(20, 20)):
                if not amount_handled:
                    if amount is None:
                        self.device.click(AMOUNT_MAX)
                    else:
                        current = OCR_SHOP_AMOUNT.ocr(self.device.image)
                        if self._prefer_amount_max(current, amount, item.count):
                            self.device.click(AMOUNT_MAX)
                            skip_first_screenshot = False
                        else:
                            skip_first_screenshot = True
                        self.ui_ensure_index(
                            amount,
                            letter=OCR_SHOP_AMOUNT,
                            prev_button=AMOUNT_MINUS,
                            next_button=AMOUNT_PLUS,
                            skip_first_screenshot=skip_first_screenshot,
                        )
                    amount_handled = True
                    timer.reset()
                    continue
                else:
                    self.device.click(SHOP_BUY_CONFIRM_AMOUNT)
                    executed = True
                    timer.reset()
                    continue
            elif self.appear(SHOP_BUY_CONFIRM, offset=(20, 40)):
                self.device.click(SHOP_BUY_CONFIRM)
                executed = True
                timer.reset()
                continue
            elif self.appear(BACK_ARROW_WHITE, offset=(20, 20)):
                if not executed:
                    if timer.reached():
                        self.device.click(item)
                        timer.reset()
                    continue
                elif timer.reached():
                    break
            elif timer.reached() and self.event_shop_handle_obstruct():
                timer.reset()
                continue

    def event_shop_handle_obstruct(self):
        if self.handle_info_bar():
            return True
        if self.handle_get_meowfficer():
            return True
        if self.appear(GET_SHIP, offset=(20, 20), interval=2):
            logger.info(f'Перекрытие магазина: {GET_SHIP} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_1, offset=(20, 20), interval=2):
            logger.info(f'Перекрытие магазина: {GET_ITEMS_1} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_3, offset=(20, 20), interval=2):
            logger.info(f'Перекрытие магазина: {GET_ITEMS_3} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        return False