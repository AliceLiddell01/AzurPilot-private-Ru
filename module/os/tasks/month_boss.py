"""
月度Boss任务模块。

挑战并击败大世界月度Boss，支持普通和困难两种难度。
战斗前检查适应性数值以选择合适的难度，战斗后在港口修理舰队。
每月重置时自动延迟到新的Boss出现周期。

Classes:
    OpsiMonthBoss: 月度Boss处理器，继承 OSMap。
"""

import numpy as np

from module.config.utils import get_os_next_reset
from module.logger import logger
from module.os.map import OSMap
from module.os_handler.action_point import OCR_OS_ADAPTABILITY
from module.os_handler.assets import OS_MONTHBOSS_NORMAL, OS_MONTHBOSS_HARD


class OpsiMonthBoss(OSMap):
    def get_adaptability(self):
        adaptability = OCR_OS_ADAPTABILITY.ocr(self.device.image)

        return adaptability

    def clear_month_boss(self):
        """
        清理月度Boss。

        检查适应性、判断当前 Boss 难度、击败 Boss 并在港口修理舰队。

        Raises:
            ActionPointLimit: 行动力不足。
            TaskEnd: 没有更多月度Boss。

        Pages:
            in: page_os, 大世界任务界面
            out: page_os, 大世界地图
        """
        if self.is_in_opsi_explore():
            logger.info('Выполняется «Ежемесячное исследование+», задача ежемесячного босса остановлена')
            self.config.task_delay(server_update=True)
            self.config.task_stop()

        logger.hr("Операция «Сирена» — ежемесячный босс", level=1)
        logger.hr("Предварительная проверка ежемесячного босса", level=2)
        checkout_offset = self.os_mission_enter(
            skip_siren_mission=self.config.cross_get('OpsiDaily.OpsiDaily.SkipSirenResearchMission'))
        logger.attr('Режим OpsiMonthBoss', self.config.OpsiMonthBoss_Mode)
        if self.appear(OS_MONTHBOSS_NORMAL, offset=checkout_offset):
            logger.attr('Сложность ежемесячного босса', 'normal')
            is_normal = True
        elif self.appear(OS_MONTHBOSS_HARD, offset=checkout_offset):
            logger.attr('Сложность ежемесячного босса', 'hard')
            is_normal = False
        else:
            logger.info("Ежемесячный босс обычной или высокой сложности не найден, задача остановлена")
            self.os_mission_quit()
            self.month_boss_delay(is_normal=False, result=False)
            return True
        self.os_mission_quit()

        if not is_normal and self.config.OpsiMonthBoss_Mode == "normal":
            logger.info("Настроен бой только с обычным ежемесячным боссом, но доступен босс высокой сложности; пропуск")
            self.month_boss_delay(is_normal=False, result=True)
            self.config.task_stop()
            return True

        if self.config.OpsiMonthBoss_CheckAdaptability:
            self.os_map_goto_globe(unpin=False)
            adaptability = self.get_adaptability()
            if (np.array(adaptability) < (203, 203, 156)).any():
                logger.info("[Операция «Сирена» — ежемесячный босс] Адаптивность ниже уровня подавления, сначала необходимо усилить флот")
                self.config.task_delay(server_update=True)
                self.config.task_stop()
            # 无需退出，复用当前状态

        # 战斗
        logger.hr("Переход к ежемесячному боссу", level=2)
        with self.config.temporary(_disable_task_switch=True):
            self.globe_goto(154)
            self.go_month_boss_room(is_normal=is_normal)
            result = self.boss_clear(has_fleet_step=True, is_month=True)

            # 战斗结束
            logger.hr("Ремонт перед ежемесячным боссом", level=2)
            self.handle_fleet_repair_by_config(revert=False)
            self.handle_fleet_resolve(revert=False)
            self.month_boss_delay(is_normal=is_normal, result=result)

    def month_boss_delay(self, is_normal=True, result=True):
        """
        月度Boss任务延迟逻辑。

        根据难度和清理结果决定延迟到下次重置还是稍后重试。

        Args:
            is_normal (bool): True 为普通难度，False 为困难难度。
            result (bool): 是否成功击败 Boss。
        """
        if is_normal:
            if result:
                if self.config.OpsiMonthBoss_Mode == 'normal_hard':
                    logger.info('Обычный ежемесячный босс побеждён, далее — босс высокой сложности')
                    self.config.task_stop()
                else:
                    logger.info('Обычный ежемесячный босс побеждён, задача остановлена')
                    next_reset = get_os_next_reset()
                    self.config.task_delay(target=next_reset)
                    self.config.task_stop()
            else:
                logger.info("Не удалось победить обычного ежемесячного босса, повторная попытка позже")
                self.config.opsi_task_delay(recon_scan=False, submarine_call=True, ap_limit=False)
                self.config.task_stop()
        else:
            if result:
                logger.info('Ежемесячный босс высокой сложности побеждён, задача остановлена')
                next_reset = get_os_next_reset()
                self.config.task_delay(target=next_reset)
                self.config.task_stop()
            else:
                logger.info("Не удалось победить ежемесячного босса высокой сложности, повторная попытка завтра")
                self.config.task_delay(server_update=True)
                self.config.task_stop()
