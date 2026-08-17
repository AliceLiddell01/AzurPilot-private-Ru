"""
Логика управления магазином события.

Отвечает за процесс покупки в магазине события: обнаружение сетки товаров,
поиск и фильтрацию товаров, выбор количества и подтверждение покупки.
Поддерживает валюты PT и UR-очков и наследует EventShopUI для навигации
по интерфейсу магазина.

Страница входа: EVENT_SHOP
"""
from collections.abc import Mapping

import cv2
import numpy as np

from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import color_similarity_2d, crop
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_3, GET_SHIP
from module.exception import GameStuckError, RequestHumanTakeover
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
SCANNER_PARTIAL_OVERLAP_MIN_MATCHES = 2


class ItemNotFoundError(Exception):
    pass


class EventShopClerk(EventShopUI):
    pt_image = None
    urpt_image = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            # Sensitive нужен для безопасного текущего запуска EventShop, но это
            # инвариант выполнения самой задачи, а не пользовательская настройка.
            # Поэтому не сохраняем его через технические методы cross_set/save и не меняем конфиг
            # только из-за создания обработчика магазина или открытия WebUI.
            self.config.override(Scheduler_Sensitive=True)
            apply_event_shop_notification_policy(self.config)
            logger.info(
                "[Магазин события] Критический режим текущего запуска закреплён: автоматический перезапуск после исключения запрещён"
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.error(
                f"[Магазин события] Не удалось закрепить критический режим задачи: {exc}"
            )
            raise RequestHumanTakeover from exc

    def _event_shop_catalog_spec(self):
        """Вернуть EventSpec, уже закреплённый контроллером на текущий проход."""
        artifact = getattr(self, "_event_shop_current_artifact", None)
        if not isinstance(artifact, Mapping):
            return None
        spec = artifact.get("event_spec")
        return spec if isinstance(spec, Mapping) else None

    @staticmethod
    def _same_scanner_row(left, right):
        """Сравнить независимо считанные факты товара, не доверяя вычисленной валюте."""
        left_row_id = getattr(left, "catalog_row_id", None)
        right_row_id = getattr(right, "catalog_row_id", None)
        if left_row_id is not None and right_row_id is not None and left_row_id != right_row_id:
            return False
        return all(
            getattr(left, field, None) == getattr(right, field, None)
            for field in ("price", "count", "total_count", "amount")
        )

    @staticmethod
    def _same_scanner_image(left, right):
        """Подтвердить совпадение товара независимым визуальным наблюдением."""
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
        if len(images) < 2 or any(
            not isinstance(image, np.ndarray) or not image.size for image in images
        ):
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
        """Убрать дубликаты целого перекрывающегося ряда только при полном доказательстве."""
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

    @classmethod
    def _scanner_matched_subset_has_visual_diversity(cls, old_row, new_row, matches):
        """Требовать визуальное разнообразие именно совпавшей части ряда."""
        old_matched = [
            item for item, matched in zip(old_row, matches) if matched
        ]
        new_matched = [
            item for item, matched in zip(new_row, matches) if matched
        ]
        if len(old_matched) < SCANNER_PARTIAL_OVERLAP_MIN_MATCHES:
            return False
        return (
            cls._scanner_row_has_visual_diversity(old_matched)
            and cls._scanner_row_has_visual_diversity(new_matched)
        )

    @classmethod
    def _scanner_overlap_remainder(cls, old_row, new_row):
        """Убрать доказанные товары перекрытия, даже если соседний шаблон распознан иначе.

        Полностью одинаковый визуально однородный ряд по-прежнему сохраняется:
        для него недостаточно независимых признаков, чтобы отличить перекрытие от
        двух настоящих одинаковых рядов. Для частичного перекрытия требуется как
        минимум два совпавших по позиции товара.
        """
        if not old_row or len(old_row) != len(new_row):
            return list(new_row)
        matches = [
            cls._same_scanner_row(old, new) and cls._same_scanner_image(old, new)
            for old, new in zip(old_row, new_row)
        ]
        if all(matches):
            return [] if cls._scanner_overlap_proven(old_row, new_row) else list(new_row)
        if sum(matches) < SCANNER_PARTIAL_OVERLAP_MIN_MATCHES:
            return list(new_row)
        if not cls._scanner_matched_subset_has_visual_diversity(
            old_row, new_row, matches
        ):
            return list(new_row)
        return [item for item, matched in zip(new_row, matches) if not matched]

    @staticmethod
    def _has_unresolved_template_items(items):
        return any(str(getattr(item, "name", "") or "").isdigit() for item in items)

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

    @staticmethod
    def _purchase_item_matches(item, target):
        """Сопоставить цель покупки только по фактам текущего снимка сканера."""
        target_row_id = getattr(target, "catalog_row_id", None)
        item_row_id = getattr(item, "catalog_row_id", None)
        if target_row_id is not None:
            if item_row_id != target_row_id:
                return False
        return all(
            getattr(item, field, None) == getattr(target, field, None)
            for field in ("name", "price", "count", "total_count", "amount")
        )

    @staticmethod
    def _clamp_scroll_position(value):
        value = float(value)
        return min(max(value, 0.0), 1.0)

    @classmethod
    def _purchase_reidentify_positions(cls, item_to_buy):
        """Построить небольшой ограниченный набор позиций прокрутки для повторной идентификации.

        Сохранённая позиция полосы прокрутки не является пиксельно точным якорем: после
        дискретного сдвига фактическая позиция может отличаться на несколько
        процентов, из-за чего крайний ряд остаётся видимым человеку, но фон цены
        уже выходит из области детектора. Поэтому после исходной точки разрешены
        только две соседние проверки без действий покупки.

        Первое направление выводится из исходной вертикальной позиции карточки:
        нижний ряд сначала сдвигается вверх по списку, верхний — вниз.
        """
        try:
            base = float(getattr(item_to_buy, "scroll_pos", None))
        except (TypeError, ValueError, OverflowError) as exc:
            raise GameStuckError(
                "[Магазин события — покупка] У товара отсутствует корректная позиция прокрутки для повторной идентификации"
            ) from exc
        if not np.isfinite(base):
            raise GameStuckError(
                "[Магазин события — покупка] Позиция прокрутки товара не является конечным числом"
            )
        base = cls._clamp_scroll_position(base)

        reidentify_threshold = float(
            getattr(EVENT_SHOP_SCROLL, "reidentify_drag_threshold", 0.0) or 0.0
        )
        edge_threshold = float(
            getattr(EVENT_SHOP_SCROLL, "edge_threshold", 0.0) or 0.0
        )
        probe_step = max(reidentify_threshold * 2.0, edge_threshold)
        if probe_step <= 0:
            return [base]

        detector_center_y = (DETECT_AREA[1] + DETECT_AREA[3]) / 2.0
        direction = 1.0
        button = getattr(item_to_buy, "button", None)
        try:
            button_center_y = (float(button[1]) + float(button[3])) / 2.0
        except (TypeError, ValueError, IndexError, KeyError):
            button_center_y = detector_center_y
        if button_center_y < detector_center_y:
            direction = -1.0

        raw_positions = (
            base,
            base + direction * probe_step,
            base - direction * probe_step,
        )
        positions = []
        for value in raw_positions:
            value = cls._clamp_scroll_position(value)
            if not any(abs(value - existing) < 1e-9 for existing in positions):
                positions.append(value)
        return positions

    def _reidentify_event_shop_item(self, item_to_buy):
        """Найти цель рядом с исходной позицией прокрутки без действий покупки."""
        positions = self._purchase_reidentify_positions(item_to_buy)
        attempts = []
        sentinel = object()
        previous = getattr(self, "_scan_extract_templates", sentinel)
        self._scan_extract_templates = False
        try:
            for index, position in enumerate(positions, start=1):
                EVENT_SHOP_SCROLL.set_precise(position, main=self)
                items = self.event_shop_get_items()
                matches = [
                    item for item in items if self._purchase_item_matches(item, item_to_buy)
                ]
                attempts.append(
                    {
                        "requested": round(float(position), 6),
                        "observed": len(items),
                        "matched": len(matches),
                    }
                )
                if not matches:
                    continue
                if index > 1:
                    logger.info(
                        "[Магазин события — покупка] Товар повторно идентифицирован на соседней позиции прокрутки "
                        f"после {index} попыток: {attempts}"
                    )
                if len(matches) > 1:
                    logger.error(
                        f"[Магазин события — покупка] Найдено несколько совпадений {item_to_buy}; нажатие покупки заблокировано"
                    )
                    raise GameStuckError(
                        "[Магазин события — покупка] Повторная идентификация неоднозначна; нажатие покупки заблокировано"
                    )
                return matches[0]
        finally:
            if previous is sentinel:
                self.__dict__.pop("_scan_extract_templates", None)
            else:
                self._scan_extract_templates = previous

        logger.error(
            f"[Магазин события — покупка] Товар {item_to_buy} не подтверждён ни на одной проверенной позиции прокрутки: {attempts}"
        )
        raise GameStuckError(
            "[Магазин события — покупка] Повторная идентификация товара не удалась; нажатие покупки заблокировано"
        )

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
            y = 1 + DETECT_AREA[1]
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
        self.event_shop_items.set_catalog_spec(self._event_shop_catalog_spec())
        extract_templates = bool(getattr(self, "_scan_extract_templates", True))
        if bool(getattr(self.config, "SHOP_EXTRACT_TEMPLATE", False)) and extract_templates:
            self.event_shop_items.extract_template(self.device.image, './assets/shop/event')
        self.event_shop_items.predict(
            self.device.image,
            name=True,
            amount=True,
            cost=False,
            price=True,
            tag=True,
            counter=True,
            scroll_pos=scroll_pos,
        )
        shop_items = self.event_shop_items.items
        if len(shop_items):
            min_row = self.event_shop_items.grids[0, 0].area[1]
            row = [str(item) for item in shop_items if item.button[1] == min_row]
            logger.info(f'[Магазин события — покупка] Ряд 1: {row}')
            row = [str(item) for item in shop_items if item.button[1] != min_row]
            logger.info(f'[Магазин события — покупка] Ряд 2: {row}')
            return shop_items
        logger.info('Товары магазина не найдены')
        return []

    def _scan_event_shop_observations(self, *, extract_templates=True):
        items = []
        sentinel = object()
        previous = getattr(self, "_scan_extract_templates", sentinel)
        self._scan_extract_templates = bool(extract_templates)
        try:
            EVENT_SHOP_SCROLL.set_top(main=self)
            while 1:
                new_items = self.event_shop_get_items(
                    scroll_pos=EVENT_SHOP_SCROLL.cal_position(main=self),
                )
                if items and new_items:
                    old_last_row = [
                        item for item in items if item.button[1] == items[-1].button[1]
                    ]
                    new_first_row = [
                        item for item in new_items if item.button[1] == new_items[0].button[1]
                    ]
                    new_second_row = [
                        item for item in new_items if item.button[1] != new_items[0].button[1]
                    ]
                    remainder = self._scanner_overlap_remainder(old_last_row, new_first_row)
                    if len(remainder) != len(new_first_row):
                        logger.info(
                            '[Магазин события — покупка] Доказанные повторяющиеся товары overlap пропущены'
                        )
                        items += remainder + new_second_row
                    else:
                        items += new_items
                else:
                    items += new_items
                if EVENT_SHOP_SCROLL.at_bottom(main=self):
                    logger.info('Достигнут конец магазина события')
                    break
                EVENT_SHOP_SCROLL.next_page(main=self, page=0.66)
        finally:
            if previous is sentinel:
                self.__dict__.pop("_scan_extract_templates", None)
            else:
                self._scan_extract_templates = previous
        return items

    def scan_all(self):
        self.device.click_record_clear()
        logger.hr('Сканирование магазина события', level=2)
        items = self._scan_event_shop_observations(extract_templates=True)

        if (
            bool(getattr(self.config, "SHOP_EXTRACT_TEMPLATE", False))
            and self._has_unresolved_template_items(items)
        ):
            logger.info(
                '[Магазин события — товар] После извлечения шаблонов остались временные numeric identity; '
                'выполняется один полный стабилизирующий перескан без нового извлечения'
            )
            self.event_shop_items.load_template_folder('./assets/shop/event')
            items = self._scan_event_shop_observations(extract_templates=False)

        try:
            return prepare_event_shop_runtime_items(self.config, items)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[Магазин события — приоритеты] Не удалось подготовить план покупок: {exc}"
            )
            return PriorityRuntimeItems([], observation_items=items)

    def event_shop_buy_item(self, item_to_buy, amount=None):
        item = self._reidentify_event_shop_item(item_to_buy)
        try:
            item_count = max(int(item.count), 0)
            requested = item_count if amount is None else max(int(amount), 0)
        except (TypeError, ValueError, OverflowError):
            item_count = 0
            requested = 0
        full_purchase = item_count > 0 and requested >= item_count
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
                self.device.click(SHOP_BUY_CONFIRM_AMOUNT)
                executed = True
                timer.reset()
                continue
            if self.appear(SHOP_BUY_CONFIRM, offset=(20, 40)):
                self.device.click(SHOP_BUY_CONFIRM)
                executed = True
                timer.reset()
                continue
            if self.appear(BACK_ARROW_WHITE, offset=(20, 20)):
                if not executed:
                    if timer.reached():
                        self.device.click(item)
                        timer.reset()
                    continue
                if timer.reached():
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
