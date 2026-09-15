"""突袭任务运行器，管理突袭的进入、次数检测和停止条件。
支持 OCR 检测剩余次数和运行次数限制。
"""

from module.base.timer import Timer
from module.campaign.campaign_event import CampaignEvent
from module.exception import ScriptEnd, ScriptError
from module.logger import logger
from module.raid.assets import RAID_REWARDS
from module.raid.raid import Raid, raid_ocr
from module.ui.page import page_campaign_menu, page_raid, page_rpg_stage


class RaidRun(Raid, CampaignEvent):
    run_count: int
    run_limit: int

    def triggered_stop_condition(self, oil_check=False, pt_check=False, coin_check=False):
        """
        检查是否触发了停止条件，包括运行次数限制和父类条件。

        Returns:
            bool: 是否触发了停止条件。
        """
        # Ограничение числа запусков
        if self.run_limit and self.config.StopCondition_RunCount <= 0:
            logger.hr('Условие остановки: число запусков')
            self.config.StopCondition_RunCount = 0
            self.config.Scheduler_Enable = False
            return True

        return super().triggered_stop_condition(oil_check=oil_check, pt_check=pt_check, coin_check=coin_check)

    def get_remain(self, mode, skip_first_screenshot=True):
        """
        获取指定难度的剩余挑战次数。

        Args:
            mode (str): 难度模式，easy、normal、hard 或 ex。
            skip_first_screenshot (bool): 是否跳过首次截图。

        Returns:
            int: 剩余挑战次数。
        """
        confirm_timer = Timer(0.3, count=0)
        prev = 30
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            ocr = raid_ocr(raid=self.config.Campaign_Event, mode=mode)
            result = ocr.ocr(self.device.image)
            if mode == 'ex':
                remain = result
            else:
                remain, _, _ = result
            logger.attr(f'Осталось попыток ({mode.capitalize()})', remain)

            if self.appear_then_click(RAID_REWARDS, offset=(30, 30), interval=3):
                confirm_timer.reset()
                continue

            # Условие завершения: считаем чтение законченным, когда результат OCR стабилизировался
            if remain == prev:
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

            prev = remain

        return remain

    def run(self, name='', mode='', total=0):
        """
        运行突袭任务主循环，处理战斗执行、停止条件和调度器切换。

        Args:
            name (str): 突袭活动名称，如 'raid_20200624'。
            mode (str): 突袭难度，如 'hard'、'normal'、'easy'。
            total (int): 总运行次数，0 表示不限制。
        """
        name = name if name else self.config.Campaign_Event
        mode = mode if mode else self.config.Raid_Mode
        if not name or not mode:
            raise ScriptError(f'Не заполнены аргументы RaidRun. name={name}, mode={mode}')

        self.run_count = 0
        self.run_limit = self.config.StopCondition_RunCount
        while 1:
            # Завершаем после достижения заданного числа запусков
            if total and self.run_count == total:
                break
            if self.event_time_limit_triggered():
                self.config.task_stop()

            # Вывод в лог
            logger.hr(f'Рейд: {name}_{mode}', level=2)
            if self.config.StopCondition_RunCount > 0:
                logger.info(f'Осталось запусков: {self.config.StopCondition_RunCount}')
            else:
                logger.info(f'Счётчик: {self.run_count}')

            # Переход UI: если значка топлива нет, сначала открываем меню кампании и проверяем условия остановки
            if not self._raid_has_oil_icon:
                self.ui_ensure(page_campaign_menu)
                if self.triggered_stop_condition(oil_check=True, coin_check=True):
                    break

            # Убеждаемся, что открыта правильная страница UI
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            if not self.is_raid_rpg():
                self.ui_ensure(page_raid)
            else:
                self.ui_ensure(page_rpg_stage)
                self.raid_rpg_swipe()
            self.disable_event_on_raid()

            # Режим EX: проверяем, достаточно ли рейдовых билетов
            if mode == 'ex' and not self.is_raid_rpg():
                if not self.get_remain(mode):
                    logger.info('[Рейд — запуск] Сработало условие остановки: билеты рейда EX закончились')
                    if self.config.task.command == 'Raid':
                        with self.config.multi_set():
                            self.config.StopCondition_RunCount = 0
                            self.config.Scheduler_Enable = False
                    break

            # Выполняем рейдовый бой
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                self.raid_execute_once(mode=mode, raid=name)
            except ScriptEnd as e:
                logger.hr('Завершение скрипта')
                logger.info(str(e))
                break

            # После боя обновляем счётчики
            self.run_count += 1
            if self.config.StopCondition_RunCount:
                self.config.StopCondition_RunCount -= 1
            # Проверяем условия остановки
            if self.triggered_stop_condition():
                break
            # Проверяем, переключил ли планировщик задачу
            if self.config.task_switched():
                self.config.task_stop()
