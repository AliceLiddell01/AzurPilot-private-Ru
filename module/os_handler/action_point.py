"""Модуль управления очками действия Операции «Сирена».

Управляет очками действия (Action Point, AP) в режиме Операции «Сирена».
Включает OCR-распознавание значений AP, считывание показателей адаптивности,
разбор запасов коробок AP, а также логику взаимодействия для автоматической
покупки или использования коробок пополнения AP.
"""
# Этот файл обрабатывает очки действия (Action Point, AP) в режиме Операции «Сирена» (Operation Siren).
# Включает OCR очков действия, разбор запасов контейнеров AP и автоматическую покупку или использование припасов.
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from numbers import Integral

import module.config.server as server
from module.application.commission_recovery import (
    ACTION_POINT_GAIN_PER_PURCHASE,
    ACTION_POINTS_BUY,
)
from module.base.button import ButtonGrid
from module.base.timer import Timer
from module.base.utils import *
from module.config.time_source import now as current_time
from module.config.utils import get_server_next_update, server_time_offset
from module.log_res import LogRes
from module.logger import logger
from module.ocr.ocr import Digit, DigitCounter
from module.os_handler.assets import *
from module.os_handler.map_event import MapEventHandler
from module.statistics.item import Item, ItemGrid
from module.ui.assets import OS_CHECK
from module.ui.ui import UI

OCR_ACTION_POINT_REMAIN = Digit(ACTION_POINT_REMAIN, letter=(255, 219, 66), name='OCR_ACTION_POINT_REMAIN')
OCR_ACTION_POINT_REMAIN_OS = Digit(ACTION_POINT_REMAIN_OS, letter=(239, 239, 239),
                                   threshold=160, name='OCR_SHOP_YELLOW_COINS_OS')

OCR_OS_ADAPTABILITY = Digit([
    OS_ADAPTABILITY_ATTACK,
    OS_ADAPTABILITY_DURABILITY,
    OS_ADAPTABILITY_RECOVER
], letter=(231, 235, 239), lang="azur_lane", name='OCR_OS_ADAPTABILITY')


class ActionPointBuyCounter(DigitCounter):
    def after_process(self, result):
        result = super().after_process(result)

        # Возможные результаты: 0/5, 05
        if result == '05':
            result = '0/5'

        return result


if server.server != 'jp':
    # Шрифт символов в ACTION_POINT_BUY_REMAIN отличается от обычного цифрового шрифта Azur Lane
    OCR_ACTION_POINT_BUY_REMAIN = ActionPointBuyCounter(
        ACTION_POINT_BUY_REMAIN, letter=(148, 247, 99), lang='azur_lane', name='OCR_ACTION_POINT_BUY_REMAIN')
else:
    # На JP-сервере цифры ACTION_POINT_BUY_REMAIN белые, а на CN и EN — светло-зелёные
    OCR_ACTION_POINT_BUY_REMAIN = ActionPointBuyCounter(
        ACTION_POINT_BUY_REMAIN, letter=(255, 255, 255), lang='azur_lane', name='OCR_ACTION_POINT_BUY_REMAIN')


class ActionPointItem(Item):
    """Предмет очков действия Операции «Сирена»."""
    def predict_valid(self):
        return True


ACTION_POINT_GRID = ButtonGrid(
    origin=(323, 274), delta=(173, 0), button_shape=(115, 115), grid_shape=(4, 1), name='ACTION_POINT_GRID')

class GridSlice:
    """Срез сетки для построения сетки предметов."""
    def __init__(self, buttons):
        self.buttons = buttons

OIL_ITEM = ItemGrid(GridSlice([ACTION_POINT_GRID.buttons[0]]), templates={}, amount_area=(43, 91, 111, 113))
OIL_ITEM.item_class = ActionPointItem

