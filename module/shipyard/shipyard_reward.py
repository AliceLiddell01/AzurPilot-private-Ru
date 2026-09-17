"""
Модуль покупки чертежей на верфи.

Автоматизирует процесс покупки и использования исследовательских чертежей на верфи (Shipyard).
Поддерживает управление чертежами обеих редкостей: PR (приоритетные) и DR (решающие).

Основные возможности:
    - Считывание баланса монет с главной страницы для расчёта покупок
    - Расчёт доступного для покупки количества чертежей по шкале цен и остатку монет
    - Автоматический переход на верфь, выбор целевого корабля и покупка чертежей
    - Использование накопившихся чертежей целевого корабля
    - Защита от повторного выполнения в течение дня (сброс в 04:00 по серверу)

Шкала цен:
    - Чертежи PR: первые 2 бесплатно, затем ступенчатое повышение до 1500 монет/шт.
    - Чертежи DR: первые 2 бесплатно, затем ступенчатое повышение до 6000 монет/шт.

Зависимости:
    Наследует ShipyardUI (операции интерфейса верфи), вызывает навигацию верфи, подтверждение покупки и др.

Pages:
    Вход: Любая страница
    Выход: page_shipyard
"""

from module.base.timer import Timer
from module.config.time_source import now as current_time
from module.config.utils import get_server_last_update
from module.exception import ScriptError
from module.logger import logger
from module.shipyard.ui import ShipyardUI
from module.ui.page import page_main, page_shipyard

# Шкала цен чертежей PR: ключ — диапазон порядковых номеров купленных чертежей, значение — цена за единицу (монеты)
PRBP_BUY_PRIZE = {
    (1, 2):               0,
    (3, 4):               150,
    (5, 6, 7):            300,
    (8, 9, 10):           600,
    (11, 12, 13, 14, 15): 1050,
}
# Шкала цен чертежей DR: ключ — диапазон порядковых номеров купленных чертежей, значение — цена за единицу (монеты)
DRBP_BUY_PRIZE = {
    (1, 2):               0,
    (3, 4, 5, 6):         600,
    (7, 8, 9, 10):        1200,
    (11, 12, 13, 14, 15): 3000,
}


