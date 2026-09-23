"""Модуль выполнения задач комиссий (поручений).

Отвечает за автоматизацию системы комиссий в Azur Lane, включая сбор наград, обнаружение поручений,
их фильтрацию и выбор, запуск и сбор статистики по полученным предметам. Поддерживает две основные категории:
ежедневные и срочные комиссии, распознаёт параметры поручений через OCR и шаблоны,
автоматически подбирая оптимальную комбинацию на основе правил пользователя.

Основные этапы:
    1. Переход на страницу комиссий со страницы наград
    2. Сбор наград за завершённые комиссии (commission_receive)
    3. Сканирование всего текущего списка комиссий (_commission_scan_all)
    4. Выбор запускаемых комиссий согласно правилам фильтрации (_commission_choose)
    5. Поочерёдный поиск и запуск выбранных комиссий (commission_start)
    6. Расчёт времени следующего запуска на основе времени завершения активных поручений

Зависимости:
    - module.commission.project: парсинг данных комиссий (класс Commission)
    - module.commission.preset: пресеты правил фильтрации
    - module.ui.ui: навигация по страницам
    - module.handler.info_handler: обработка всплывающих окон и информационной строки
"""

import copy
from datetime import timedelta
from enum import StrEnum

from scipy import signal

from module.application.commission_recovery import (
    MAX_WEEKLY_ACTION_POINT_PURCHASES,
    CommissionRecoveryStore,
)
from module.application.errors import StorageError
from module.base.timer import Timer
from module.base.utils import *
from module.combat.assets import *
from module.commission.assets import *
from module.commission.preset import DICT_FILTER_PRESET, SHORTEST_FILTER
from module.commission.project import COMMISSION_FILTER, Commission
from module.config.config_generated import GeneratedConfig
from module.config.time_source import now as current_time
from module.os.action_point_policy import ACTION_POINT_GAIN_PER_PURCHASE
from module.config.utils import (
    get_server_last_update,
    get_server_next_update,
    nearest_future,
)
from module.dorm.dorm import RewardDorm
from module.exception import GameStuckError, OilMaxed, RequestHumanTakeover
from module.handler.info_handler import InfoHandler
from module.logger import logger
from module.map.map_grids import SelectedGrids
from module.notify.notify import handle_notify, notify_webui
from module.os_handler.action_point import (
    ActionPointHandler,
    EmergencyActionPointPurchaseStatus,
)
from module.retire.assets import DOCK_CHECK
from module.tactical.assets import TACTICAL_CLASS_CANCEL, TACTICAL_CLASS_START
from module.ui.assets import BACK_ARROW, REWARD_GOTO_COMMISSION
from module.ui.page import page_commission, page_os, page_reward
from module.ui.scroll import Scroll
from module.ui.switch import Switch
from module.ui.ui import UI
from module.ui_white.assets import REWARD_1_WHITE, REWARD_GOTO_COMMISSION_WHITE

COMMISSION_SWITCH = Switch('Commission_switch', is_selector=True)
COMMISSION_SWITCH.add_state('daily', COMMISSION_DAILY)
COMMISSION_SWITCH.add_state('urgent', COMMISSION_URGENT)
COMMISSION_SCROLL = Scroll(COMMISSION_SCROLL_AREA, color=(247, 211, 66), name='COMMISSION_SCROLL')


class CommissionRecoveryOutcome(StrEnum):
    """Typed outcome ограниченного восстановления переполнения нефти."""

    AP_RECOVERED = "recovered_via_ap"
    DORM_RECOVERED = "recovered_via_confirmed_zero_dorm"
    BLOCKED = "blocked"
    AMBIGUOUS_MUTATION = "ambiguous_mutation"


def lines_detect(image):
    """Определяет координаты белых разделительных линий внизу каждого элемента комиссии в списке.

    Анализирует среднюю яркость в градациях серого в области разделительной линии (x: 597-619)
    и использует scipy.signal.find_peaks для поиска Y-координат белых линий.

    Args:
        image (np.ndarray): Снимок экрана игры.

    Returns:
        np.ndarray: Массив Y-координат белых разделительных линий под каждой комиссией.
    """
    # Позиция поручения определяется поиском белой разделительной линии снизу.
    # (597, 0, 619, 720) — область, содержащая только белую разделительную линию.
    color_height = np.mean(rgb2gray(crop(image, (597, 0, 619, 720), copy=False)), axis=1)
    parameters = {'height': 200, 'distance': 100}
    peaks, _ = signal.find_peaks(color_height, **parameters)
    # 67 — высота заголовка списка поручений
    # 117 — высота одной карточки поручения.
    peaks = [y for y in peaks if y > 67 + 117]
    return np.array(peaks)


