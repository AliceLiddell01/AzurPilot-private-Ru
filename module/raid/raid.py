# Этот файл обрабатывает различные временные совместные (Raid) этапы игры.
# Отвечает за автоматическое определение типа события, расход пропусков, вход на разных сложностях, специализированный бой Raid и учёт полученных PT.
"""
Основной модуль обработки событий рейдов (Raid).

Обрабатывает различные временные совместные этапы игры, включая:
- Сопоставление названия события рейда с префиксом ресурсов (raid_name_shorten)
- Фабричные функции кнопок входа для различных сложностей и OCR-распознавателей (raid_entrance, raid_ocr, pt_ocr)
- Полный цикл подготовки к бою рейда, входа, выполнения и завершения
- Обработку диалогового окна подтверждения использования билетов рейда
- OCR-считывание очков PT и проверку условий остановки

Поддерживаемые события рейдов: ESSEX, SURUGA, BRISTOL, IRIS, ALBION, KUYBYSHEY,
GORIZIA, HUANCHANG, RPG, CHIENWU, CHANGWU.
"""
import cv2
import numpy as np

import module.config.server as server
from module.base.timer import Timer
from module.campaign.campaign_event import CampaignEvent
from module.combat.assets import *
from module.exception import ScriptError
from module.logger import logger
from module.map.map_operation import MapOperation
from module.ocr.ocr import Digit, DigitCounter
from module.raid.assets import *
from module.raid.combat import RaidCombat
from module.ui.assets import RAID_CHECK
from module.ui.page import page_rpg_stage, page_campaign_menu
from module.log_res import LogRes


class RaidCounterPostMixin(DigitCounter):
    """
    Примесь постобработки счётчика рейдов.

    Выполняет постобработку результатов распознавания OCR, исправляя
    ошибочные распознавания вида "915/", "1515" и приводя их к корректному формату "X/15".
    Используется для новых событий рейдов, таких как CHANGWU.
    """

    def after_process(self, result):
        # Исправляем ошибки OCR вроде "915/" и "1515"
        result = result.strip('/')
        if result.isdigit() and len(result) > 2 and result.endswith('15'):
            result = f'{result[:-2]}/15'
        return result


class RaidCounter(DigitCounter):
    """
    OCR-распознаватель счётчика рейдов.

    На этапе предварительной обработки добавляет белые поля (padding) сверху и снизу изображения,
    чтобы повысить точность распознавания цифр и разделителей.
    Используется для старых событий рейдов (ESSEX, SURUGA, BRISTOL).
    """

    def pre_process(self, image):
        image = super().pre_process(image)
        image = np.pad(image, ((2, 2), (0, 0)), mode='constant', constant_values=255)
        return image


class HuanChangCounter(Digit):
    """
    Оставшиеся попытки в рейде Huan Chang ("Весеннее волнение") отображаются вертикально,
    поэтому OCR распознаёт только верхнюю часть цифр.
    """

    def ocr(self, image, direct_ocr=False):
        result = super().ocr(image, direct_ocr)
        return (result, 0, 15)


class HuanChangPtOcr(Digit):
    """
    OCR-распознаватель очков PT для рейда Huan Chang.

    Использует анализ связных компонент для фильтрации нецифровых областей,
    сохраняя только компоненты с площадью более 60 пикселей как валидные цифры,
    чтобы справиться со специфическими помехами фона в событии Huan Chang.
    """
    def pre_process(self, image):
        """
        Предварительная обработка изображения PT: градация серого, бинаризация,
        анализ связных компонент и отсечение нецифровых областей.

        Args:
            image (np.ndarray): Входное изображение размерностью (height, width, channel).

        Returns:
            np.ndarray: Обработанное бинарное изображение размерностью (height, width).
        """
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        image = cv2.threshold(image, 128, 255, cv2.THRESH_BINARY_INV)[1]
        count, cc = cv2.connectedComponents(image)
        # Вычисляем площадь связных компонент; компоненты площадью больше 60 считаем цифрами
        # На фоне CN/JP крайняя правая область связана, а на EN — нет, поэтому исключаем и [0,-1], и [-1,-1]
        num_idx = [i for i in range(1, count + 1) if
                   i != cc[0, -1] and i != cc[-1, -1] and np.count_nonzero(cc == i) > 60]
        image = ~(np.isin(cc, num_idx) * 255)  # Цифры белые, поэтому инвертируем
        return image.astype(np.uint8)