ACTION_POINT_ITEMS = ItemGrid(GridSlice(ACTION_POINT_GRID.buttons[1:]), templates={}, amount_area=(75, 91, 111, 113))
ACTION_POINT_ITEMS.item_class = ActionPointItem
ACTION_POINTS_COST = {
    1: 5,
    2: 10,
    3: 15,
    4: 20,
    5: 30,
    6: 40,
}
ACTION_POINTS_COST_OBSCURE = {
    1: 10,  # В CL1 фактически нет скрытых зон
    2: 10,
    3: 20,
    4: 20,
    5: 40,
    6: 40,
}
ACTION_POINTS_COST_ABYSSAL = {
    1: 80,
    2: 80,
    3: 80,  # Ниже CL4 фактически нет бездонных зон
    4: 80,
    5: 100,
    6: 100,
}
ACTION_POINT_BUY_GAIN = ACTION_POINT_GAIN_PER_PURCHASE


class EmergencyActionPointPurchaseStatus(StrEnum):
    """Результат одной ограниченной попытки покупки AP для восстановления Commission."""

    PURCHASED = "purchased"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT_OIL = "insufficient_oil"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EmergencyActionPointPurchase:
    """Наблюдаемый результат покупки AP с явным числом кликов."""

    status: EmergencyActionPointPurchaseStatus
    remaining_before: int | None = None
    remaining_after: int | None = None
    oil_cost: int | None = None
    oil_before: int | None = None
    oil_after: int | None = None
    ap_before: int | None = None
    ap_after: int | None = None
    ap_gain: int | None = None
    click_count: int = 0
ACTION_POINT_BOX = {
    0: 0,
    1: 20,
    2: 50,
    3: 100,
}


class ActionPointLimit(Exception):
    """
    Исключение нехватки очков действия.

    Вызывается, когда очков действия недостаточно для входа в целевую зону.
    """
    def __init__(self, current=None, total=None, cost=None, preserve=None):
        super().__init__()
        self.current = current
        self.total = total
        self.cost = cost
        self.preserve = preserve

    @property
    def delay_minutes(self):
        """
        Получить количество минут для задержки.

        Returns:
            int | None: Количество минут задержки, либо None, если задержка не требуется.
        """
        if self.cost is None or self.current is None:
            return None

        missing = self.cost - self.current
        if missing <= 0:
            return None

        return missing * 10


