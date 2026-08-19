"""活动 SP 关卡执行模块。

执行活动的 SP（Special）关卡，每日限定 1 次。
SP 关卡通常需要完成所有前置关卡后才可解锁，难度较高但奖励丰厚。

流程简单：检查 sp.py 地图文件是否存在 → 执行 1 次 → 延迟到次日。
如果活动没有 SP 关卡或已执行过，自动延迟到次日服务器刷新。

配置路径: Campaign.Name (战役名称)
"""

from module.config.config import TaskEnd
from module.event.base import EventBase
from module.exception import RequestHumanTakeover
from module.logger import logger


class CampaignSP(EventBase):
    """活动 SP 关卡的执行器。

    执行单个 SP 关卡（每日限定 1 次），执行完毕或无法进入时延迟到次日服务器刷新。

    Pages:
        in: page_event
        out: page_event
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