def raid_name_shorten(name):
    """
    Преобразует название события рейда в префикс имени ресурса кнопки.

    Args:
        name (str): Название события рейда, например raid_20200624, raid_20210708.

    Returns:
        str: Префикс имени ресурса кнопки, например ESSEX, SURUGA.
    """
    if name == 'raid_20200624':
        return 'ESSEX'
    elif name == 'raid_20210708':
        return 'SURUGA'
    elif name == 'raid_20220127':
        return 'BRISTOL'
    elif name == 'raid_20220630':
        return 'IRIS'
    elif name == "raid_20221027":
        return "ALBION"
    elif name == "raid_20230118":
        return "KUYBYSHEY"
    elif name == "raid_20230629":
        return "GORIZIA"
    elif name == "raid_20240130":
        return "HUANCHANG"
    elif name == "raid_20240328":
        return "RPG"
    elif name == 'raid_20250116':
        return 'CHIENWU'
    elif name == 'raid_20260212':
        return 'CHANGWU'
    else:
        raise ScriptError(f'Неизвестное имя рейда: {name}')


def raid_entrance(raid, mode):
    """
    Получает ресурс кнопки входа в соответствии с названием события рейда и сложностью.

    Args:
        raid (str): Название события рейда, например raid_20200624, raid_20210708.
        mode (str): Режим сложности: easy, normal или hard.

    Returns:
        Button: Кнопка входа соответствующей сложности.
    """
    key = f'{raid_name_shorten(raid)}_RAID_{mode.upper()}'
    try:
        return globals()[key]
    except KeyError:
        raise ScriptError(f'Ресурс входа в рейд не существует: {key}')


def raid_ocr(raid, mode):
    """
    Получает экземпляр OCR-распознавателя в соответствии с названием события рейда и сложностью.

    Args:
        raid (str): Название события рейда, например raid_20200624, raid_20210708.
        mode (str): Режим сложности: easy, normal, hard или ex.

    Returns:
        DigitCounter: Соответствующий OCR-распознаватель (DigitCounter или Digit).
    """
    raid = raid_name_shorten(raid)
    key = f'{raid}_OCR_REMAIN_{mode.upper()}'
    try:
        button = globals()[key]
    except KeyError:
        raise ScriptError(f'Ресурс входа в рейд не существует: {key}')
    # Старые рейды используют RaidCounter для совместимости со старыми моделями OCR и ресурсами
    # Новые рейды используют DigitCounter
    if raid == 'ESSEX':
        return RaidCounter(button, letter=(57, 52, 255), threshold=128)
    elif raid == 'SURUGA':
        return RaidCounter(button, letter=(49, 48, 49), threshold=128)
    elif raid == 'BRISTOL':
        return RaidCounter(button, letter=(214, 231, 219), threshold=128)
    elif raid == 'IRIS':
        # Этого шрифта нет в модели azur_lane, поэтому используем универсальную модель OCR
        if server.server == 'en':
            # На EN-сервере используется жирный шрифт
            return RaidCounter(button, letter=(148, 138, 123), threshold=80, lang='azur_lane')
        if server.server == 'jp':
            return RaidCounter(button, letter=(148, 138, 123), threshold=128, lang='azur_lane')
        else:
            return DigitCounter(button, letter=(148, 138, 123), threshold=128, lang='azur_lane')
    elif raid == "ALBION":
        return DigitCounter(button, letter=(99, 73, 57), threshold=128)
    elif raid == 'KUYBYSHEY':
        if mode == 'ex':
            return Digit(button, letter=(189, 203, 214), threshold=128)
        else:
            return DigitCounter(button, letter=(231, 239, 247), threshold=128)
    elif raid == 'GORIZIA':
        if mode == 'ex':
            return Digit(button, letter=(198, 223, 140), threshold=128)
        else:
            return DigitCounter(button, letter=(82, 89, 66), threshold=128)
    elif raid == "HUANCHANG":
        if mode == 'ex':
            return Digit(button, letter=(255, 255, 255), threshold=180)
        else:
            # Счётчик расположен вертикально
            return HuanChangCounter(button, letter=(255, 255, 255), threshold=80)
    elif raid == 'CHIENWU':
        if mode == 'ex':
            return Digit(button, letter=(247, 223, 222), threshold=128)
        else:
            return DigitCounter(button, letter=(0, 0, 0), threshold=128)
    elif raid == 'CHANGWU':
        if mode == 'ex':
            return Digit(button, letter=(255, 239, 215), threshold=128)
        else:
            return RaidCounterPostMixin(button, lang='azur_lane', letter=(154, 148, 133), threshold=128)


