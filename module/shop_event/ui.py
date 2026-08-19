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
EVENT_SHOP_SETTLE_REQUIRED_PAIRS = 2
EVENT_SHOP_SETTLE_MAX_SHIFT = 1.5
EVENT_SHOP_SETTLE_MIN_PHASE_RESPONSE = 0.10
EVENT_SHOP_SETTLE_MAX_SCROLL_DELTA = 0.01


class EventShopScroll(Scroll):
    terminal_drag_threshold = 0.02
    reidentify_drag_threshold = 0.02
    _set_lock = RLock()

    def _drag_threshold_for_target(self, position):
        if position <= self.edge_threshold or position >= 1 - self.edge_threshold:
            return min(self.terminal_drag_threshold, self.edge_threshold)
        return self.drag_threshold

    @staticmethod
    def _content_shift(previous, current):
        """Оценить глобальный межкадровый сдвиг сетки карточек.

        В EventShop постоянно меняются локальные эффекты и фон. Пиксельная
        идентичность поэтому не доказывает и не опровергает остановку прокрутки.
        Phase correlation оценивает именно общий геометрический сдвиг структуры,
        который нужен перед OCR и destructive re-identification.
        """
        if not isinstance(previous, np.ndarray) or not isinstance(current, np.ndarray):
            return None
        if not previous.size or not current.size or previous.shape != current.shape:
            return None

        if previous.ndim == 3:
            previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
            current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
        elif previous.ndim == 2:
            previous_gray = previous
            current_gray = current
        else:
            return None

        previous_float = np.ascontiguousarray(previous_gray, dtype=np.float32)
        current_float = np.ascontiguousarray(current_gray, dtype=np.float32)
        height, width = previous_float.shape
        window = cv2.createHanningWindow((width, height), cv2.CV_32F)
        shift, response = cv2.phaseCorrelate(previous_float, current_float, window)
        dx, dy = float(shift[0]), float(shift[1])
        response = float(response)
        if not np.isfinite((dx, dy, response)).all():
            return None
        return dx, dy, response

    @staticmethod
    def _content_shift_stable(result):
        if result is None:
            return False
        dx, dy, response = result
        return (
            response >= EVENT_SHOP_SETTLE_MIN_PHASE_RESPONSE
            and abs(dx) <= EVENT_SHOP_SETTLE_MAX_SHIFT
            and abs(dy) <= EVENT_SHOP_SETTLE_MAX_SHIFT
        )

    @classmethod
    def _content_frames_stable(cls, previous, current):
        """Проверить отсутствие глобального движения сетки карточек."""
        return cls._content_shift_stable(cls._content_shift(previous, current))

    def wait_content_stable(self, main):
        """Дождаться геометрической стабилизации карточек после scrollbar move.

        OCR допускается после двух последовательных кадров, на которых одновременно
        стабильны и сама сетка карточек, и измеренная позиция scrollbar. Локальная
        анимация, блики и фон не должны превращать нормальный неподвижный магазин в
        ложный timeout. Ожидание остаётся bounded.
        """
        previous = main.image_crop(EVENT_SHOP_SETTLE_AREA, copy=True)
        previous_scroll = self.cal_position(main)
        stable_pairs = 0
        deadline = monotonic() + EVENT_SHOP_SETTLE_TIMEOUT

        while monotonic() < deadline:
            sleep(EVENT_SHOP_SETTLE_SAMPLE_INTERVAL)
            main.device.screenshot()
            current = main.image_crop(EVENT_SHOP_SETTLE_AREA, copy=True)
            current_scroll = self.cal_position(main)
            shift = self._content_shift(previous, current)
            content_stable = self._content_shift_stable(shift)
            scroll_stable = (
                abs(current_scroll - previous_scroll)
                <= EVENT_SHOP_SETTLE_MAX_SCROLL_DELTA
            )

            if content_stable and scroll_stable:
                stable_pairs += 1
                if stable_pairs >= EVENT_SHOP_SETTLE_REQUIRED_PAIRS:
                    if shift is not None:
                        dx, dy, response = shift
                        logger.debug(
                            '[Магазин события — сканер] Сетка карточек стабилизировалась: '
                            f'shift=({dx:.2f}, {dy:.2f}), response={response:.3f}, '
                            f'scroll={current_scroll:.3f}'
                        )
                    return True
            else:
                stable_pairs = 0

            previous = current
            previous_scroll = current_scroll

        logger.warning(
            '[Магазин события — сканер] Геометрия карточек или scrollbar не стабилизировались до тайм-аута'
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
        remove_click_history = getattr(
            getattr(main, 'device', None),
            'click_record_remove',
            None,
        )
        if callable(remove_click_history):
            removed = remove_click_history(self.name)
            if removed:
                logger.debug(
                    '[Магазин события — покупка] Перед точной повторной идентификацией '
                    f'удалено записей штатной прокрутки: {removed}'
                )

        with self._set_lock:
            default_drag_threshold = self.drag_threshold
            default_edge_add = self.edge_add
            self.drag_threshold = min(
                self.reidentify_drag_threshold,
                self._drag_threshold_for_target(position),
            )
            # Базовый Scroll.set подменяет random_range у краёв на edge_add.
            # Для повторной идентификации исключаем этот намеренный overshoot.
            self.edge_add = (0.0, 0.0)
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
                self.edge_add = default_edge_add

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