class RewardShipyard(ShipyardUI):
    """
    Обработчик задачи покупки чертежей на верфи.

    Отвечает за покупку и использование чертежей на верфи. Поддерживает оба типа редкости:
    PR (обычные приоритетные) и DR (решающие), рассчитывая оптимальный объём покупки по ступенчатой шкале.

    Атрибуты:
        _shipyard_bp_rarity (str): Текущая редкость чертежей, 'PR' или 'DR'
        _coin_count (int): Баланс монет, полученный через OCR с главной страницы

    Настройки:
        Shipyard_ShipIndex: Индекс корабля PR
        Shipyard_BuyAmount: Количество покупки PR
        Shipyard_ResearchSeries: Серия исследований PR
        ShipyardDr_ShipIndex: Индекс корабля DR
        ShipyardDr_BuyAmount: Количество покупки DR
        ShipyardDr_ResearchSeries: Серия исследований DR

    Pages:
        Вход: Любая страница
        Выход: page_shipyard
    """
    _shipyard_bp_rarity = 'PR'
    _coin_count = 0

    @staticmethod
    def _shipyard_task_enabled(index, count):
        return index > 0 and count > 0

    def _shipyard_get_cost(self, amount, rarity=None):
        """
        Расчёт стоимости покупки одного чертежа на основе его порядкового номера и редкости.

        Args:
            amount (int): Порядковый номер покупаемого чертежа
            rarity (str): Редкость чертежа, 'DR' или 'PR'

        Returns:
            int: Цена покупки в монетах

        Raises:
            ScriptError: При неверно указанной редкости
        """
        if rarity is None:
            rarity = self._shipyard_bp_rarity

        if rarity == 'PR':
            cost = [v for k, v in PRBP_BUY_PRIZE.items() if amount in k]
            if len(cost):
                return cost[0]
            else:
                return 1500
        elif rarity == 'DR':
            cost = [v for k, v in DRBP_BUY_PRIZE.items() if amount in k]
            if len(cost):
                return cost[0]
            else:
                return 6000
        else:
            raise ScriptError(f'Недопустимая редкость в _shipyard_get_cost: {rarity}')

    def _shipyard_calculate(self, start, count, pay=False):
        """
        Расчёт максимального числа чертежей для покупки при текущем балансе монет.

        На основе начального номера, оставшегося количества и баланса монет вычисляет,
        сколько чертежей можно купить. При pay=True вычитает потраченные монеты из баланса.

        Args:
            start (int): Начальный порядковый номер чертежа
            count (int): Оставшееся требуемое количество чертежей
            pay (bool): Списывать ли монеты из локального баланса

        Returns:
            tuple: (следующий начальный номер, число чертежей для покупки)
        """
        if start <= 0 or count <= 0:
            return start, count

        total = 0
        i = start
        for i in range(start, (start + count)):
            cost = self._shipyard_get_cost(i)

            if (total + cost) > self._coin_count:
                if pay:
                    self._coin_count -= total
                else:
                    logger.info(f'Можно купить не более {(i - start)} '
                                f'/ {count} чертежей')
                return i, i - start
            total += cost

        if pay:
            self._coin_count -= total
        else:
            logger.info(f'Можно купить все {count} чертежей')
        return i + 1, count

    def _shipyard_buy_calc(self, start, count):
        """Расчёт доступного количества чертежей без списания монет."""
        return self._shipyard_calculate(start, count, pay=False)

    def _shipyard_pay_calc(self, start, count):
        """Расчёт и списание стоимости купленных чертежей из баланса монет."""
        return self._shipyard_calculate(start, count, pay=True)

    def _shipyard_buy(self, count):
        """
        Покупка заданного количества чертежей.

        Поддерживает покупку на стадиях DEV и FATE. Циклически открывает интерфейс покупки,
        выставляет количество и подтверждает приобретение, пока чертежи не закончатся или усиление невозможно.

        Args:
            count (int): Общее количество чертежей для покупки
        """
        logger.hr('Верфь — покупка')
        prev = 1
        start, count = self._shipyard_buy_calc(prev, count)
        while count > 0:
            if not self._shipyard_buy_enter() or \
                    self._shipyard_cannot_strengthen():
                break

            remain = self._shipyard_ensure_index(count)
            if remain is None:
                break

            if self._shipyard_bp_rarity == 'DR':
                self.config.ShipyardDr_LastRun = current_time().replace(microsecond=0)
            else:
                self.config.Shipyard_LastRun = current_time().replace(microsecond=0)

            self._shipyard_buy_confirm('BP_BUY')

            # Вычитаем монеты по фактически купленному количеству (remain) и одновременно обновляем start
            # Сохраняем в prev для следующего вызова _shipyard_pay_calc
            start, _ = self._shipyard_pay_calc(prev, (count - remain))
            prev = start

            start, count = self._shipyard_buy_calc(start, remain)

    def _shipyard_use(self, index):
        """
        Использование всех имеющихся лишних чертежей целевого корабля.

        Поддерживает использование чертежей на стадиях DEV и FATE.

        Args:
            index (int): Индекс целевого корабля
        """
        logger.hr('Верфь — использование')
        count = self._shipyard_get_bp_count(index)
        while count > 0:
            if not self._shipyard_buy_enter() or \
                    self._shipyard_cannot_strengthen():
                break

            remain = self._shipyard_ensure_index(count)
            if remain is None:
                break
            self._shipyard_buy_confirm('BP_USE')

            count = self._shipyard_get_bp_count(index)

    def shipyard_run(self, series, index, count):
        """
        Выполнение процесса покупки чертежей на верфи.

        Pages: in: page_main, out: page_shipyard

        Args:
            series (int): Серия исследований, 1-4 (для некоторых серий 1-5)
            index (int): Индекс корабля, 1-6
            count (int): Количество чертежей для покупки после использования имеющихся

        Returns:
            bool: Был ли выполнен процесс покупки
        """
        if count <= 0:
            logger.info('Количество чертежей для покупки равно 0; пропуск')
            return False
        if index <= 0:
            logger.info('Индекс корабля на верфи равен 0; пропуск')
            return False

        # OCR монет на странице верфи ненадёжен из-за выравнивания текста и числа по правому краю
        # Поэтому получаем данные о монетах с главной страницы
        self.ui_ensure(page_main)
        timeout = Timer(1, count=1).start()
        skip_first_screenshot = True
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            self._coin_count = self._shipyard_get_coin()

            if self._coin_count > 0:
                break
            if timeout.reached():
                logger.warning('Считаем OCR_COIN находящимся в правильной позиции')
                break

        self.ui_goto(page_shipyard)
        if not self.shipyard_set_focus(series=series, index=index) \
                or not self._shipyard_buy_enter() \
                or self._shipyard_cannot_strengthen():
            return True

        self._shipyard_use(index=index)
        self._shipyard_buy(count=count)

        return True

    def run(self):
        """
        Pages:
            in: Any page
            out: page_shipyard
        """
        dr_enabled = self._shipyard_task_enabled(
            self.config.ShipyardDr_ShipIndex,
            self.config.ShipyardDr_BuyAmount,
        )
        pr_enabled = self._shipyard_task_enabled(
            self.config.Shipyard_ShipIndex,
            self.config.Shipyard_BuyAmount,
        )
        if not dr_enabled and not pr_enabled:
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        logger.hr('Верфь — DR', level=1)
        logger.attr('Последний запуск верфи DR', self.config.ShipyardDr_LastRun)
        if not dr_enabled:
            logger.info('Задание верфи DR не настроено; пропуск')
        elif self.config.ShipyardDr_LastRun > get_server_last_update('04:00'):
            logger.warning('Задание верфи DR уже выполнялось сегодня; пропуск')
        else:
            self._shipyard_bp_rarity = 'DR'
            self.shipyard_run(series=self.config.ShipyardDr_ResearchSeries,
                              index=self.config.ShipyardDr_ShipIndex,
                              count=self.config.ShipyardDr_BuyAmount)

        logger.hr('Верфь — PR', level=1)
        logger.attr('Последний запуск верфи PR', self.config.Shipyard_LastRun)
        if not pr_enabled:
            logger.info('Задание верфи PR не настроено; пропуск')
        elif self.config.Shipyard_LastRun > get_server_last_update('04:00'):
            logger.warning('Задание верфи PR уже выполнялось сегодня; остановка')
            self.config.task_delay(server_update=True)
            self.config.task_stop()
        else:
            self._shipyard_bp_rarity = 'PR'
            self.shipyard_run(series=self.config.Shipyard_ResearchSeries,
                              index=self.config.Shipyard_ShipIndex,
                              count=self.config.Shipyard_BuyAmount)

        self.config.task_delay(server_update=True)
