"""Навигация и определение состояния интерфейса магазина события.

Содержит проверку страницы магазина, OCR баланса, управление полосой прокрутки
и навигацию по вкладкам. EventShopScroll учитывает особенности полосы прокрутки
магазина события, а также поддерживаются проверка доступности магазина по срокам
и чтение валютного баланса.

Pages: in: EVENT_SHOP
"""
import re
from datetime import datetime, timedelta
from threading import RLock
from time import monotonic, sleep

import cv2
import numpy as np

import module.config.server as server
from module.base.button import ButtonGrid
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import rgb2luma, crop, color_similarity_2d
from module.config.time_source import now as current_time
from module.config.utils import server_time_offset
from module.exception import GameStuckError
from module.logger import logger
from module.meowfficer.assets import MEOWFFICER_GET_CHECK, MEOWFFICER_TRAIN_CLICK_SAFE_AREA
from module.meowfficer.collect import SWITCH_LOCK
from module.ocr.ocr import Ocr, Digit
from module.shop.assets import SHOP_OCR_BALANCE, SHOP_OCR_OIL_CHECK, SHOP_OCR_OIL
from module.shop_event.assets import *
from module.ui.navbar import Navbar
from module.ui.scroll import Scroll
from module.ui.ui import UI

EVENT_SHOP_SETTLE_AREA = (221, 194, 1049, 632)
EVENT_SHOP_SETTLE_SAMPLE_INTERVAL = 0.08
EVENT_SHOP_SETTLE_TIMEOUT = 1.5
EVENT_SHOP_SETTLE_REQUIRED_PAIRS = 3
EVENT_SHOP_SETTLE_PIXEL_DELTA = 12
EVENT_SHOP_SETTLE_CHANGED_RATIO = 0.015
EVENT_SHOP_SETTLE_MEAN_DELTA = 1.5