class ActionPointHandler(UI, MapEventHandler):
    _action_point_box = [0, 0, 0, 0]
    _action_point_current = 0
    _action_point_total = 0

    @staticmethod
    def _is_in_month_end_purchase_block_week():
        """
        Определить, находится ли текущий момент на неделе блокировки покупок в конце месяца.

        В течение календарной недели (понедельник–воскресенье), содержащей первый день
        следующего месяца сервера, еженедельная покупка AP блокируется.
        После наступления следующего месяца сервера покупка снова становится доступной.

        Returns:
            bool: Находится ли в неделе блокировки конца месяца.
        """
        diff = server_time_offset()
        server_now = current_time() - diff
        next_month = (server_now.replace(day=28) + timedelta(days=4)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        next_month_start = next_month.replace(day=1)
        current_week_start = server_now.date() - timedelta(days=server_now.weekday())
        next_month_week_start = next_month_start.date() - timedelta(days=next_month_start.weekday())
        return current_week_start == next_month_week_start

    def _is_in_action_point(self):
        return self.appear(ACTION_POINT_USE, offset=(20, 20))

    def is_current_ap_visible(self):
        return self.match_template_color(CURRENT_AP_CHECK, offset=(40, 5), threshold=15)

    def action_point_use(self):
        prev = self._action_point_current
        self.interval_clear(ACTION_POINT_USE)
        for _ in self.loop():

            if self.appear_then_click(ACTION_POINT_USE, offset=(20, 20), interval=3):
                self.device.sleep(0.3)
                continue

            if self.handle_popup_confirm('ACTION_POINT_USE'):
                continue

            self.action_point_safe_get()
            if self._action_point_current > prev:
                break

    def action_point_update(self):
        """
        Обновить информацию об очках действия.

        Returns:
            int: Суммарные очки действия, включая коробки AP.
        """
        oil = OIL_ITEM.predict(self.device.image, name=False, amount=True)
        items = ACTION_POINT_ITEMS.predict(self.device.image, name=False, amount=True)
        box = [item.amount for item in oil] + [item.amount for item in items]
        current = OCR_ACTION_POINT_REMAIN.ocr(self.device.image)
        total = current
        if self.config.OS_ACTION_POINT_BOX_USE:
            total += int(np.sum(np.array(box) * tuple(ACTION_POINT_BOX.values())))
        oil = box[0]

        LogRes(self.config).Oil = oil
        logger.info(f'[Операция «Сирена» — очки действия] Очки действия: {current}({total}), топливо: {oil}')
        LogRes(self.config).ActionPoint = {'Value': current, 'Total': total}
        self.config.update()
        self._action_point_current = current
        self._action_point_box = box
        self._action_point_total = total
        # Обрабатываем превышение верхнего предела
        if total > 3000:
            self.config.override(OpsiGeneral_DoRandomMapEvent=False)
        return current

    def action_point_safe_get(self):
        """
        Безопасно получить информацию об очках действия.

        Ожидает полной загрузки всплывающего окна AP и обрабатывает возможные события карты.

        Returns:
            int | None: AP, распознанные на свежем кадре, либо ``None``.
        """
        timeout = Timer(3, count=6).start()
        for _ in self.loop():
            # Завершение
            if self.is_current_ap_visible():
                break
            if timeout.reached():
                logger.warning('[Операция «Сирена» — очки действия] Истекло время получения очков действия')
                break
            # Обрабатываем обязательные события карты поверх окна очков действия
            if self.handle_map_event():
                timeout.reset()
                continue

        skip_first_screenshot = True
        timeout = Timer(1, count=2).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('[Операция «Сирена» — очки действия] Истекло время получения очков действия')
                break
            # Обрабатываем обязательные события карты поверх окна очков действия
            if self.handle_map_event():
                timeout.reset()
                continue

            current = self.action_point_update()

            # Текущих очков действия слишком много — возможно, ошибка OCR
            if self._action_point_current > 600:
                continue

            oil, boxes = self._action_point_box[0], self._action_point_box[1:]
            # Есть контейнеры очков действия
            if sum(boxes) > 0:
                if oil > 100:
                    return current
                else:
                    # [11, 0, 1, 0]
                    continue
            # Либо есть нефть
            # Пока страница загружена не полностью, значение может быть 0 или 1
            # [1, 0, 0, 0]
            if oil > 100:
                return current

        return None

    @staticmethod
    def action_point_get_cost(zone, pinned):
        """
        Получить расход очков действия для входа в указанную зону.

        Args:
            zone (Zone): Зона для входа.
            pinned (str): Тип зоны. Допустимые типы: DANGEROUS, SAFE, OBSCURE, ABYSSAL, STRONGHOLD.

        Returns:
            int: Расход очков действия.
        """
        if pinned == 'DANGEROUS':
            cost = ACTION_POINTS_COST[zone.hazard_level] * 2
        elif pinned == 'SAFE':
            cost = ACTION_POINTS_COST[zone.hazard_level]
        elif pinned == 'OBSCURE':
            cost = ACTION_POINTS_COST_OBSCURE[zone.hazard_level]
        elif pinned == 'ABYSSAL':
            cost = ACTION_POINTS_COST_ABYSSAL[zone.hazard_level]
        elif pinned == 'STRONGHOLD':
            cost = 200
        else:
            logger.warning(f'[Операция «Сирена» — очки действия] Не удалось определить расход очков действия: zone={zone}, pinned={pinned}; предполагается расход 40')
            cost = 40

        if zone.is_port:
            cost = 0

        return cost

    def action_point_get_active_button(self):
        """
        Получить индекс текущей активной кнопки коробки AP.

        Returns:
            int: От 0 до 3. 0 — топливо, 1 — коробка на 20 AP, 2 — коробка на 50 AP, 3 — коробка на 100 AP.
        """
        for index, item in enumerate(ACTION_POINT_GRID.buttons):
            area = item.area
            color = get_color(self.device.image, area=(area[0], area[3] + 5, area[2], area[3] + 10))
            # Активная кнопка становится синей
            # Активная: 196, неактивная: 118 ~ 123
            if color[2] > 160:
                return index

        logger.warning('[Операция «Сирена» — очки действия] Не найдена активная кнопка контейнера с очками действия')
        return 1

    def action_point_set_button(self, index):
        """
        Выбрать кнопку коробки пополнения AP.

        Args:
            index (int): От 0 до 3. 0 — топливо, 1 — коробка на 20 AP, 2 — коробка на 50 AP, 3 — коробка на 100 AP.

        Returns:
            bool: Успешно ли переключено.
        """
        for _ in self.loop(timeout=2):
            if self.action_point_get_active_button() == index:
                return True
            else:
                self.device.click(ACTION_POINT_GRID[index, 0])
                self.device.sleep(0.3)
        else:
            logger.warning('[Операция «Сирена» — очки действия] Истекло время настройки кнопки очков действия')
            return False

    def action_point_get_buy_remain(self):
        """
        Получить оставшееся число покупок очков действия.

        Returns:
            int: Оставшееся количество покупок.

        Pages:
            in: ACTION_POINT_USE
        """
        current = self.action_point_get_buy_remain_optional(timeout=1)
        if current is None:
            logger.warning('[Операция «Сирена» — очки действия] Истекло время получения остатка доступных покупок очков действия')
            return 0
        return current

    def action_point_get_buy_remain_optional(self, timeout=1):
        """Распознать остаток покупок AP или вернуть ``None`` при неизвестности."""

        for _ in self.loop(timeout=timeout):
            current, _, total = OCR_ACTION_POINT_BUY_REMAIN.ocr(self.device.image)

            # Возможные результаты: 0/5, 05. Нулевой total означает, что OCR
            # ещё не увидел окно и не должен превращаться в ложный 0/5.
            if total == 0:
                continue
            if not isinstance(current, Integral) or not 0 <= current <= 5:
                continue
            return int(current)
        return None

    def action_point_buy_emergency_once(
        self,
        *,
        expected_remaining: int | None = None,
        wait_timeout: float = 5,
    ):
        """Выполнить не более одной покупки AP и доказать её постусловие.

        Метод намеренно не вызывает ``action_point_buy``: месячный блок,
        пользовательский лимит и резерв нефти относятся к обычной политике
        Operation Siren и не должны скрыто влиять на аварийное восстановление
        комиссии. После клика новый экран только распознаётся; повторного
        клика в этом методе нет.
        """

        if not self.action_point_set_button(0):
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNSAFE,
            )

        # Перед mutation требуется отдельная свежая граница кадра. AP и Oil
        # читаются с popup, а не из LogRes/config snapshot или арифметики.
        self.device.screenshot()
        ap_before = self.action_point_safe_get()
        if not isinstance(ap_before, Integral) or isinstance(ap_before, bool) or ap_before < 0:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNKNOWN,
                remaining_before=expected_remaining,
            )
        ap_before = int(ap_before)

        # Повторно подтверждаем weekly counter после свежего AP/Oil кадра.
        # Сохранённый остаток защищает от неожиданного изменения после записи в cache.
        observed_before = self.action_point_get_buy_remain_optional(timeout=1)
        if observed_before is None:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNKNOWN,
                remaining_before=expected_remaining,
                ap_before=ap_before,
            )
        if expected_remaining is not None and observed_before != expected_remaining:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNKNOWN,
                remaining_before=observed_before,
                ap_before=ap_before,
            )
        remaining = observed_before
        if remaining == 0:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNAVAILABLE,
                remaining_before=remaining,
                ap_before=ap_before,
            )
        cost = ACTION_POINTS_BUY.get(remaining)
        if cost is None:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNKNOWN,
                remaining_before=remaining,
                ap_before=ap_before,
            )

        oil = self._action_point_box[0]
        if not isinstance(oil, Integral) or isinstance(oil, bool) or oil < 0:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNKNOWN,
                remaining_before=remaining,
                oil_cost=cost,
                ap_before=ap_before,
            )
        oil = int(oil)
        if oil < cost:
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.INSUFFICIENT_OIL,
                remaining_before=remaining,
                oil_cost=cost,
                oil_before=oil,
                ap_before=ap_before,
            )
        if not self.appear(ACTION_POINT_USE, offset=(20, 20)):
            return EmergencyActionPointPurchase(
                status=EmergencyActionPointPurchaseStatus.UNSAFE,
                remaining_before=remaining,
                oil_cost=cost,
                oil_before=oil,
                ap_before=ap_before,
            )

        self.device.click(ACTION_POINT_USE)
        # Ровно один mutation-клик. Все следующие итерации только получают
        # свежие screenshots и OCR, чтобы исключить повторный цикл кликов.
        self.device.screenshot()
        remaining_after = None
        ap_after = None
        ap_gain = None
        oil_after = None
        for _ in self.loop(timeout=wait_timeout):
            after = self.action_point_get_buy_remain_optional(timeout=0.25)
            if after is None:
                continue
            remaining_after = after
            self.device.screenshot()
            observed_after = self.action_point_safe_get()
            if (
                not isinstance(observed_after, Integral)
                or isinstance(observed_after, bool)
                or observed_after < 0
            ):
                continue
            ap_after = int(observed_after)
            after_oil = self._action_point_box[0]
            if not isinstance(after_oil, Integral) or isinstance(after_oil, bool) or after_oil < 0:
                continue
            oil_after = int(after_oil)
            ap_gain = ap_after - ap_before
            if (
                after == remaining - 1
                and ap_gain == ACTION_POINT_BUY_GAIN
                and oil_after == oil - cost
            ):
                return EmergencyActionPointPurchase(
                    status=EmergencyActionPointPurchaseStatus.PURCHASED,
                    remaining_before=remaining,
                    remaining_after=after,
                    oil_cost=cost,
                    oil_before=oil,
                    oil_after=oil_after,
                    ap_before=ap_before,
                    ap_after=ap_after,
                    ap_gain=ap_gain,
                    click_count=1,
                )
            if (
                after == remaining
                and ap_gain == 0
                and oil_after == oil
            ):
                continue
            if ap_gain > ACTION_POINT_BUY_GAIN or oil_after > oil:
                return EmergencyActionPointPurchase(
                    status=EmergencyActionPointPurchaseStatus.FAILED,
                    remaining_before=remaining,
                    remaining_after=after,
                    oil_cost=cost,
                    oil_before=oil,
                    oil_after=oil_after,
                    ap_before=ap_before,
                    ap_after=ap_after,
                    ap_gain=ap_gain,
                    click_count=1,
                )

        return EmergencyActionPointPurchase(
            status=EmergencyActionPointPurchaseStatus.UNKNOWN,
            remaining_before=remaining,
            remaining_after=remaining_after,
            oil_cost=cost,
            oil_before=oil,
            oil_after=oil_after,
            ap_before=ap_before,
            ap_after=ap_after,
            ap_gain=ap_gain,
            click_count=1,
        )

    def action_point_buy(self, preserve=1000):
        """
        Купить очки действия за топливо (нефть).

        Args:
            preserve (int): Резервируемое количество топлива.

        Returns:
            bool: Успешна ли покупка.

        Pages:
            in: ACTION_POINT_USE
        """
        self.action_point_set_button(0)
        current = self.action_point_get_buy_remain()
        buy_max = 5  # В текущей версии игрок может покупать очки действия 5 раз в неделю
        buy_count = buy_max - current
        buy_limit = self.config.OpsiGeneral_BuyActionPointLimit
        if self._is_in_month_end_purchase_block_week():
            logger.info('[Операция «Сирена» — очки действия] Покупка очков действия на этой неделе пропущена: это последняя неделя месяца')
            return False
        if buy_count >= buy_limit:
            logger.info('[Операция «Сирена» — очки действия] Достигнут недельный предел покупки очков действия')
            return False
        cost = ACTION_POINTS_BUY[current]
        oil = self._action_point_box[0]
        logger.info(f'[Операция «Сирена» — очки действия] Покупка очков действия потребует {cost} нефти; текущая нефть: {oil}, резерв: {preserve}')
        if oil >= cost + preserve:
            self.action_point_use()
            return True
        else:
            logger.info('[Операция «Сирена» — очки действия] Недостаточно нефти для покупки')
            return False

    def action_point_quit(self, timeout=None):
        """
        Выйти из всплывающего окна очков действия.

        Pages:
            in: ACTION_POINT_USE
            out: page_os
        """
        for _ in self.loop(timeout=timeout):
            # Завершение
            # Иногда у окна очков действия нет чёрного размытого фона
            # ACTION_POINT_CANCEL и OS_CHECK появляются одновременно
            if not self.appear(ACTION_POINT_CANCEL, offset=(20, 20)):
                if self.appear(OS_CHECK, offset=(20, 20)):
                    return True
            # Нажатие
            if self.appear_then_click(ACTION_POINT_CANCEL, offset=(20, 20), interval=3):
                continue
            # Обрабатываем обязательные события карты поверх окна очков действия
            if self.handle_map_event():
                continue
        logger.warning('[Операция «Сирена» — очки действия] Истекло время закрытия окна очков действия')
        return False

    def handle_action_point(self, zone, pinned, cost=None, keep_current_ap=True, check_rest_ap=False):
        """
        Обработать очки действия, включая покупку и использование коробок AP.

        Args:
            zone (Zone): Зона для входа.
            pinned (str): Тип зоны. Допустимые типы: DANGEROUS, SAFE, OBSCURE, ABYSSAL, STRONGHOLD.
            cost (int): Пользовательское значение расхода AP.
            keep_current_ap (bool): Проверять ли AP заранее, чтобы избежать траты остатка при нехватке.
            check_rest_ap (bool): Если сумма текущего AP и доступного сегодня превышает 200, пропустить проверку keep_current_ap.

        Returns:
            bool: Успешно ли обработано.

        Raises:
            ActionPointLimit: Вызывается при нехватке очков действия.

        Pages:
            in: ACTION_POINT_USE
        """
        if not self._is_in_action_point():
            return False

        # У контейнеров очков действия есть анимация появления
        self.action_point_safe_get()
        if cost is None:
            cost = self.action_point_get_cost(zone, pinned)
        buy_checked = False

        # Проверяем оставшиеся очки действия
        if check_rest_ap:
            diff = get_server_next_update('00:00') - current_time()
            today_rest = int(diff.total_seconds() // 600)
            if self._action_point_current + today_rest >= 200:
                logger.info('[Операция «Сирена» — очки действия] Сумма текущих и доступных сегодня очков действия превышает 200, проверка пропущена')
                logger.info(f'[Операция «Сирена» — очки действия] Текущие={self._action_point_current}, доступно сегодня={today_rest}')
                keep_current_ap = False

        # Сначала проверяем очки действия
        if keep_current_ap:
            if self._action_point_total <= self.config.OS_ACTION_POINT_PRESERVE:
                logger.info(f'[Операция «Сирена» — очки действия] Достигнут предел очков действия, резерв={self.config.OS_ACTION_POINT_PRESERVE}')
                self.action_point_quit()
                raise ActionPointLimit(
                    current=self._action_point_current,
                    total=self._action_point_total,
                    preserve=self.config.OS_ACTION_POINT_PRESERVE,
                )

        for _ in range(12):
            # Очков действия достаточно
            if self._action_point_current >= cost:
                logger.info('[Операция «Сирена» — очки действия] Очков действия достаточно')
                self.action_point_quit()
                return True

            # Покупаем очки действия
            if self.config.OpsiGeneral_BuyActionPointLimit > 0 and not buy_checked:
                if self.action_point_buy(preserve=self.config.OpsiGeneral_OilLimit):
                    self.action_point_safe_get()
                    continue
                else:
                    buy_checked = True

            # Повторно проверяем, меньше ли общий запас очков действия требуемого расхода
            # Если да, использование контейнеров пропускаем
            if self._action_point_total < cost:
                logger.info('[Операция «Сирена» — очки действия] Недостаточно очков действия')
                self.action_point_quit()
                raise ActionPointLimit(
                    current=self._action_point_current,
                    total=self._action_point_total,
                    cost=cost,
                )

            # Сортируем контейнеры очков действия
            box = []
            for index in [3, 2, 1]:
                if self._action_point_box[index] > 0:
                    if self._action_point_current + ACTION_POINT_BOX[index] >= 200:
                        box.append(index)
                    else:
                        box.insert(0, index)

            # Используем контейнер очков действия
            if len(box):
                if self._action_point_total > self.config.OS_ACTION_POINT_PRESERVE:
                    self.action_point_set_button(box[0])
                    self.action_point_use()
                    continue
                else:
                    logger.info(f'[Операция «Сирена» — очки действия] Достигнут предел очков действия, резерв={self.config.OS_ACTION_POINT_PRESERVE}')
                    self.action_point_quit()
                    raise ActionPointLimit(
                        current=self._action_point_current,
                        total=self._action_point_total,
                        preserve=self.config.OS_ACTION_POINT_PRESERVE,
                    )
            else:
                logger.info('[Операция «Сирена» — очки действия] Контейнеров с очками действия больше нет')
                self.action_point_quit()
                raise ActionPointLimit(
                    current=self._action_point_current,
                    total=self._action_point_total,
                    cost=cost,
                )

        logger.warning('[Операция «Сирена» — очки действия] Не удалось получить очки действия за 12 попыток')
        return False

    def action_point_enter(self, timeout=None):
        """
        Войти во всплывающее окно очков действия.

        Pages:
            in: OS_CHECK
            out: ACTION_POINT_USE
        """
        for _ in self.loop(timeout=timeout):
            if self.appear(ACTION_POINT_USE, offset=(20, 20)):
                return True

            if self.appear(OS_CHECK, offset=(20, 20), interval=3):
                self.device.click(ACTION_POINT_REMAIN_OS)
                continue
            if self.handle_map_event():
                # Сюжет прозрачен, поэтому при его обработке может определяться OS_CHECK
                self.interval_reset(OS_CHECK)
                continue
            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50)):
                continue
        logger.warning('[Операция «Сирена» — очки действия] Истекло время открытия окна очков действия')
        return False

    def action_point_set(self, zone=None, pinned=None, cost=None, keep_current_ap=True, check_rest_ap=False):
        """
        Настроить очки действия, открыть всплывающее окно AP и обработать.

        Args:
            zone (Zone): Зона для входа.
            pinned (str): Тип зоны. Допустимые типы: DANGEROUS, SAFE, OBSCURE, ABYSSAL, STRONGHOLD.
            cost (int): Пользовательское значение расхода AP.
            keep_current_ap (bool): Проверять ли AP заранее, чтобы избежать траты остатка при нехватке.
            check_rest_ap (bool): Если сумма текущего AP и доступного сегодня превышает 200, пропустить проверку keep_current_ap.

        Returns:
            bool: Успешно ли обработано.

        Raises:
            ActionPointLimit: Вызывается при нехватке очков действия.
        """
        self.action_point_enter()
        if not self.handle_action_point(zone, pinned, cost, keep_current_ap, check_rest_ap):
            return False

        # Ждём закрытия окна очков действия
        for _ in self.loop():
            if self.appear(IN_MAP, offset=(200, 5)):
                break

        return True

    def action_point_check(self, amount):
        """
        Проверить, достаточно ли очков действия.

        Args:
            amount (int): Проверяемое количество очков действия.

        Returns:
            bool: Достаточно ли очков действия.
        """
        self.action_point_enter()
        self.action_point_safe_get()

        enough = self._action_point_total > amount
        if enough:
            logger.info(f'[Операция «Сирена» — очки действия] Доступно {amount} очков действия')
        else:
            logger.info(f'[Операция «Сирена» — очки действия] Нет {amount} очков действия')

        self.action_point_quit()
        for _ in self.loop():
            if self.appear(IN_MAP, offset=(200, 5)):
                break

        return enough