def pt_ocr(raid):
    """
    Получает экземпляр OCR-распознавателя очков PT в соответствии с названием события рейда.

    Args:
        raid (str): Название события рейда, например raid_20200624, raid_20210708.

    Returns:
        Digit: OCR-распознаватель очков PT либо None, если не поддерживается.
    """
    raid = raid_name_shorten(raid)
    key = f'{raid}_OCR_PT'
    try:
        button = globals()[key]
    except KeyError:
        return None
    if raid == 'IRIS':
        return Digit(button, letter=(181, 178, 165), threshold=128)
    elif raid == "ALBION":
        return Digit(button, letter=(23, 20, 9), threshold=128)
    elif raid == 'KUYBYSHEY':
        return Digit(button, letter=(16, 24, 33), threshold=64)
    elif raid == 'GORIZIA':
        return Digit(button, letter=(255, 255, 255), threshold=64)
    elif raid == "HUANCHANG":
        return HuanChangPtOcr(button, letter=(23, 20, 6), threshold=128)
    elif raid == 'CHIENWU':
        return Digit(button, letter=(255, 231, 231), threshold=128)
    elif raid == 'CHANGWU':
        return Digit(button, letter=(255, 239, 215), threshold=128)


class Raid(MapOperation, RaidCombat, CampaignEvent):
    """
    Основной обработчик событий рейдов.

    Наследует MapOperation, RaidCombat и CampaignEvent, обеспечивая полный боевой цикл
    событий рейда: вход, подготовка к бою, проведение боя и обработка экрана завершения.

    Основные обязанности:
    - Проверка условий остановки (топливо, очки PT, монеты, балансировщик задач)
    - Обработка экрана подготовки к бою (автоматизация, отставка, настроение, использование билетов)
    - Навигация и вход на этап рейда
    - Проведение боя рейда (обычный режим и EX-режим)
    - Считывание очков PT через OCR и их регистрация
    - Специальная обработка для рейдов RPG-типа (прокрутка до крайнего правого этапа)

    Attributes:
        _raid_has_oil_icon: Отображается ли значок топлива в интерфейсе текущего рейда (property, по умолчанию False).
    """
    @property
    def _raid_has_oil_icon(self):
        """
        Определяет, отображается ли значок топлива в интерфейсе текущего рейда.
        В большинстве событий рейдов отображение топлива удалено, см. https://github.com/LmeSzinc/AzurLaneAutoScript/issues/5214
        """
        return False

    def triggered_stop_condition(self, oil_check=False, pt_check=False, coin_check=False):
        """
        Проверяет, сработало ли условие остановки: лимит топлива, очков PT события, монет или балансировщика задач.

        Returns:
            bool: Сработало ли условие остановки.
        """
        # Лимит топлива
        if oil_check:
            if self.get_oil() < max(500, self.config.StopCondition_OilLimit):
                logger.hr('Условие остановки: лимит топлива')
                self.config.task_delay(minute=(120, 240))
                return True
        # Лимит очков события
        if pt_check:
            if self.event_pt_limit_triggered():
                logger.hr('Условие остановки: лимит PT события')
                return True
        # Лимит монет
        if coin_check and self.coin_limit_triggered():
            logger.hr('Условие остановки: лимит монет')
            return True
        # Балансировщик задач
        if coin_check:
            if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
                logger.hr('Условие остановки: лимит монет')
                self.handle_task_balancer()
                return True

        return False

    def combat_preparation(self, balance_hp=False, emotion_reduce=False, auto='combat_auto', fleet_index=1):
        """
        Обрабатывает экран подготовки к бою рейда, включая настройки автобоя, отставку, проверку настроения и использование билетов.

        Args:
            balance_hp (bool): Выполнять ли балансировку здоровья кораблей.
            emotion_reduce (bool): Уменьшать ли показатель настроения.
            auto (str): Режим автобоя.
            fleet_index (int): Индекс флота.
        """
        logger.info('Подготовка к бою')

        # Здесь не нужно ждать восстановления настроения: это уже обрабатывается в raid_execute_once()

        checked = False
        for _ in self.loop():
            if self.appear(BATTLE_PREPARATION, offset=(30, 20)):
                if self.handle_combat_automation_set(auto=auto == 'combat_auto'):
                    continue
                if not checked and self._raid_has_oil_icon:
                    checked = True
                    if self.triggered_stop_condition(oil_check=True, coin_check=True):
                        self.config.task_stop()
            if self.handle_raid_ticket_use():
                continue
            if self.handle_retirement():
                continue
            if self.handle_combat_low_emotion():
                continue
            if self.appear_then_click(BATTLE_PREPARATION, offset=(30, 20), interval=2):
                continue
            if self.handle_combat_automation_confirm():
                continue
            if self.handle_story_skip():
                continue

            # Условие завершения: бой начал выполняться
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Боевой интерфейс', pause)
                if emotion_reduce:
                    self.emotion.reduce(fleet_index)
                break

    def handle_raid_ticket_use(self):
        """
        Обрабатывает диалоговое окно подтверждения использования билета рейда в зависимости от настроек.

        Returns:
            bool: Была ли нажата кнопка.
        """
        if self.appear(TICKET_USE_CONFIRM, offset=(30, 30), interval=1):
            if self.config.Raid_UseTicket:
                self.device.click(TICKET_USE_CONFIRM)
            else:
                self.device.click(TICKET_USE_CANCEL)
            return True

        return False

    def raid_enter(self, mode, raid, skip_first_screenshot=True):
        """
        Осуществляет вход на указанный этап рейда, выполняя переход со страницы рейда к экрану подготовки к бою.

        Args:
            mode (str): Режим сложности: easy, normal или hard.
            raid (str): Название события рейда.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Pages:
            in: page_raid
            out: BATTLE_PREPARATION
        """
        entrance = raid_entrance(raid=raid, mode=mode)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(entrance, offset=(10, 10), interval=5):
                # При появлении входа проверяем лимит PT
                if self.triggered_stop_condition(pt_check=True):
                    self.config.task_stop()
                self.device.click(entrance)
                continue
            if self.appear_then_click(RAID_FLEET_PREPARATION, offset=(20, 20), interval=5):
                continue

            # Условие завершения: появился экран боя
            if self.combat_appear():
                break

    def raid_expected_end(self):
        """
        Определяет, завершился ли бой рейда.

        Проверяет появление окна наград RAID_REWARDS или возврат на страницу рейда.
        Для RPG-типа рейдов проверяет page_rpg_stage, для остальных — RAID_CHECK.

        Returns:
            bool: Завершился ли бой.
        """
        if self.appear_then_click(RAID_REWARDS, offset=(30, 30), interval=3):
            return False
        if self.is_raid_rpg():
            return self.appear(page_rpg_stage.check_button, offset=(30, 30))
        else:
            return self.appear(RAID_CHECK, offset=(30, 30))

    def raid_execute_once(self, mode, raid):
        """
        Выполняет один бой рейда от входа на этап до завершения боя.

        Args:
            mode (str): Режим сложности.
            raid (str): Название события рейда.

        Pages:
            in: page_raid
            out: page_raid
        """
        logger.hr('Запуск рейда')
        self.config.override(
            Campaign_Name=f'{raid}_{mode}',
            Campaign_UseAutoSearch=False,
            Fleet_FleetOrder='fleet1_all_fleet2_standby'
        )

        if mode == 'ex':
            backup = self.config.temporary(
                Submarine_Fleet=1,
                Submarine_Mode='every_combat'
            )

        self.emotion.check_reduce(1)

        self.raid_enter(mode=mode, raid=raid)
        self.combat(balance_hp=False, expected_end=self.raid_expected_end)

        if mode == 'ex':
            backup.recover()

        logger.hr('Рейд завершён')

    def raid_execute_once_with_oil_check(self, mode, raid):
        """
        Выполняет один бой рейда с предварительной проверкой запасов топлива перед входом.
        Используется для таких событий, как raid_20240328, где требуется заранее получить значение топлива во избежание проблем с интерфейсом.

        Args:
            mode (str): Режим сложности.
            raid (str): Название события рейда.

        Pages:
            in: page_raid
            out: page_raid
        """
        logger.hr('Запуск рейда')
        self.config.override(
            Campaign_Name=f'{raid}_{mode}',
            Campaign_UseAutoSearch=False,
            Fleet_FleetOrder='fleet1_all_fleet2_standby'
        )

        if mode == 'ex':
            backup = self.config.temporary(
                Submarine_Fleet=1,
                Submarine_Mode='every_combat'
            )

        self.emotion.check_reduce(1)

        if self.is_raid_rpg():
            logger.info('RPG-рейд: получение нефти перед входом в бой')
            self.ui_ensure(page_campaign_menu)
            CampaignEvent.get_oil(self, skip_first_screenshot=True, update=False)
            self.ui_ensure(page_rpg_stage)
            self.raid_rpg_swipe()

        self.raid_enter(mode=mode, raid=raid)
        self.combat(balance_hp=False, expected_end=self.raid_expected_end)

        if mode == 'ex':
            backup.recover()

        logger.hr('Рейд завершён')

    def get_event_pt(self):
        """
        Считывает текущее количество очков PT события рейда через OCR.

        Returns:
            int: Очки PT рейда либо 0, если OCR для данного события не поддерживается.

        Pages:
            in: page_raid
        """
        skip_first_screenshot = True
        timeout = Timer(1.5, count=5).start()
        ocr = pt_ocr(self.config.Campaign_Event)
        if ocr is not None:
            # 70000 может быть начальным значением по умолчанию; ждём, пока OCR считает фактическое значение
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()

                pt = ocr.ocr(self.device.image)
                if timeout.reached():
                    logger.warning('Тайм-аут ожидания PT; считаем, что значение достигнуто')
                    LogRes(self.config).Pt = pt
                    return pt
                if pt in [70000, 70001]:
                    continue
                else:
                    LogRes(self.config).Pt = pt
                    return pt
        else:
            logger.info(f'[Рейд — PT] Рейд {self.config.Campaign_Event} не поддерживает OCR PT; пропуск')
            return 0

    def is_raid_rpg(self):
        """
        Определяет, относится ли текущее событие рейда к типу RPG.

        Рейды RPG-типа (raid_20240328) имеют иной макет интерфейса и логику входа,
        требуя специальной обработки (жесты свайпа, иные детекторы страниц и т.д.).

        Returns:
            bool: Является ли текущий рейд типом RPG.
        """
        return self.config.Campaign_Event == 'raid_20240328'

    def raid_rpg_swipe(self, skip_first_screenshot=True):
        """
        Выполняет жест прокрутки (свайп) к крайнему правому входу на этап в рейдах RPG-типа.

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.
        """
        interval = Timer(1)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения: список прокручен до крайнего правого положения
            if self.appear(RPG_RAID_EASY, offset=(10, 10)):
                logger.info('RPG-рейд уже находится в крайнем правом положении')
                break

            if self.handle_story_skip():
                continue
            if self.handle_get_items():
                continue
            if interval.reached():
                self.device.swipe_vector((-900, 0), box=(0, 130, 1280, 440))
                interval.reset()
                continue