class RewardCommission(UI, InfoHandler):
    """Обработчик задач комиссий.

    Наследует UI и InfoHandler, реализуя полный цикл автоматизации системы комиссий:
    обнаружение, выбор по фильтрам, запуск и получение наград.

    Attributes:
        daily (SelectedGrids): Список текущих обнаруженных ежедневных комиссий.
        urgent (SelectedGrids): Список текущих обнаруженных срочных комиссий.
        daily_choose (SelectedGrids): Выбранные фильтром для запуска ежедневные комиссии.
        urgent_choose (SelectedGrids): Выбранные фильтром для запуска срочные комиссии.
        comm_choose (SelectedGrids): Все выбранные комиссии (ежедневные и срочные),
            используемые для планирования и расчёта задержки задач.
        max_commission (int): Максимальное число одновременно выполняемых комиссий, по умолчанию 4.
            При наличии комиссии события (daily_event) увеличивается до 5.
    """

    daily: SelectedGrids
    urgent: SelectedGrids
    daily_choose: SelectedGrids
    urgent_choose: SelectedGrids
    comm_choose: SelectedGrids
    max_commission = 4

    def _commission_detect(self, image):
        """
        Извлекает все комиссии из изображения.

        Args:
            image (np.ndarray): Входное изображение.

        Returns:
            SelectedGrids: Набор обнаруженных объектов Commission.
        """
        logger.hr('Обнаружение комиссий')
        commission = []
        for y in lines_detect(image):
            comm = Commission(image, y=y, config=self.config)
            logger.attr('Комиссия', comm)
            repeat = len([c for c in commission if c == comm])
            comm.repeat_count += repeat
            commission.append(comm)

        return SelectedGrids(commission)

    def commission_detect(self, trial=1, area=None, skip_first_screenshot=True):
        """
        Обнаруживает комиссии на экране с поддержкой повторных попыток.

        Args:
            trial (int): Количество попыток при обнаружении невалидных комиссий
                         (обычно из-за неисчезнувшей информационной строки info_bar).
            area (tuple): Ограничивающая область обрезки либо None.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            SelectedGrids: Набор обнаруженных комиссий.
        """
        commissions = SelectedGrids([])
        for _ in range(trial):
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            image = self.device.image
            if area is not None:
                image = crop(image, area, copy=False)
            commissions = self._commission_detect(image)

            if commissions.count >= 2 and commissions.select(valid=False).count == 1:
                logger.warning('[Комиссия — обнаружение] Найдена 1 некорректная комиссия; повторное обнаружение')
                continue
            else:
                return commissions

        logger.info('[Комиссия — обнаружение] Попытки повторного обнаружения исчерпаны; остановка')
        return commissions

    def _commission_choose(self, daily, urgent):
        """
        Выбирает комиссии для запуска на основе правил фильтрации.

        Args:
            daily (SelectedGrids): Список ежедневных комиссий.
            urgent (SelectedGrids): Список срочных комиссий.

        Returns:
            SelectedGrids, SelectedGrids: Выбранные ежедневные комиссии, выбранные срочные комиссии.
        """
        self.comm_choose = SelectedGrids([])
        # Подсчет количества поручений
        total = daily.add_by_eq(urgent)
        # Поручения с большим номером суффикса всегда расположены ниже меньших номеров
        # Разворачиваем список поручений для приоритета больших суффиксов
        total = total[::-1]
        self.max_commission = 4
        for comm in total:
            if comm.genre == 'daily_event':
                self.max_commission = 5
        running_list = [c for c in total if c.status == 'running']
        running_count = len(running_list)
        logger.attr('Выполняется', f'{running_count}/{self.max_commission}')

        # Загрузка строки фильтра
        preset = self.config.Commission_PresetFilter
        if preset == 'custom':
            string = self.config.Commission_CustomFilter
        else:
            if f'{preset}_night' in DICT_FILTER_PRESET:
                start_time = get_server_last_update('02:00')
                end_time = get_server_last_update('21:00')
                if start_time < end_time:
                    preset = f'{preset}_night'
            if preset not in DICT_FILTER_PRESET:
                logger.warning(f'[Комиссия — фильтр] Предустановка не найдена: {preset}; используется предустановка по умолчанию')
                preset = GeneratedConfig.Commission_PresetFilter
            string = DICT_FILTER_PRESET[preset]
        logger.attr('Фильтр комиссий', preset)

        # Фильтрация
        COMMISSION_FILTER.load(string)
        run = COMMISSION_FILTER.apply(total.grids, func=self._commission_check)
        logger.attr('Порядок фильтрации', ' > '.join([str(c) for c in run]))
        run = SelectedGrids(run)

        # Добавляем поручения с наименьшим временем
        if self.config.Commission_AddShortest == False and preset == 'custom':
            logger.info('[Комиссия — выбор] Недостаточно комиссий для запуска')
        else:
            no_shortest = run.delete(SelectedGrids(['shortest']))
            if no_shortest.count + running_count < self.max_commission:
                if daily.count:
                    logger.info('[Комиссия — выбор] Недостаточно комиссий для запуска; добавляем самую короткую ежедневную комиссию')
                    COMMISSION_FILTER.load(SHORTEST_FILTER)
                    shortest = COMMISSION_FILTER.apply(daily[::-1], func=self._commission_check)
                    # Разворачиваем список ежедневных поручений для выбора лучших
                    run = no_shortest.add_by_eq(SelectedGrids(shortest))
                    logger.attr('Порядок фильтрации', ' > '.join([str(c) for c in run]))
                else:
                    logger.info('[Комиссия — выбор] Недостаточно комиссий для запуска')

        # Приоритетная обработка истекающих важных поручений
        if 'expire' in run:
            logger.info('[Комиссия] Попытка заранее выполнить скоро истекающую комиссию')

            valid_runs = [c for c in run if isinstance(c, Commission)]
            queue = running_list + valid_runs[:self.max_commission - running_count]

            if queue:
                min_duration_time = queue[0].duration
                for c in queue:
                    if c.duration < min_duration_time:
                        min_duration_time = c.duration
            else:
                min_duration_time = timedelta(seconds=0)
            logger.attr('Минимальная длительность', min_duration_time)

            expire_index = run.grids.index('expire')
            important = run[:expire_index].filter(lambda c: isinstance(c, Commission) and c.expire)
            priority = [c for c in important if c.expire < min_duration_time]
            run = run.delete(SelectedGrids(['expire']))
            run = SelectedGrids(priority).add_by_eq(run)
            logger.attr('Порядок фильтрации', ' > '.join([str(c) for c in run]))

        self.comm_choose = run
        if running_count >= self.max_commission:
            return SelectedGrids([]), SelectedGrids([])

        # Разделение ежедневных и срочных поручений
        run = run[:self.max_commission - running_count]
        daily_choose = run.intersect_by_eq(daily)
        urgent_choose = run.intersect_by_eq(urgent)
        if daily_choose:
            logger.info('[Комиссия — выбор] Выбор ежедневных комиссий')
            for comm in daily_choose:
                logger.info(comm)
        if urgent_choose:
            logger.info('[Комиссия — выбор] Выбор срочных комиссий')
            for comm in urgent_choose:
                logger.info(comm)

        return daily_choose, urgent_choose

    def _commission_check(self, commission):
        """Проверяет, подходит ли комиссия под условия выполнения.

        Отфильтровывает невалидные комиссии, комиссии в не ожидающем запуска статусе,
        а также основные сюжетные комиссии (major commission), если они отключены в настройках.

        Args:
            commission (Commission): Проверяемый объект комиссии.

        Returns:
            bool: Может ли данная комиссия быть запущена.
        """
        if not commission.valid or commission.status != 'pending':
            return False
        if not self.config.Commission_DoMajorCommission and commission.category_str == 'major':
            return False

        return True

    def _commission_ensure_mode(self, mode):
        """Переключает режим отображения списка комиссий (ежедневные/срочные).

        После переключения в указанный режим ожидает завершения анимации прокрутки списка,
        чтобы избежать ложного или неполного распознавания карточек в процессе анимации.

        Args:
            mode (str): Целевой режим, 'daily' или 'urgent'.

        Returns:
            bool: Успешно ли выполнено переключение.
        """
        if COMMISSION_SWITCH.set(mode, main=self):
            # Когда в списке ежедневных поручений больше 4 (обычно 5), а срочных от 1 до 4,
            # в списке поручений возникает анимация прокрутки,
            # из-за чего верхнее поручение не обнаруживается.
            if not COMMISSION_SCROLL.appear(main=self) or COMMISSION_SCROLL.cal_position(main=self) < 0.05 or COMMISSION_SCROLL.length / COMMISSION_SCROLL.total > 0.98:
                pre_peaks = lines_detect(self.device.image)
                self.device.screenshot()
                while 1:
                    peaks = lines_detect(self.device.image)
                    if (not len(peaks) or peaks[0] > 67 + 117) and (not len(pre_peaks) or not len(peaks) or abs(peaks[0] - pre_peaks[0]) < 3):
                        break
                    pre_peaks = peaks
                    self.device.screenshot()

            return True
        else:
            return False

    def _commission_mode_reset(self):
        """Сбрасывает режим отображения списка комиссий.

        Сначала переключается на другой режим, затем возвращается в текущий, принудительно обновляя список.
        Используется для восстановления состояния списка после неудачного запуска поручения.

        Returns:
            bool: Успешен ли сброс режима. Возвращает False, если текущий режим не распознан.
        """
        logger.hr('Сброс режима комиссий')
        if self.appear(COMMISSION_DAILY):
            current, another = 'daily', 'urgent'
        elif self.appear(COMMISSION_URGENT):
            current, another = 'urgent', 'daily'
        else:
            logger.warning('[Комиссия — режим] Неизвестный режим комиссий')
            return False

        self._commission_ensure_mode(another)
        self._commission_ensure_mode(current)

        return True

    def _commission_swipe(self):
        """Прокручивает список комиссий на одну страницу вниз.

        Если полоса прокрутки видна и ещё не достигла низа, листает страницу вниз; иначе возвращает False.

        Returns:
            bool: Была ли выполнена прокрутка. Возвращает False, если полоса прокрутки не видна или достигла низа.
        """
        if COMMISSION_SCROLL.appear(main=self):
            if COMMISSION_SCROLL.at_bottom(main=self):
                return False
            else:
                COMMISSION_SCROLL.next_page(main=self)
                return True
        else:
            return False

    def _commission_swipe_to_top(self):
        """Прокручивает список комиссий в самый верх.

        Returns:
            bool: Была ли выполнена операция прокрутки. Возвращает False, если полоса прокрутки не видна.
        """
        if not COMMISSION_SCROLL.appear(main=self):
            return False
        COMMISSION_SCROLL.set_top(main=self, skip_first_screenshot=True)
        return True

    def _commission_scan_list(self):
        """
        Сканирует текущий список комиссий с постраничной прокруткой.

        Returns:
            SelectedGrids: Набор объектов Commission из отсканированного списка.
        """
        self.device.click_record_clear()
        commission = SelectedGrids([])
        for _ in range(15):
            new = self.commission_detect(trial=2)
            commission = commission.add_by_eq(new)

            # Конец
            if not self._commission_swipe():
                break

        self.device.click_record_clear()
        return commission

    def _commission_scan_all(self):
        """
        Pages:
            in: page_commission
            out: page_commission
        """
        logger.hr('Сканирование комиссий', level=1)
        # Список срочных поручений загружается лениво; переключаем для принудительного обновления.
        self._commission_ensure_mode('urgent')

        logger.hr('Сканирование ежедневных комиссий', level=2)
        self._commission_ensure_mode('daily')
        self._commission_swipe_to_top()
        daily = self._commission_scan_list()

        urgent = SelectedGrids([])
        for _ in range(2):
            logger.hr('Сканирование срочных комиссий', level=2)
            self._commission_ensure_mode('urgent')
            self._commission_swipe_to_top()
            urgent = self._commission_scan_list()
            # Преобразуем дополнительное поручение в ночное
            urgent.call('convert_to_night')

            # Вне диапазона 21:00~03:00, но обнаружено ночное поручение
            # Возможно, просроченное поручение; решается обновлением
            if current_time() - get_server_next_update('21:00') > timedelta(hours=6):
                night = urgent.select(category_str='night')
                if night:
                    logger.warning('[Комиссия — сканирование] Вне интервала 21:00–03:00 обнаружена ночная комиссия')
                    for comm in night:
                        logger.attr('Комиссия', comm)
                    logger.info('[Комиссия — сканирование] Повторное сканирование списка срочных комиссий')
                    # Не лучший вариант, но допустим в редких случаях
                    self.device.sleep(2)
                    self._commission_ensure_mode('daily')
                    continue

            break

        logger.hr('Список комиссий', level=2)
        logger.info('[Комиссия — список] Ежедневные комиссии')
        for comm in daily.sort('status', 'genre'):
            logger.attr('Комиссия', comm)
        if urgent.count:
            logger.info('[Комиссия — список] Срочные комиссии')
            for comm in urgent.sort('status', 'genre'):
                logger.attr('Комиссия', comm)

        self.daily = daily
        self.urgent = urgent
        self.daily_choose, self.urgent_choose = self._commission_choose(self.daily, self.urgent)
        return daily, urgent

    def _commission_start_click(self, comm, is_urgent=False, skip_first_screenshot=True):
        """
        Запускает одну выбранную комиссию.

        Args:
            comm (Commission): Объект комиссии.
            is_urgent (bool): Является ли комиссия срочной.
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Returns:
            bool: Успешен ли запуск.

        Pages:
            in: page_commission
            out: page_commission, info_bar, раскрытые детали комиссии
        """
        logger.hr('Запуск комиссии')
        self.interval_clear(COMMISSION_ADVICE)
        self.interval_clear(COMMISSION_START)
        comm_timer = Timer(7)
        count = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Конец
            if self.info_bar_count():
                break
            if count >= 3:
                # Перезапуск игры для обхода бага рекомендации в поручениях.
                # После клика «Рекомендовать» корабли появляются и внезапно исчезают.
                # При этом иконка поручения мигает.
                logger.warning('[Комиссия — запуск] Сработала ошибка мигания списка комиссий')
                raise GameStuckError('[Комиссия — запуск] Сработала ошибка мигания списка комиссий')

            # Клик
            if self.match_template_color(COMMISSION_START, offset=(5, 20), interval=7):
                self.device.click(COMMISSION_START)
                self.interval_reset(COMMISSION_ADVICE)
                comm_timer.reset()
                continue
            if self.handle_popup_confirm('COMMISSION_START'):
                self.interval_reset(COMMISSION_ADVICE)
                comm_timer.reset()
                continue
            # Случайный вход в док
            if self.appear(DOCK_CHECK, offset=(20, 20), interval=3):
                logger.info(f'[Комиссия — запуск] Ошибочный вход в док {DOCK_CHECK} -> {BACK_ARROW}')
                self.device.click(BACK_ARROW)
                comm_timer.reset()
                continue
            # Проверка корректности поручения
            if self.appear(COMMISSION_ADVICE, offset=(5, 20), interval=7):
                area = (0, 0, image_size(self.device.image)[0], COMMISSION_ADVICE.button[1])
                current = self.commission_detect(area=area)
                if is_urgent:
                    current.call('convert_to_night')  # Преобразуем дополнительное поручение в ночное
                if current.count >= 1:
                    current = current[0]
                    if current == comm:
                        logger.info('[Комиссия — запуск] Выбрана правильная комиссия')
                    else:
                        logger.warning('[Комиссия — запуск] Выбрана неправильная комиссия')
                        return False
                else:
                    logger.warning('[Комиссия — запуск] Выбранная комиссия не определена; считаем выбор правильным')
                self.device.click(COMMISSION_ADVICE)
                count += 1
                self.interval_reset(COMMISSION_ADVICE)
                self.interval_clear(COMMISSION_START)
                comm_timer.reset()
                continue
            # Вход в поручение
            if comm_timer.reached():
                self.device.click(comm.button)
                self.device.sleep(0.3)
                comm_timer.reset()

        return True

    def _commission_find_and_start(self, comm, is_urgent=False):
        """
        Находит и запускает указанную комиссию.

        Args:
            comm (Commission): Объект запускаемой комиссии.
            is_urgent (bool): Является ли комиссия срочной.
        """
        self.device.click_record_clear()
        comm = copy.deepcopy(comm)
        comm.repeat_count = 1
        for _ in range(3):
            logger.hr('Поиск и запуск комиссии', level=2)
            logger.info(f'[Комиссия — поиск] Поиск комиссии {comm}')

            failed = True

            for _ in range(15):
                new = self.commission_detect(trial=2)
                if is_urgent:
                    new.call('convert_to_night')  # Преобразуем дополнительное поручение в ночное

                # Обновление позиции поручения.
                # В разных сканированиях данные поручения совпадают, но позиция может меняться.
                current = None
                for new_comm in new:
                    if new_comm == comm:
                        current = new_comm
                if current is not None:
                    if self._commission_start_click(current, is_urgent=is_urgent):
                        self.device.click_record_clear()
                        return True
                    else:
                        self._commission_mode_reset()
                        self._commission_swipe_to_top()
                        failed = False
                        break

                # Условие завершения
                if not self._commission_swipe():
                    break

            if failed:
                logger.warning(f'[Комиссия — поиск] Не удалось выбрать комиссию: {comm}')
                self._commission_mode_reset()
                self._commission_swipe_to_top()
                self.device.click_record_clear()
                continue
            else:
                logger.warning(f'[Комиссия — поиск] Комиссия не найдена: {comm}')
                self.device.click_record_clear()
                return False

        logger.warning('[Комиссия — поиск] Не удалось выбрать комиссию после 3 попыток')
        self.device.click_record_clear()
        return False

    def commission_start(self):
        """
        Сканирует и запускает все выбранные комиссии.

        Pages:
            in: page_commission
            out: page_commission
        """
        self._commission_scan_all()

        logger.hr('Выполнение комиссий', level=1)
        if self.daily_choose:
            for comm in self.daily_choose:
                self._commission_ensure_mode('daily')
                self._commission_swipe_to_top()
                self.handle_info_bar()
                if self._commission_find_and_start(comm, is_urgent=False):
                    comm.convert_to_running()
                self._commission_mode_reset()
        if self.urgent_choose:
            for comm in self.urgent_choose:
                self._commission_ensure_mode('urgent')
                self._commission_swipe_to_top()
                self.handle_info_bar()
                if self._commission_find_and_start(comm, is_urgent=True):
                    comm.convert_to_running()
                self._commission_mode_reset()
        if not self.daily_choose and not self.urgent_choose:
            logger.info('[Комиссия — выполнение] Не выбрано ни одной комиссии')

    def _record_commission_income(self):
        """
        Регистрирует полученные награды комиссий (предметы).

        Анализирует скриншоты, сохранённые в `_commission_reward_images` во время сбора наград,
        распознаёт определённые предметы (алмазы, кубы, когнитивные чипы, нефть, монеты),
        суммирует их количество и сохраняет в базу данных.
        """
        try:
            from module.statistics.get_items import (
                GetItemsStatistics, ITEM_GRIDS_1_ODD, ITEM_GRIDS_1_EVEN,
                ITEM_GRIDS_2, ITEM_GRIDS_3
            )
            from module.statistics.item import ItemGrid, Item
            from module.application.runtime_storage import get_runtime_storage
            from module.statistics.postgresql_stats import get_commission_reward_stats
            from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3
            from module.handler.assets import INFO_BAR_1
            import os

            template_folder = os.path.join('.', 'assets', 'stats_commission_items')
            if not os.path.exists(template_folder):
                logger.info('[Комиссия — награды] Каталог шаблонов отсутствует; пропуск')
                return

            grid = ItemGrid(None, {}, template_area=(40, 21, 89, 70), amount_area=(50, 71, 91, 92))
            grid.item_class = Item
            grid.similarity = 0.92
            grid.load_template_folder(template_folder)

            if not grid.templates:
                logger.info('[Комиссия — награды] Шаблоны не загружены; пропуск')
                return

            get_items = GetItemsStatistics()

            merged_items = {}
            item_count = 0

            images = getattr(self, '_commission_reward_images', None)
            if not images:
                logger.info('[Комиссия — награды] Снимки наград не собраны')
                return

            COMMISSION_TRACKED_ITEMS = ['Gem', 'Cube', 'Chip', 'Oil', 'Coin']

            COMMISSION_ITEM_NAME_MAP = {
                'Gems': 'Gem',
                'Cubes': 'Cube',
                'CognitiveChips': 'Chip',
                'Coins': 'Coin',
            }

            logger.info(f'[Комиссия — награды] Обработка снимков наград: {len(images)}')
            for idx, image in enumerate(images):
                try:
                    if INFO_BAR_1.appear_on(image):
                        logger.info(f'[Комиссия — награды] На снимке[{idx}] есть информационная панель; пропуск')
                        continue
                    grid.grids = None
                    if GET_ITEMS_1.match_template_color(image, offset=(5, 0)):
                        is_odd = get_items._stats_get_items_is_odd(image)
                        grid.grids = ITEM_GRIDS_1_ODD if is_odd else ITEM_GRIDS_1_EVEN
                    elif GET_ITEMS_2.match_template_color(image, offset=(5, 0)):
                        grid.grids = ITEM_GRIDS_2
                    elif GET_ITEMS_3.match_template_color(image, offset=(5, 0)):
                        grid.grids = ITEM_GRIDS_3
                    else:
                        logger.info(f'[Комиссия — награды] Снимок[{idx}] не является экраном получения предметов; пропуск')
                        continue
                    grid.predict(image)
                    recognized = []
                    for item in grid.items:
                        if item.is_known_item() and item.name not in ('DefaultItem',):
                            mapped_name = COMMISSION_ITEM_NAME_MAP.get(item.name, item.name)
                            if mapped_name not in COMMISSION_TRACKED_ITEMS:
                                logger.info(f'[Комиссия — награды] Снимок[{idx}]: {item.name} пропущен (не отслеживается)')
                                continue
                            merged_items[mapped_name] = merged_items.get(mapped_name, 0) + item.amount
                            item_count += 1
                            recognized.append(f'{mapped_name}x{item.amount}')
                    if recognized:
                        logger.info(f'[Комиссия — награды] На снимке[{idx}] распознано предметов: {len(recognized)}: {", ".join(recognized)}')
                    else:
                        logger.info(f'[Комиссия — награды] На снимке[{idx}] не распознаны известные предметы')
                except Exception as e:
                    logger.info(f'[Комиссия — награды] Ошибка распознавания снимка[{idx}]: {e}')
                    continue

            if merged_items:
                instance = self.config.config_name
                get_runtime_storage().record_commission_income(
                    instance, merged_items, commission_count=1
                )
                item_str = ', '.join([f'{k}x{v}' for k, v in merged_items.items()])
                logger.info(f'[Комиссия — награды] Запись дохода комиссии: {item_str} (экземпляр={instance})')
                if self.config.Commission_CommissionNotifyReward:
                    reward_stats = None
                    if self.config.Commission_CommissionNotifyRewardStatistics:
                        reward_stats = get_commission_reward_stats(instance)
                    gem_count = merged_items.get("Gem", 0)
                    tracked = []
                    if gem_count > 0:
                        text = f'Получено гемов * {gem_count}'
                        if reward_stats:
                            text += (
                                f'\n\nИтого за сегодня: {reward_stats["today"].get("Gem", 0)}'
                                f'\nИтого за неделю: {reward_stats["week"].get("Gem", 0)}'
                                f'\nИтого за месяц: {reward_stats["month"].get("Gem", 0)}'
                            )
                        tracked.append(text)
                    if tracked:

                        msg = '\n'.join(tracked)
                        webui_msg = msg.replace('\n\n', '\n')
                        title = f"AzurPilot <{instance}> Комиссия принесла награду, мяу!"
                        webui_title = f"AzurPilot <{instance}> Комиссия принесла награду, мяу!"
                        if gem_count >= 50:
                            title = f"AzurPilot <{instance}> Большой успех!!! Комиссия принесла отличную награду, мяу!"
                            webui_title = f"AzurPilot <{instance}> Большой успех!!! Комиссия принесла отличную награду, мяу!"

                        elif gem_count > 0:
                            title = f"AzurPilot <{instance}> Комиссия принесла отличную награду, мяу!"
                            webui_title = f"AzurPilot <{instance}> Комиссия принесла отличную награду, мяу!"
                        handle_notify(
                            self.config.Error_OnePushConfig,
                            title=title,
                            content=msg,
                        )

                        notify_webui(
                            instance,
                            title=webui_title,
                            content=webui_msg,
                        )

            else:
                logger.info('[Комиссия — награды] Ни на одном снимке не распознаны известные предметы')

        except StorageError:
            raise
        except Exception as e:
            logger.warning(f'[Комиссия — награды] Не удалось записать доход комиссии: {e}')

    def _handle_research_genre_t_update(self, completed_commission_count):
        """Обновляет оставшийся счётчик комиссий для исследований типа T.

        Когда активно исследование типа T (требующее завершения определённого числа комиссий),
        вычитает количество завершённых комиссий из оставшегося счётчика. При обнулении счётчика вызывает планировщик задачи Research.

        Args:
            completed_commission_count (int): Количество комиссий, завершённых при текущем сборе наград.
        """
        if completed_commission_count <= 0:
            return
        required_commissions = self.config.cross_get('Research.Research.RemainingCommissions', -1)
        if required_commissions <= -1:
            return

        new_value = max(required_commissions - completed_commission_count, 0)
        logger.info(f'Исследование типа T требует выполнить комиссий: {required_commissions}; выполнено: {completed_commission_count}; осталось: {new_value}')
        self.config.cross_set('Research.Research.RemainingCommissions', new_value)
        if new_value <= 0:
            logger.info('Требование исследования типа T выполнено; вызываем задачу Research')
            self.config.task_call('Research')

    def _commission_receive(self, skip_first_screenshot=True):
        """Получает награды за завершённые комиссии.

        Циклически обрабатывает страницу комиссий и страницу наград, нажимая на все доступные окна наград
        (опыт, предметы, корабли), параллельно собирая снимки наград для статистики дохода.
        Обрабатывает переполнение запасов нефти (запуск кормления в общежитии для траты нефти).

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана, используя снимок из предыдущего состояния.

        Returns:
            bool: Были ли получены какие-либо награды.

        Raises:
            OilMaxed: Вызывается, если нефть переполнена и трёхкратное кормление не помогло решить проблему.
        """
        logger.hr('Получение наград')

        reward = False
        click_timer = Timer(1)
        self._commission_reward_images = []
        completed_commission_count = 0

        try:
            with self.stat.new(
                    'commission', method=self.config.DropRecord_CommissionRecord
            ) as drop:
                while 1:
                    if skip_first_screenshot:
                        skip_first_screenshot = False
                    else:
                        self.device.screenshot()

                    if self.ui_page_appear(page_commission, offset=(20, 20)):
                        break

                    for button in [EXP_INFO_S_REWARD, GET_ITEMS_1, GET_ITEMS_2, GET_ITEMS_3]:
                        if self.appear(button, interval=1):
                            self.ensure_no_info_bar(timeout=1)

                            if drop:
                                drop.add(self.device.image)

                            if button is EXP_INFO_S_REWARD:
                                completed_commission_count += 1
                                if self._commission_reward_images:
                                    self._record_commission_income()
                                    self._commission_reward_images = []
                            else:
                                self._commission_reward_images.append(self.device.image.copy())
                                logger.info(f'[Комиссия — награды] Сохранён снимок награды (кнопка={button.name})')

                            REWARD_SAVE_CLICK.name = button.name
                            self.device.click(REWARD_SAVE_CLICK)
                            if button is EXP_INFO_S_REWARD:
                                self.device.sleep(0.3)
                            click_timer.reset()
                            reward = True
                            continue
                    if click_timer.reached() and self.appear_then_click(REWARD_1, offset=(20, 20), interval=1):
                        self.interval_reset(GET_SHIP)
                        click_timer.reset()
                        reward = True
                        continue
                    if click_timer.reached() and self.appear_then_click(REWARD_1_WHITE, offset=(20, 20), interval=1):
                        self.interval_reset(GET_SHIP)
                        click_timer.reset()
                        reward = True
                        continue
                    if click_timer.reached() and self.appear_then_click(REWARD_GOTO_COMMISSION, offset=(20, 20)):
                        self.interval_reset(GET_SHIP)
                        click_timer.reset()
                        continue
                    if click_timer.reached() and self.appear_then_click(REWARD_GOTO_COMMISSION_WHITE, offset=(20, 20)):
                        self.interval_reset(GET_SHIP)
                        click_timer.reset()
                        continue
                    if self.ui_main_appear_then_click(page_reward, interval=3):
                        self.interval_reset(GET_SHIP)
                        continue

                    if self.config.SERVER == 'en':
                        if self.appear(OIL_MAXED, offset=(20, 20), interval=3):
                            raise OilMaxed

                    for button in [GET_SHIP]:
                        if click_timer.reached() and self.appear(button, interval=1):
                            self.ensure_no_info_bar(timeout=1)
                            drop.add(self.device.image)

                            REWARD_SAVE_CLICK.name = button.name
                            self.device.click(REWARD_SAVE_CLICK)
                            click_timer.reset()
                            reward = True
                            continue
                    if click_timer.reached() and self.ui_additional():
                        click_timer.reset()
                        continue
        finally:
            self._handle_research_genre_t_update(completed_commission_count)

        if reward:
            self._record_commission_income()

        return reward

    def _commission_recovery_store(self):
        """Получить прикладное cache-состояние для текущего профиля."""

        return CommissionRecoveryStore.from_environment()

    def _recover_commission_oil_overflow(self) -> CommissionRecoveryOutcome:
        """Выполнить одну Redis-first попытку восстановления переполнения нефти."""

        profile = self.config.config_name
        store = self._commission_recovery_store()
        ap_handler = None
        os_opened = False
        ap_opened = False
        purchase_started = False

        def blocked(
            outcome: CommissionRecoveryOutcome = CommissionRecoveryOutcome.BLOCKED,
        ) -> CommissionRecoveryOutcome:
            self._commission_recovery_blocked = True
            return outcome

        def valid_state(state: object) -> bool:
            remaining = getattr(state, 'remaining', None)
            return (
                getattr(state, 'status', None) == 'confirmed'
                and isinstance(remaining, int)
                and not isinstance(remaining, bool)
                and 0 <= remaining <= MAX_WEEKLY_ACTION_POINT_PURCHASES
            )

        def valid_ap(value: object) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        def close_navigation() -> bool:
            nonlocal ap_opened, os_opened
            cleanup_ok = True
            if ap_opened and ap_handler is not None:
                try:
                    ap_handler.action_point_quit(timeout=10)
                except Exception as error:  # noqa: BLE001 — очистка завершается fail-closed
                    cleanup_ok = False
                    logger.warning(
                        '[Комиссия — нефть] Не удалось закрыть окно AP (%s)',
                        type(error).__name__,
                    )
                ap_opened = False
            if os_opened:
                try:
                    self.ui_ensure(page_reward)
                except Exception as error:  # noqa: BLE001 — очистка завершается fail-closed
                    cleanup_ok = False
                    logger.warning(
                        '[Комиссия — нефть] Возврат к наградам после восстановления AP не подтверждён (%s)',
                        type(error).__name__,
                    )
                os_opened = False
            return cleanup_ok

        def invalidate_after_mutation() -> CommissionRecoveryOutcome:
            try:
                invalidated = store.invalidate(
                    profile,
                    last_result='ambiguous_ap_purchase',
                )
            except Exception as error:  # noqa: BLE001 — повтор запрещён без инвалидации
                logger.warning(
                    '[Комиссия — нефть] Не удалось инвалидировать состояние после AP mutation (%s)',
                    type(error).__name__,
                )
                return blocked(CommissionRecoveryOutcome.AMBIGUOUS_MUTATION)
            if getattr(invalidated, 'status', None) != 'unknown':
                logger.warning(
                    '[Комиссия — нефть] После AP mutation состояние осталось неустановленным: статус=%s',
                    getattr(invalidated, 'status', 'unknown'),
                )
            return blocked(CommissionRecoveryOutcome.AMBIGUOUS_MUTATION)

        def run_dorm(state: object) -> CommissionRecoveryOutcome:
            if not valid_state(state) or state.remaining != 0:
                return blocked()
            if not close_navigation():
                return blocked()
            try:
                logger.info('[Комиссия — нефть] Тратим нефть через подтверждённый резерв общежития')
                RewardDorm(self.config, self.device).dorm_food_run(amount=10)
                stored = store.record_result(profile, 'dorm_fallback')
                logger.info(
                    '[Комиссия — нефть] Резерв общежития завершён; состояние кэша=%s',
                    getattr(stored, 'status', 'unknown'),
                )
                self.ui_ensure(page_reward)
            except Exception as error:  # noqa: BLE001 — после mutation нет скрытого повтора
                logger.warning(
                    '[Комиссия — нефть] Резерв общежития не подтверждён (%s)',
                    type(error).__name__,
                )
                return blocked()
            return CommissionRecoveryOutcome.DORM_RECOVERED

        try:
            if getattr(self, '_commission_recovery_blocked', False):
                return blocked()

            try:
                state = store.read(profile)
            except Exception as error:  # noqa: BLE001 — сбой кэша не может выбрать mutation
                logger.warning(
                    '[Комиссия — нефть] Каноническое состояние AP недоступно (%s)',
                    type(error).__name__,
                )
                return blocked()
            logger.info(
                '[Комиссия — нефть] Каноническое состояние недельной покупки AP: статус=%s, остаток=%s, источник=%s',
                getattr(state, 'status', 'unknown'),
                getattr(state, 'remaining', None),
                getattr(state, 'source', None) or '-',
            )

            state_status = getattr(state, 'status', None)
            if state_status == 'unavailable':
                return blocked()
            if state_status == 'confirmed':
                if not valid_state(state):
                    return blocked()
                if state.remaining == 0:
                    return run_dorm(state)
                if getattr(self, '_commission_emergency_purchase_attempted', False):
                    logger.warning(
                        '[Комиссия — нефть] Аварийная покупка AP уже использована; при remaining>0 Dorm запрещён',
                    )
                    return blocked()
            elif state_status != 'unknown':
                return blocked()

            if getattr(self, '_commission_emergency_purchase_attempted', False):
                return blocked()

            expected_remaining = state.remaining if valid_state(state) else None
            ap_handler = ActionPointHandler(self.config, self.device)
            self.ui_ensure(page_os)
            os_opened = True
            if not ap_handler.action_point_enter(timeout=15):
                logger.warning('[Комиссия — нефть] Не удалось безопасно открыть окно AP')
                return blocked()
            ap_opened = True

            self._commission_emergency_purchase_attempted = True
            purchase_started = True
            purchase = ap_handler.action_point_buy_emergency_once(
                expected_remaining=expected_remaining,
            )
            if (
                purchase.status is EmergencyActionPointPurchaseStatus.PURCHASED
                and purchase.click_count == 1
                and isinstance(purchase.remaining_before, int)
                and not isinstance(purchase.remaining_before, bool)
                and 1 <= purchase.remaining_before <= MAX_WEEKLY_ACTION_POINT_PURCHASES
                and (expected_remaining is None or purchase.remaining_before == expected_remaining)
                and purchase.remaining_after == purchase.remaining_before - 1
                and valid_ap(purchase.ap_before)
                and valid_ap(purchase.ap_after)
                and valid_ap(purchase.ap_gain)
                and purchase.ap_after == purchase.ap_before + ACTION_POINT_GAIN_PER_PURCHASE
                and purchase.ap_gain == purchase.ap_after - purchase.ap_before
                and valid_ap(purchase.oil_cost)
                and valid_ap(purchase.oil_before)
                and valid_ap(purchase.oil_after)
                and purchase.oil_after == purchase.oil_before - purchase.oil_cost
            ):
                from module.dev_runtime.hooks import record_product_evidence

                record_product_evidence(
                    self.config.config_name,
                    "commission_ap_purchase",
                    {
                        "status": purchase.status.value,
                        "click_count": purchase.click_count,
                        "remaining_before": purchase.remaining_before,
                        "remaining_after": purchase.remaining_after,
                        "ap_before": purchase.ap_before,
                        "ap_after": purchase.ap_after,
                        "ap_gain": purchase.ap_gain,
                        "oil_before": purchase.oil_before,
                        "oil_after": purchase.oil_after,
                        "oil_cost": purchase.oil_cost,
                    },
                    task="Commission",
                )
                stored = store.record_observation(
                    profile,
                    purchase.remaining_after,
                    source='emergency_ap_purchase',
                    last_result='ap_purchase',
                )
                if (
                    valid_state(stored)
                    and stored.remaining == purchase.remaining_after
                    and stored.source == 'emergency_ap_purchase'
                    and stored.last_result == 'ap_purchase'
                ):
                    logger.info(
                        '[Комиссия — нефть] Покупка AP подтверждена typed result: weekly %s -> %s, AP %s -> %s (+%s), Oil %s -> %s',
                        purchase.remaining_before,
                        stored.remaining,
                        purchase.ap_before,
                        purchase.ap_after,
                        purchase.ap_gain,
                        purchase.oil_before,
                        purchase.oil_after,
                    )
                    return CommissionRecoveryOutcome.AP_RECOVERED
                return invalidate_after_mutation()

            logger.warning(
                '[Комиссия — нефть] Аварийная покупка AP не подтверждена: результат=%s, кликов=%s',
                getattr(purchase.status, 'value', purchase.status),
                getattr(purchase, 'click_count', 0),
            )
            if (
                getattr(purchase, 'click_count', 0) > 0
                or purchase.status is EmergencyActionPointPurchaseStatus.PURCHASED
            ):
                return invalidate_after_mutation()

            observed_remaining = purchase.remaining_before
            if (
                isinstance(observed_remaining, int)
                and not isinstance(observed_remaining, bool)
                and 0 <= observed_remaining <= MAX_WEEKLY_ACTION_POINT_PURCHASES
            ):
                observed = store.record_observation(
                    profile,
                    observed_remaining,
                    source='game_ocr',
                    last_result='ap_unavailable' if observed_remaining == 0 else None,
                )
                if not valid_state(observed) or observed.remaining != observed_remaining:
                    return blocked()
                if observed_remaining == 0:
                    return run_dorm(observed)
            return blocked()
        except Exception as error:  # noqa: BLE001 — восстановление ограничено и завершается fail-closed
            logger.warning(
                '[Комиссия — нефть] Восстановление AP завершилось исключением (%s)',
                type(error).__name__,
            )
            if purchase_started:
                return invalidate_after_mutation()
            return blocked()
        finally:
            close_navigation()
            store.close()

    def commission_receive(self):
        """
        Получает награды за комиссии с автоматической обработкой переполнения нефти.

        Returns:
            bool: Были ли получены награды.

        Pages:
            in: page_reward
            out: page_commission
        """
        self._commission_emergency_purchase_attempted = False
        self._commission_recovery_blocked = False
        for _ in range(3):
            try:
                return self._commission_receive()
            except OilMaxed:
                outcome = self._recover_commission_oil_overflow()
                self.ui_ensure(page_reward)
                if outcome not in {
                    CommissionRecoveryOutcome.AP_RECOVERED,
                    CommissionRecoveryOutcome.DORM_RECOVERED,
                }:
                    raise RequestHumanTakeover

        logger.critical('[Комиссия — нефть] Не удалось устранить переполнение нефти после 3 ограниченных попыток')
        raise RequestHumanTakeover

    def run(self):
        """
        Pages:
            in: Any
            out: page_commission
        """
        # Исправление: если застряли на TACTICAL_CLASS_START (выбор учебника), нажимаем отмену для выхода
        # TACTICAL_CHECK ложно срабатывает в TACTICAL_CLASS_START, из-за чего навигация A*
        # выбирает BACK_ARROW, но с этой страницы нельзя перейти на page_reward
        self.device.screenshot()
        if self.appear(TACTICAL_CLASS_START, offset=(30, 30)):
            logger.info('[Комиссия — тактика] Обнаружена кнопка начала тактического обучения; нажимаем отмену для выхода')
            self.device.click(TACTICAL_CLASS_CANCEL)
            self.device.sleep((0.5, 1.0))
        self.ui_ensure(page_reward)
        self.commission_receive()

        # При получении корабля в поручении церемонии отплытия появляется информационная панель
        # Это баг игры: панель циклически показывает получение корабля, пока не будет нажат get_ship
        self.handle_info_bar()
        self.commission_start()

        # Планировщик
        total = self.daily.add_by_eq(self.urgent)
        future_finish = sorted([f for f in total.get('finish_time') if f is not None])
        logger.info(f'[Комиссия — завершение] Время завершения комиссий: {[str(f) for f in future_finish]}')
        if len(future_finish):
            self.config.task_delay(target=future_finish)
        else:
            logger.info('[Комиссия — завершение] Нет выполняющихся комиссий')
            self.config.task_delay(success=False)

        # Откладываем задачи фарма гемов / 3-oil low cost
        # Проверяем задачи из группы GemsFarming: активны ли они и включен ли CommissionLimit
        limit_tasks = [
            task for task in ['GemsFarming', 'ThreeOilLowCost']
            if self.config.is_task_enabled(task)
            and self.config.cross_get(keys=f'{task}.GemsFarming.CommissionLimit', default=False)
        ]

        if limit_tasks:
            daily = self.daily.select(category_str='daily', status='pending').count
            filtered_urgent = self.comm_choose.intersect_by_eq(self.urgent.select(status='pending')).count
            filtered_extra = self.comm_choose.intersect_by_eq(self.daily.select(category_str='extra', status='pending')).count
            logger.info(f'[Комиссия — планировщик] Ежедневные: {daily}, отфильтровано срочных: {filtered_urgent}, дополнительных: {filtered_extra}')
            future = nearest_future(future_finish) if len(future_finish) else None
            if daily > 0 and filtered_urgent >= 1:
                for task in limit_tasks:
                    logger.info(f"[Комиссия — планировщик] Есть ожидающая ежедневная комиссия; задача '{task}' отложена")
                    self.config.task_delay(minute=None if future else 120, target=future, task=task)
            elif filtered_urgent >= 4:
                for task in limit_tasks:
                    logger.info(f"[Комиссия — планировщик] Слишком много срочных комиссий; задача '{task}' отложена")
                    self.config.task_delay(minute=None if future else 120, target=future, task=task)
