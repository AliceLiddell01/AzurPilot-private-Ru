"""Модуль системы постройки (Gacha), обрабатывающий полный цикл строительства кораблей.
Включает навигацию по страницам постройки, предварительный расчёт расхода ресурсов,
отправку заказов, автоматический сбор результатов и управление очередью постройки."""

# Этот файл обрабатывает операции строительства (Gacha/Build).
# Включает навигацию по страницам строительства, предварительный расчёт ресурсов, отправку заказов, автоматический сбор результатов и очистку очереди.
from module.base.timer import Timer
from module.campaign.campaign_status import CampaignStatus
from module.combat.assets import GET_SHIP
from module.exception import ScriptError
from module.gacha.assets import *
from module.gacha.ui import GachaUI
from module.handler.assets import POPUP_CONFIRM, STORY_SKIP
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.retirement import Retirement
from module.log_res import LogRes

RECORD_GACHA_OPTION = ('RewardRecord', 'gacha')
RECORD_GACHA_SINCE = (0,)
OCR_BUILD_CUBE_COUNT = Digit(BUILD_CUBE_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_TICKET_COUNT = Digit(BUILD_TICKET_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_SUBMIT_COUNT = Digit(BUILD_SUBMIT_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_SUBMIT_WW_COUNT = Digit(BUILD_SUBMIT_WW_COUNT, letter=(255, 247, 247), threshold=64)


class RewardGacha(GachaUI, Retirement, CampaignStatus):
    build_coin_count = 0
    build_cube_count = 0
    build_ticket_count = 0

    def gacha_prep(self, target, skip_first_screenshot=True):
        """
        Подготовка к отправке заказов на постройку.

        Args:
            target (int): Количество отправляемых заказов на постройку.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            bool: True при успешной подготовке, иначе False.

        Pages:
            in: page_build (любая подстраница)
            out: Всплывающее окно подтверждения отправки

        Raises:
            ScriptError: Вызывается, если не удалось распознать OCR-ресурс.
        """
        # При target = 0 подготовка не требуется
        if not target:
            return False

        # Подготовка возможна только на нужной странице
        if not self.appear(BUILD_SUBMIT_ORDERS) \
                and not self.appear(BUILD_SUBMIT_WW_ORDERS):
            return False

        # Через 'appear' обновляем фактическое положение ресурса для ui_ensure_index
        confirm_timer = Timer(1, count=2).start()
        ocr_submit = None
        index_offset = (60, 20)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(BUILD_SUBMIT_ORDERS, interval=3):
                ocr_submit = OCR_BUILD_SUBMIT_COUNT
                confirm_timer.reset()
                continue

            if self.appear_then_click(BUILD_SUBMIT_WW_ORDERS, interval=3):
                ocr_submit = OCR_BUILD_SUBMIT_WW_COUNT
                confirm_timer.reset()
                continue
            # Продолжаем строительство, даже если очки обмена UR уже заполнены
            if self.handle_popup_confirm('GACHA_PREP'):
                confirm_timer.reset()
                continue

            # Завершение
            if self.appear(BUILD_PLUS, offset=index_offset) \
                    and self.appear(BUILD_MINUS, offset=index_offset):
                if confirm_timer.reached():
                    break

        # Проверяем аномальный ранний выход и задаём правильное количество заказов
        if ocr_submit is None:
            raise ScriptError('[Строительство — подготовка] Не удалось распознать OCR-ресурс; '
                              'подготовка не может быть продолжена')
        area = ocr_submit.buttons[0]
        ocr_submit.buttons = [(BUILD_MINUS.button[2] + 3, area[1], BUILD_PLUS.button[0] - 3, area[3])]
        self.ui_ensure_index(target, letter=ocr_submit, prev_button=BUILD_MINUS,
                             next_button=BUILD_PLUS, skip_first_screenshot=True)

        return True

    def gacha_calculate(self, target_count, gold_cost, cube_cost):
        """
        Расчёт фактического доступного количества построек на основе текущих ресурсов.

        Args:
            target_count (int): Желаемое количество заказов на постройку.
            gold_cost (int): Расход монет.
            cube_cost (int): Расход Кубов мудрости.

        Returns:
            int: Количество, которое фактически можно заказать с учётом ресурсов.
        """
        while 1:
            # Рассчитываем расход ресурсов по target_count
            gold_total = gold_cost * target_count
            cube_total = cube_cost * target_count

            # При нулевом количестве строительство выполнить нельзя
            if not target_count:
                logger.warning('Недостаточно монет и/или Кубов мудрости для строительства')
                break

            # При нехватке ресурсов уменьшаем количество на 1 и пересчитываем
            if gold_total > self.build_coin_count or cube_total > self.build_cube_count:
                target_count -= 1
                continue

            break

        # Вычитаем ресурсы и возвращаем текущий target_count
        logger.info(f'Можно отправить не более {target_count} заказов на строительство')
        self.build_coin_count -= gold_total
        self.build_cube_count -= cube_total
        LogRes(self.config).Cube = self.build_cube_count
        self.config.update()
        return target_count

    def gacha_goto_pool(self, target_pool):
        """
        Переход на страницу указанного пула постройки.

        Args:
            target_pool (str): Название пула постройки; при выходе за пределы по умолчанию используется пул 'light'.

        Returns:
            str: Название текущего доступного пула постройки.

        Pages:
            in: page_build (выбор пула постройки)
            out: page_build (страница операций пула постройки)

        Raises:
            ScriptError: Вызывается, если выбран 'wishing_well', но настройка не завершена.
        """
        # Переключаемся на пул 'light'
        self.gacha_bottom_navbar_ensure(right=3, is_build=True)

        # При необходимости переходим к target_pool и обновляем его фактическое значение
        if target_pool == 'wishing_well':
            if self._gacha_side_navbar.get_total(main=self) != 5:
                logger.warning('\'wishing_well\' недоступен; '
                               'используется пул \'light\'')
                target_pool = 'light'
            else:
                self.gacha_side_navbar_ensure(upper=2)
                if self.appear(BUILD_WW_CHECK):
                    raise ScriptError('\'wishing_well\' должен быть настроен '
                                      'пользователем вручную; продолжить '
                                      'gacha_goto_pool невозможно')
        elif target_pool == 'event':
            gacha_bottom_navbar = self._gacha_bottom_navbar(is_build=True)
            total = gacha_bottom_navbar.get_total(main=self)
            if total == 3:
                logger.warning('\'event\' недоступен; используется '
                               'пул \'light\'')
                target_pool = 'light'
            else:
                # Доступный пул события находится на самой левой вкладке.
                # Индекс left у Navbar начинается с 1, а get_info() возвращает абсолютный индекс с 0,
                # поэтому значение get_info() нельзя передавать как `left`.
                self.gacha_bottom_navbar_ensure(left=1, is_build=True)
        elif target_pool in ['heavy', 'special']:
            if target_pool == 'heavy':
                self.gacha_bottom_navbar_ensure(right=2, is_build=True)
            else:
                self.gacha_bottom_navbar_ensure(right=1, is_build=True)

        return target_pool

    def gacha_flush_queue(self, skip_first_screenshot=True):
        """
        Очистка очереди заказов на постройку перед новой отправкой.

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Pages:
            in: page_build (любая подстраница)
            out: page_build (выбор пула постройки)

        Raises:
            ScriptError: Вызывается, если не удалось полностью очистить очередь (возможно, док переполнен).
        """
        # Переходим на страницу строительства/заказов
        self.gacha_side_navbar_ensure(bottom=3)

        # Переключаемся на нужный экран и в итоге возвращаемся на страницу строительства
        confirm_timer = Timer(1, count=2).start()
        confirm_mode = True  # Учения, блокировка кораблей
        # Сбрасываем смещение кнопки, иначе нажатие попадёт на PLUS самоцветов или HOME
        STORY_SKIP.clear_offset()
        queue_clean = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(BUILD_QUEUE_EMPTY, offset=(20, 20)) and queue_clean:
                self.gacha_side_navbar_ensure(upper=1)
                break
            else:
                queue_clean = False

            if self.appear_then_click(BUILD_FINISH_ORDERS, interval=3):
                confirm_timer.reset()
                continue

            if self.handle_retirement():
                confirm_timer.reset()
                continue

            if self.handle_popup_confirm('FINISH_ORDERS'):
                if confirm_mode:
                    self.device.sleep((0.5, 0.8))
                    self.device.click(BUILD_FINISH_ORDERS)  # Пропускаем анимацию, безопасная область
                    confirm_mode = False
                confirm_timer.reset()
                continue

            if self.appear(GET_SHIP, interval=1):
                self.device.click(STORY_SKIP)  # При нескольких заказах ускоряем просмотр
                confirm_timer.reset()
                continue
            if self.handle_get_items_ship():
                continue

            if self.appear(BUILD_FINISH_RESULTS, offset=(20, 150), interval=3):
                self.device.click(BUILD_FINISH_ORDERS)  # Безопасная область
                confirm_timer.reset()
                continue

            # Завершение: после нажатия при пустой очереди происходит возврат к пулу строительства
            if self.appear(BUILD_SUBMIT_ORDERS) or self.appear(BUILD_SUBMIT_WW_ORDERS):
                if confirm_timer.reached():
                    break

        # В Колодце желаний монеты больше не отображаются; возвращаемся к обычному пулу
        if self.appear(BUILD_SUBMIT_WW_ORDERS):
            logger.info('Находимся в Колодце желаний; возврат к обычному пулу')
            self.gacha_side_navbar_ensure(upper=1)

    def gacha_submit(self, skip_first_screenshot=True):
        """
        Отправка заказов на постройку.

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Pages:
            in: POPUP_CONFIRM
            out: BUILD_FINISH_ORDERS
        """
        logger.info('Отправка заказов на строительство')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(POPUP_CONFIRM, offset=(20, 80), interval=3):
                # Временно меняем имя ресурса для нажатия
                POPUP_CONFIRM.name = POPUP_CONFIRM.name + '_' + 'GACHA_ORDER'
                self.device.click(POPUP_CONFIRM)
                POPUP_CONFIRM.name = POPUP_CONFIRM.name[:-len('GACHA_ORDER') - 1]
                continue

            # Завершение
            if self.appear(BUILD_FINISH_ORDERS):
                break

    def gacha_run(self):
        """
        Выполнение операции постройки: отправка заказов на строительство.

        Returns:
            bool: True при успешном выполнении, иначе False.

        Pages:
            in: Любая страница
            out: page_build
        """
        # Переходим на страницу строительства
        self.ui_goto_gacha()

        # Очищаем текущую очередь строительства, чтобы начать с пустой
        # После выхода ожидается главная страница строительства
        self.gacha_flush_queue()

        # Через OCR считываем количество монет и Кубов мудрости
        self.build_coin_count = self.get_coin()
        self.build_cube_count = OCR_BUILD_CUBE_COUNT.ocr(self.device.image)

        # Переходим к целевому пулу строительства и определяем соответствующую стоимость
        actual_pool = self.gacha_goto_pool(self.config.Gacha_Pool)

        # Определяем стоимость по результату gacha_goto_pool
        gold_cost = 600
        cube_cost = 1
        if actual_pool in ['heavy', 'special', 'event', 'wishing_well']:
            gold_cost = 1500
            cube_cost = 2

        # Через OCR считываем число билетов строительства и решаем, использовать ли Кубы мудрости/монеты
        # buy = [число строительств за билеты, число строительств за Кубы мудрости]
        buy = [self.config.Gacha_Amount, 0]
        if actual_pool == "event" and self.config.Gacha_UseTicket:
            if self.appear(BUILD_TICKET_CHECK, offset=(30, 30)):
                self.build_ticket_count = OCR_BUILD_TICKET_COUNT.ocr(self.device.image)
            else:
                logger.info('Билет на строительство не обнаружен; используются Кубы мудрости и монеты')
        if self.config.Gacha_Amount > self.build_ticket_count:
            buy[0] = self.build_ticket_count
            # По конфигурации и ресурсам рассчитываем допустимое число строительств
            buy[1] = self.gacha_calculate(self.config.Gacha_Amount - self.build_ticket_count, gold_cost, cube_cost)
        else:
            LogRes(self.config).Cube = self.build_cube_count
            self.config.update()

        # Отправляем buy_count и выполняем строительство
        # handle_popup_confirm использовать нельзя, потому что в этом окне нет POPUP_CANCEL
        result = False
        for buy_count in buy:
            if self.gacha_prep(buy_count):
                self.gacha_submit()

                # Если настроено использование ускорителя после строительства
                if self.config.Gacha_UseDrill:
                    self.gacha_flush_queue()
                # Возвращаем True, если хотя бы одна отправка прошла успешно
                result = True

        return result

    def run(self):
        """
        Выполнение операции постройки в соответствии с конфигурацией.

        Pages:
            in: Любая страница
            out: page_build
        """
        self.gacha_run()
        self.config.task_delay(server_update=True)
