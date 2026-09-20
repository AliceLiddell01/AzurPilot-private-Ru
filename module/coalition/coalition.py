"""Модуль выполнения совместных операций (Coalition Event).

Автоматически выполняет бои совместных операций Azur Lane. Совместные операции — это
особое временное событие, обычно разделённое на несколько сложностей (Easy/Normal/Hard или TC1/TC2/TC3),
некоторые события также имеют этап SP.

Ключевые возможности модуля:
- Распознавание очков PT события: разные события используют индивидуальные стратегии OCR и параметры
- Проверка топлива: в интерфейсе некоторых событий отсутствует значок топлива, проверка пропускается
- Стандартизация названий этапов: совместимость с устаревшими именами сложностей TC-1/2/3
- Управление условиями остановки: лимит числа запусков, топливо, PT, монеты, балансировщик задач
- Управление настроением флота: в режиме одного флота принудительно предотвращается оранжевое настроение

Поддерживаемые события включают Frostfall, Academy, Date A Live (DAL),
Neon City, Fashion, Horror Stories и др.

Пути в конфигурации: Campaign.Event (имя события), Coalition.Mode (сложность этапа),
         Coalition.Fleet (режим флота).
"""

import re

from module.base.timer import Timer
from module.campaign.campaign_event import CampaignEvent
from module.coalition.assets import *
from module.coalition.combat import CoalitionCombat
from module.exception import ScriptEnd, ScriptError
from module.logger import logger
from module.ocr.ocr import Digit
from module.log_res.log_res import LogRes
from module.ui.assets import BACK_ARROW
from module.ui.page import page_campaign_menu


class AcademyPtOcr(Digit):
    """Специализированный OCR для очков PT события Academy, распознающий текст формата очков (например, 'Всего: 840')."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.alphabet += ':'

    def after_process(self, result):
        """Извлечение числовой части после двоеточия.

        Пример входных данных: 'Всего: 840' -> извлечение '840'.
        """
        logger.attr(self.name, result)
        try:
            result = result.rsplit(':')[1]
        except IndexError:
            pass
        return super().after_process(result)


class DALPtOcr(Digit):
    """Специализированный OCR для очков PT события DAL, распознающий текст вида 'X9100'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.alphabet += 'X'

    def after_process(self, result):
        """Извлечение числовой части после символа X.

        Пример входных данных: 'X9100' -> извлечение '9100'.
        """
        logger.attr(self.name, result)
        try:
            result = result.rsplit('X')[1]
        except IndexError:
            pass
        return super().after_process(result)


