"""Ежедневное выполнение обычных этапов события.

Этапы сортируются пользовательским EventDaily.StageFilter и выполняются по одному
разу. Прогресс сохраняется в EventDaily.LastStage, поэтому задача может продолжить
работу после прерывания без повторного прохождения уже завершённых этапов.

Список этапов предоставляет EventBase: текущее generated-событие использует
verified-каталог artifact, историческое событие — legacy campaign-каталог.
После завершения выбранных этапов задача откладывается до следующего серверного дня.
"""

from module.config.config import TaskEnd
from module.config.utils import get_server_last_update
from module.event.base import STAGE_FILTER, EventBase
from module.exception import ScriptEnd, RequestHumanTakeover
from module.logger import logger


class CampaignABCD(EventBase):
    """Исполнитель ежедневных A/B/C/D и других не-SP этапов события.

    Этапы выполняются в порядке фильтра с восстановлением позиции после прерывания.

    Страницы:
        вход: page_event
        выход: page_event
    """

    def run(self, *args, **kwargs):
        """Выполнить ежедневные этапы события по настроенному фильтру."""

        stages = self.available_stages()
        stages = self.convert_stages(stages)
        logger.attr('Этапы', [str(stage) for stage in stages])
        logger.attr('Фильтр этапов', self.config.EventDaily_StageFilter)
        STAGE_FILTER.load(self.config.EventDaily_StageFilter)
        self.convert_stages(STAGE_FILTER)
        stages = [str(stage) for stage in STAGE_FILTER.apply(stages)]
        logger.attr('Порядок фильтрации', ' > '.join(stages))

        # После фильтрации нет доступных этапов: отключаем задачу.
        if not stages:
            logger.warning('Нет этапов, соответствующих текущему фильтру')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        # Продолжаем после последнего успешно выполненного этапа.
        logger.info(f'[Событие — этап] Предыдущий этап {self.config.EventDaily_LastStage}, записан в {self.config.Scheduler_NextRun}')
        if get_server_last_update(self.config.Scheduler_ServerUpdate) >= self.config.Scheduler_NextRun:
            logger.info('[Событие — этап] Запись предыдущего этапа устарела; сброс')
            self.config.EventDaily_LastStage = 0
        else:
            last = str(self.config.EventDaily_LastStage).lower()
            last = self.convert_stages(last)
            if last in stages:
                stages = stages[stages.index(last) + 1:]
                logger.attr('Порядок фильтрации', ' > '.join(stages))
            else:
                logger.info('Начинаем с начала')

        # Выполняем каждый выбранный этап по одному разу.
        for stage in stages:
            stage = str(stage)
            try:
                super().run(name=stage, folder=self.config.Campaign_Event, total=1)
            except TaskEnd:
                # Переключение задачи считается штатным завершением текущего этапа.
                pass
            except ScriptEnd as e:
                # Ошибка имени этапа из CampaignUI.ensure_campaign_ui().
                if str(e) == 'Campaign name error':
                    task = self.config.task.command
                    logger.critical(
                        f'Не удалось найти этап "{stage}". '
                        f'Задача "{task}" предназначена для ежедневного получения тройного PT; если этап {stage} ещё не разблокирован, '
                        f'используйте задачу "Event" для его разблокировки вместо задачи "{task}"')
                    raise RequestHumanTakeover
                else:
                    raise

            # После успешного этапа сохраняем позицию для продолжения.
            if self.run_count > 0:
                with self.config.multi_set():
                    self.config.EventDaily_LastStage = stage
                    self.config.task_delay(minute=0)
            else:
                self.config.task_stop()
            if self.config.task_switched():
                self.config.task_stop()

        # Все выбранные этапы завершены; ждём следующего серверного дня.
        self.config.task_delay(server_update=True)