class EventShopScroll(Scroll):
    terminal_drag_threshold = 0.02
    reidentify_drag_threshold = 0.02
    _set_lock = RLock()

    def _drag_threshold_for_target(self, position):
        if position <= self.edge_threshold or position >= 1 - self.edge_threshold:
            return min(self.terminal_drag_threshold, self.edge_threshold)
        return self.drag_threshold

    @staticmethod
    def _content_frames_stable(previous, current):
        """Проверить, что область карточек магазина перестала заметно двигаться."""
        if not isinstance(previous, np.ndarray) or not isinstance(current, np.ndarray):
            return False
        if not previous.size or not current.size or previous.shape != current.shape:
            return False

        delta = cv2.absdiff(previous, current)
        if delta.ndim == 3:
            delta = np.max(delta, axis=2)
        mean_delta = float(np.mean(delta))
        changed_ratio = float(np.mean(delta >= EVENT_SHOP_SETTLE_PIXEL_DELTA))
        return (
            mean_delta <= EVENT_SHOP_SETTLE_MEAN_DELTA
            and changed_ratio <= EVENT_SHOP_SETTLE_CHANGED_RATIO
        )

    def wait_content_stable(self, main):
        """Адаптивно дождаться стабилизации карточек после движения scrollbar.

        OCR не должен стартовать по переходному кадру сразу после swipe. Вместо
        фиксированной задержки требуются несколько последовательных визуально
        стабильных пар кадров. Ожидание ограничено жёстким timeout.
        """
        previous = main.image_crop(EVENT_SHOP_SETTLE_AREA, copy=True)
        stable_pairs = 0
        deadline = monotonic() + EVENT_SHOP_SETTLE_TIMEOUT

        while monotonic() < deadline:
            sleep(EVENT_SHOP_SETTLE_SAMPLE_INTERVAL)
            main.device.screenshot()
            current = main.image_crop(EVENT_SHOP_SETTLE_AREA, copy=True)

            if self._content_frames_stable(previous, current):
                stable_pairs += 1
                if stable_pairs >= EVENT_SHOP_SETTLE_REQUIRED_PAIRS:
                    logger.debug(
                        '[Магазин события — сканер] Область карточек стабилизировалась после прокрутки'
                    )
                    return True
            else:
                stable_pairs = 0

            previous = current

        logger.warning(
            '[Магазин события — сканер] Область карточек не стабилизировалась до тайм-аута'
        )
        return False

    def set(self, position, main, random_range=(-0.05, 0.05), distance_check=True, skip_first_screenshot=True):
        with self._set_lock:
            default_drag_threshold = self.drag_threshold
            self.drag_threshold = self._drag_threshold_for_target(position)
            try:
                dragged = super().set(
                    position,
                    main=main,
                    random_range=random_range,
                    distance_check=distance_check,
                    skip_first_screenshot=skip_first_screenshot,
                )
                if dragged and not self.wait_content_stable(main):
                    raise GameStuckError(
                        '[Магазин события — сканер] Карточки не стабилизировались после прокрутки; OCR заблокирован'
                    )
                return dragged
            finally:
                self.drag_threshold = default_drag_threshold

    def set_precise(self, position, main, distance_check=True, skip_first_screenshot=True):
        """Вернуться к сохранённой позиции товара и доказать стабильность кадра.

        Полный scan может использовать грубый порог прокрутки, но повторная
        идентификация конкретного товара перед покупкой зависит от той же
        геометрии карточек, на которой был получен исходный OCR-снимок. Поэтому
        здесь запрещено обычное random_range, используется строгий порог, а
        destructive click разрешается только после visual-settle gate.
        """
        with self._set_lock:
            default_drag_threshold = self.drag_threshold
            self.drag_threshold = min(
                self.reidentify_drag_threshold,
                self._drag_threshold_for_target(position),
            )
            try:
                dragged = super().set(
                    position,
                    main=main,
                    random_range=(0.0, 0.0),
                    distance_check=distance_check,
                    skip_first_screenshot=skip_first_screenshot,
                )
                if not self.wait_content_stable(main):
                    raise GameStuckError(
                        '[Магазин события — покупка] Карточки не стабилизировались перед повторной идентификацией товара'
                    )
                return dragged
            finally:
                self.drag_threshold = default_drag_threshold

    def match_color(self, main):
        background_transparency = 0.2
        button_transparency = 0.5
        delta_x = 3
        area = (
            self.area[0] - delta_x,
            self.area[1],
            self.area[2] + delta_x,
            self.area[3]
        )
        image = main.image_crop(area, copy=False).astype(float)
        baseline_color = np.mean(image[:, [0, -1], :], axis=1)
        masked_color = image[:, image.shape[1] // 2, :]
        background_mask = background_transparency * np.array(self.color) + (1 - background_transparency) * baseline_color
        button_mask = button_transparency * np.array(self.color) + (1 - button_transparency) * baseline_color
        err_background = np.sum((masked_color - background_mask) ** 2, axis=1)
        err_button = np.sum((masked_color - button_mask) ** 2, axis=1)
        mask = err_button < err_background
        self.length = np.sum(mask)
        return mask


EVENT_SHOP_SCROLL = EventShopScroll(
    EVENT_SHOP_SCROLL_AREA,
    color=(44, 48, 56),
    name="EVENT_SHOP_SCROLL"
)
EVENT_SHOP_SCROLL.drag_threshold = 0.1
EVENT_SHOP_SCROLL.edge_threshold = 0.02


if server.server == 'tw':
    EVENT_SHOP_DEADLINE_COLOR = (102, 204, 255)
elif server.server == 'en':
    EVENT_SHOP_DEADLINE_COLOR = (255, 207, 129)
else:
    EVENT_SHOP_DEADLINE_COLOR = (96, 162, 62)
OCR_EVENT_SHOP_DEADLINE = Ocr(SHOP_EVENT_DEADLINE, lang='azur_lane', letter=EVENT_SHOP_DEADLINE_COLOR,
                              alphabet='0123456789.:~-', name="OCR_EVENT_SHOP_DEADLINE")

OCR_EVENT_SHOP_PT = Digit(SHOP_OCR_BALANCE, letter=(100, 100, 100), name='OCR_EVENT_SHOP_PT')
OCR_EVENT_SHOP_URPT = Digit(SHOP_OCR_BALANCE_SECOND, letter=(100, 100, 100), name='OCR_EVENT_SHOP_URPT')


class EventShopUI(UI):
    @cached_property
    def event_shop_tab_count_and_navbar(self):
        gap_x = 33
        area = (206, 92, 1092, 134)
        image = crop(self.device.image, area)
        tab = color_similarity_2d(image, color=(232, 238, 240))
        index = np.where(np.average(tab > 221, axis=0) > 0.5)[0]
        count = (area[2] - area[0] + gap_x) // (len(index) + gap_x)
        logger.info(f"Количество вкладок магазина события: {count}")
        delta_x = (area[2] - area[0] + gap_x) // count - gap_x
        grid = ButtonGrid((206, 92), (delta_x + gap_x, 44),
                          (delta_x, 44), (count, 1),
                          "EVENT_SHOP_TAB_GRID")
        navbar = Navbar(grids=grid,
                        active_color=(232, 238, 240), inactive_color=(127, 141, 151),
                        active_count=delta_x * (area[3] - area[1]) // 2,
                        inactive_count=delta_x * (area[3] - area[1]) // 2)
        return count, navbar

    @cached_property
    def event_shop_has_urpt(self):
        if self.image_color_count(SHOP_OCR_BALANCE_SECOND, OCR_EVENT_SHOP_URPT.letter, count=15):
            logger.info("[Магазин события — UI] Магазин содержит UR-очки")
            return True
        else:
            logger.info("[Магазин события — UI] В магазине нет UR-очков")
            return False

    @cached_property
    def is_event_ended(self):
        if self.config.EVENT_SHOP_IGNORE_DEADLINE:
            return True
        period = OCR_EVENT_SHOP_DEADLINE.ocr(self.device.image)[:-8]
        pattern = r'(\d{4})\.(\d{1,2})\.(\d{1,2})'
        matches = re.findall(pattern, period)
        if not matches or len(matches) < 2:
            logger.warning(f"[Магазин события — UI] Не удалось прочитать дату окончания события: {period}")
            return False
        y, m, d = matches[-1]
        deadline = datetime(int(y), int(m), int(d)) + timedelta(days=1)  # Серверный срок окончания.
        server_now = current_time() - server_time_offset()
        return (deadline - server_now).days < 7

    def event_shop_load_ensure(self):
        ensure_timeout = Timer(3, count=6).start()
        for _ in self.loop():
            if self.image_color_count(SHOP_OCR_BALANCE, OCR_EVENT_SHOP_PT.letter, count=15):
                logger.info("Магазин события загружен.")
                break
            if ensure_timeout.reached():
                raise GameStuckError('Слишком долгое ожидание появления магазина события.')
        return True

    def event_shop_get_pt(self):
        pt = OCR_EVENT_SHOP_PT.ocr(self.device.image)
        return pt

    def event_shop_get_urpt(self):
        urpt = OCR_EVENT_SHOP_URPT.ocr(self.device.image)
        return urpt

    def get_oil(self, skip_first_screenshot=True):
        """
        Вернуть:
            int: количество нефти.
        """
        amount = 0
        timeout = Timer(1, count=2).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('Тайм-аут получения количества нефти')
                break

            if not self.appear(SHOP_OCR_OIL_CHECK, offset=(10, 2)):
                logger.info('Значок нефти отсутствует')
                continue
            ocr = Digit(SHOP_OCR_OIL, name='OCR_OIL', letter=(247, 247, 247), threshold=128)
            amount = ocr.ocr(self.device.image)
            if amount >= 100:
                break

        return amount

    def handle_get_meowfficer(self):
        if self.appear(MEOWFFICER_GET_CHECK, offset=(40, 40), interval=3):
            logger.info('Получение награды Мяуфицера.')
            SWITCH_LOCK.set('lock', main=self)
            # Ждём исчезновения информационной панели.
            self.ensure_no_info_bar(timeout=1)
            self.device.click(MEOWFFICER_TRAIN_CLICK_SAFE_AREA)
            return True
        return False