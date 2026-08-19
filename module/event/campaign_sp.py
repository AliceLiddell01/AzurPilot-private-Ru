"""Ежедневное выполнение специального SP-этапа события.

SP обычно доступен один раз за серверный день. Наличие этапа определяется через
общий каталог EventBase: current generated-событие использует verified artifact,
а историческое событие — физический legacy campaign-каталог.

После успешного выполнения или недоступности SP задача откладывается до следующего
серверного дня.
"""

from module.config.config import TaskEnd
from module.event.base import EventBase
from module.exception import RequestHumanTakeover
from module.logger import logger


class CampaignSP(EventBase):
    """Исполнитель ежедневного SP-этапа текущего события.

    Страницы:
        вход: page_event
        выход: page_event
    """

    def run(self, *args, **kwargs):
        """Выполнить ежедневный SP текущего события один раз."""

        stages = self.convert_stages(self.available_stages())
        if 'sp' not in {str(stage) for stage in stages}:
            logger.info('[Событие — SP] Проверенный этап SP отсутствует в текущем событии')
            logger.info('В этом событии нет доступного SP; пропуск')
            self.config.Scheduler_Enable = False
            self.config.task_stop()

        try:
            super().run(name=self.config.Campaign_Name, folder=self.config.Campaign_Event, total=1)
        except TaskEnd:
            # Переключение задачи считается штатным завершением.
            pass
        except RequestHumanTakeover:
            # Ежедневный SP уже завершён или недоступен; ждём следующий серверный день.
            logger.info('Ежедневный SP уже завершён или недоступен')
            logger.info('Задача отложена до завтра')
            self.config.task_delay(server_update=True)
            return

        # Выбираем дальнейшее расписание по факту завершения запуска.
        if self.run_count > 0:
            logger.info(f'Завершено, run_count={self.run_count}')
            self.config.task_delay(server_update=True)
        else:
            logger.info('Выполнение не удалось; возможно, SP уже завершён сегодня')
            logger.info('Задача отложена до завтра')
            self.config.task_delay(server_update=True)