class Coalition(CoalitionCombat, CampaignEvent):
    """Исполнитель кампании совместных операций (Coalition Event).

    Наследует CoalitionCombat (боевая логика совместных операций) и CampaignEvent (база событий кампании),
    отвечает за полный цикл автоматизации совместной операции:
    1. Чтение из конфигурации названия события, сложности этапа и режима флота
    2. Стандартизация названия этапа (совместимость со старыми форматами конфигурации)
    3. Циклическое проведение боёв с проверкой условий остановки перед каждым боем
    4. Распознавание значения PT события для отслеживания прогресса
    5. Обработка особого интерфейса событий без значка топлива

    Attributes:
        run_count: Количество уже проведённых боёв.
        run_limit: Ограничение количества запусков из конфигурации.
    """

    run_count: int
    run_limit: int

    def get_event_pt(self):
        """Распознавание текущего количества очков PT события.

        В зависимости от текущего события выбирает соответствующий объект OCR и параметры,
        считывая значение PT со снимка экрана.
        Значение 999999 считается заполнителем по умолчанию, требующим ожидания обновления экрана.

        Returns:
            int: Количество очков PT; 0 при ошибке распознавания.
        """
        event = self.config.Campaign_Event
        if event == 'coalition_20230323':
            ocr = Digit(FROSTFALL_OCR_PT, name='OCR_PT', letter=(198, 158, 82), threshold=128)
        elif event == 'coalition_20240627':
            ocr = AcademyPtOcr(ACADEMY_PT_OCR, name='OCR_PT', letter=(255, 255, 255), threshold=128)
        elif event == 'coalition_20250626':
            # Используем универсальную модель OCR
            ocr = Digit(NEONCITY_PT_OCR, name='OCR_PT', lang='azur_lane', letter=(208, 208, 208), threshold=128)
        elif event == 'coalition_20251120':
            ocr = DALPtOcr(DAL_PT_OCR, name='OCR_PT', letter=(255, 213, 69), threshold=128)
        elif event == 'coalition_20260122':
            ocr = Digit(FASHION_PT_OCR, name='OCR_PT', letter=(41, 40, 40), threshold=128)
        elif event == 'coalition_20260723':
            ocr = Digit(HORROR_PT_OCR, name='OCR_PT', lang='azur_lane', letter=(228, 230, 237), threshold=256)
        else:
            logger.error(f'[Коалиция] Для события {event} не определён объект OCR')
            raise ScriptError

        pt = 0
        for _ in self.loop(timeout=1.5):
            pt = ocr.ocr(self.device.image)
            # 999999 — значение-заполнитель по умолчанию; ждём обновления экрана
            if pt not in [999999]:
                break
        else:
            logger.warning('Тайм-аут ожидания PT; считаем, что значение достигнуто')
        LogRes(self.config).Pt = pt
        self.config.update()
        return pt

    def check_oil(self):
        """Проверка, не опустился ли уровень топлива ниже установленного лимита.

        В интерфейсе некоторых событий значок топлива не отображается, в этом случае проверка пропускается.
        При первом обнаружении нехватки топлива ожидает стабилизации экрана для повторного подтверждения.

        Returns:
            bool: True, если топлива недостаточно; иначе False.
        """
        # Для коалиционных событий без значка топлива пропускаем проверку
        if not self._coalition_has_oil_icon:
            logger.info('В коалиционном событии нет значка топлива; проверка топлива пропущена')
            return False

        limit = max(500, self.config.StopCondition_OilLimit)
        if not (self.get_oil() < limit):
            return False

        # Ждём стабилизации значения OCR и затем проверяем ещё раз
        timeout = Timer(1, count=2).start()
        while True:
            self.device.screenshot()
            if self.appear(BACK_ARROW, offset=(5, 2)):
                break
            if timeout.reached():
                logger.warning('Считаем OCR_OIL стабильным')
                break
        if self.get_oil() < limit:
            return True
        else:
            return False

    @property
    def _coalition_has_oil_icon(self):
        """Отображается ли значок топлива в интерфейсе текущей совместной операции.

        В некоторых событиях значок топлива удалён разработчиками из соображений дизайна UI.
        См.: https://github.com/LmeSzinc/AzurLaneAutoScript/issues/5214
        """
        if self.config.Campaign_Event in [
            'coalition_20260122',
            'coalition_20260723',
            "coalition_20260723"
        ]:
            return False
        return True

    def triggered_stop_condition(self, oil_check=False, pt_check=False, coin_check=False):
        """Проверка выполнения условий остановки.

        Последовательно проверяет: лимит числа запусков, нехватку топлива, лимит PT события,
        лимит монет, балансировщик задач.

        Args:
            oil_check: Проверять ли лимит топлива.
            pt_check: Проверять ли лимит PT события.
            coin_check: Проверять ли лимит монет.

        Returns:
            bool: True, если сработало хотя бы одно условие остановки.
        """
        # Лимит числа запусков
        if self.run_limit and self.config.StopCondition_RunCount <= 0:
            logger.hr('Условие остановки: число запусков')
            self.config.StopCondition_RunCount = 0
            self.config.Scheduler_Enable = False
            return True
        # Лимит топлива
        if oil_check:
            # Проверяем наличие ui_current, чтобы избежать ошибки атрибута
            ui_is_campaign_menu = hasattr(self, 'ui_current') and self.ui_current == page_campaign_menu
            if (self._coalition_has_oil_icon or ui_is_campaign_menu) and self.check_oil():
                logger.hr('Условие остановки: лимит топлива')
                self.config.task_delay(minute=(120, 240))
                return True
        # Лимит PT события
        if pt_check:
            if self.event_pt_limit_triggered():
                logger.hr('Условие остановки: лимит PT события')
                return True
        # Лимит монет
        if coin_check and self.coin_limit_triggered():
            logger.hr('Условие остановки: лимит монет')
            return True
        # Балансировщик задач
        if self.run_count >= 1:
            if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
                logger.hr('Условие остановки: лимит монет')
                self.handle_task_balancer()
                return True

        return False

    def coalition_execute_once(self, event, stage, fleet):
        """Выполнение одного боя совместной операции.

        Переопределяет настройки кампании, контролирует настроение флотов,
        проверяет условия остановки и входит в бой.
        Для этапа SP принудительно используется несколько флотов; в режиме одного флота
        контроль настроения не опускается ниже оранжевого (yellow_face).

        Args:
            event: Название события, например 'coalition_20230323'.
            stage: Название этапа, например 'a1', 'sp'.
            fleet: Режим флота, например 'single', 'multi'.

        Pages:
            in: in_coalition
            out: in_coalition
        """
        self.config.override(
            Campaign_Name=f'{event}_{stage}',
            Campaign_UseAutoSearch=False,
            Fleet_FleetOrder='fleet1_all_fleet2_standby',
        )
        if self.config.Coalition_Fleet == 'single' and self.config.Emotion_Fleet1Control == 'prevent_red_face':
            logger.warning('[Коалиция] В режиме одной коалиционной флотилии нельзя допускать мораль ниже 30; принудительно включён режим prevent_yellow_face')
            self.config.override(Emotion_Fleet1Control='prevent_yellow_face')
        if stage == 'sp':
            # Для этапа SP требуется несколько флотов
            self.config.override(
                Coalition_Fleet='multi',
            )
        try:
            self.emotion.check_reduce(battle=self.coalition_get_battles(event, stage))
        except ScriptEnd:
            self.coalition_map_exit(event)
            raise

        if self._coalition_has_oil_icon and self.triggered_stop_condition(oil_check=True, coin_check=True):
            self.coalition_map_exit(event)
            raise ScriptEnd

        self.enter_map(event=event, stage=stage, mode=fleet)
        self.coalition_combat()

    @staticmethod
    def handle_stage_name(event, stage):
        """Стандартизация названий события и этапа.

        Удаляет пробельные символы и приводит к нижнему регистру. Для события Frostfall
        используется внутренний номер TC; для остальных событий преобразует устаревшие названия TC-1/2/3.

        Args:
            event: Название события.
            stage: Название этапа.

        Returns:
            tuple: (event, stage) со стандартизированными названиями.
        """
        stage = re.sub('[ \t\n]', '', str(stage)).lower()
        if event == 'coalition_20230323':
            stage = stage.replace('-', '')
            stage = {
                'easy': 'tc1',
                'normal': 'tc2',
                'hard': 'tc3',
            }.get(stage, stage)
        else:
            converted = {
                'tc1': 'easy',
                'tc2': 'normal',
                'tc3': 'hard',
            }.get(stage.replace('-', ''), stage)
            if converted != stage:
                logger.warning(f'Преобразование устаревшего этапа коалиции: {stage} -> {converted}')
                stage = converted

        return event, stage

    def run(self, event='', mode='', fleet='', total=0):
        """Основной рабочий цикл совместной операции.

        Считывает из конфигурации параметры события, этапа и флота, циклически выполняет
        бои до срабатывания условий остановки.
        Для событий без отображения топлива предварительно переходит в меню кампании
        для проверки условий остановки перед входом на страницу события.

        Args:
            event: Название события; если пусто, считывается из конфигурации.
            mode: Название этапа; если пусто, считывается из конфигурации.
            fleet: Режим флота; если пусто, считывается из конфигурации.
            total: Общий лимит числа запусков, 0 — без ограничений.

        Raises:
            ScriptError: Если обязательные параметры не указаны.
            ScriptEnd: При срабатывании условий остановки или штатном завершении скрипта.
        """
        event = event if event else self.config.Campaign_Event
        mode = mode if mode else self.config.Coalition_Mode
        fleet = fleet if fleet else self.config.Coalition_Fleet
        if not event or not mode or not fleet:
            raise ScriptError(f'Не заполнены аргументы Coalition. name={event}, mode={mode}, fleet={fleet}')

        event, mode = self.handle_stage_name(event, mode)
        self.run_count = 0
        self.run_limit = self.config.StopCondition_RunCount
        while 1:
            # Достигнут общий лимит запусков
            if total and self.run_count == total:
                break
            if self.event_time_limit_triggered():
                self.config.task_stop()

            # Выводим в лог текущий этап и оставшееся число запусков
            logger.hr(f'Коалиция: {event}_{mode}', level=2)
            if self.config.StopCondition_RunCount > 0:
                logger.info(f'Осталось запусков: {self.config.StopCondition_RunCount}')
            else:
                logger.info(f'Счётчик: {self.run_count}')

            # Если значка топлива нет, сначала проверяем условия остановки в меню кампании
            if not self._coalition_has_oil_icon:
                self.ui_goto(page_campaign_menu)
                if self.triggered_stop_condition(oil_check=True, coin_check=True):
                    break
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            self.ui_goto_coalition()
            self.disable_event_on_raid()
            self.coalition_ensure_mode(event, 'battle')

            # Проверяем условия остановки по PT и монетам
            if self.triggered_stop_condition(pt_check=True, coin_check=True):
                break

            # Выполняем бой
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                self.coalition_execute_once(event=event, stage=mode, fleet=fleet)
            except ScriptEnd as e:
                logger.hr('Завершение скрипта')
                logger.info(str(e))
                break

            # После боя обновляем счётчики
            self.run_count += 1
            if self.config.StopCondition_RunCount:
                self.config.StopCondition_RunCount -= 1
            # Повторно проверяем условия остановки
            if self.triggered_stop_condition(pt_check=True, coin_check=True):
                break
            # Проверяем планировщик задач
            if self.config.task_switched():
                self.config.task_stop()


if __name__ == '__main__':
    self = Coalition('alas5', task='Coalition')
    self.device.screenshot()
    self.get_event_pt()
