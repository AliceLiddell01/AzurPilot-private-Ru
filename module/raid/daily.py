"""
Модуль ежедневных заданий рейда (Daily Raid).

Отвечает за последовательное прохождение ежедневных попыток рейда на указанных сложностях (easy, normal, hard).
Поддерживает следующие возможности:
- Выбор сложностей для прохождения через фильтр StageFilter
- Автоматическое определение оставшихся попыток и циклический запуск боёв
- Сложность EX всегда выполняется последней с предварительным сбором наград за зачистку
- Для RPG-рейдов ежедневный режим отсутствует, планировщик отключается автоматически
"""
import re

from module.base.filter import Filter
from module.logger import logger
from module.raid.run import RaidRun
from module.reward.reward import Reward
from module.ui.page import page_raid


class RaidStage:
    """
    Класс данных этапа сложности рейда.

    Используется для представления опции сложности рейда в фильтре StageFilter.

    Attributes:
        name (str): Название сложности ('easy', 'normal', 'hard').
    """

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


STAGES = ['easy', 'normal', 'hard']
STAGE_FILTER = Filter(regex=re.compile('(\w+)'), attr=['name'])


class RaidDaily(RaidRun):
    """
    Исполнитель ежедневных заданий рейда.

    Последовательно выполняет ежедневные задания рейда на выбранных сложностях. Последовательность:
    1. Проверка типа рейда (для RPG ежедневный режим отсутствует, задача отключается)
    2. Фильтрация нужных сложностей через StageFilter (по умолчанию easy > normal > hard)
    3. Последовательное исчерпание 15 ежедневных попыток на каждой сложности
    4. Если настроена сложность EX, предварительно забираются награды за зачистку, затем выполняется EX

    Наследует RaidRun, используя его логику проведения боёв и проверки условий остановки.
    """
    def run(self, name=''):
        """
        Запуск ежедневных заданий рейда с последовательным исчерпанием попыток на выбранных сложностях.

        Args:
            name (str): Название рейдового события, например 'raid_20200624'.
        """
        if self.is_raid_rpg():
            logger.info('[Рейд — ежедневный] У RPG-рейда нет ежедневного задания')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        name = name if name else self.config.Campaign_Event
        stages = [RaidStage(name) for name in STAGES]
        STAGE_FILTER.load(self.config.RaidDaily_StageFilter)
        stages = STAGE_FILTER.apply(stages)

        self.ui_ensure(page_raid)

        for stage in stages:
            mode = stage.name
            logger.hr(mode, level=1)
            for _ in range(15):
                remain = self.get_remain(mode=mode)
                if remain <= 0:
                    break
                super().run(name=name, mode=mode, total=1)

        # Если настроена сложность EX, всегда выполняем её последней, поэтому не используем фильтрацию этапов
        stages = [stage.lower().strip()\
            for stage in\
            self.config.RaidDaily_StageFilter.split('>')]
        if 'ex' in stages:
            # Забираем награды в виде рейдовых билетов за 5 и 10 прохождений любой сложности
            self.ui_goto_main()
            Reward(self.config, self.device).reward_mission(
                   daily=self.config.Reward_CollectMission,
                   weekly=False)
            self.ui_ensure(page_raid)

            logger.hr('EX', level=1)
            super().run(name=name, mode='ex', total=self.get_remain('ex'))

        self.config.task_delay(server_update=True)
