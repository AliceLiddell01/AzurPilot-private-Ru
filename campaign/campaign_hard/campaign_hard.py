from module.base.timer import Timer
from module.campaign.campaign_base import CampaignBase
from module.exception import CampaignEnd
from module.logger import logger
from module.map.assets import FLEET_PREPARATION, MAP_PREPARATION
from module.ui.assets import CAMPAIGN_CHECK


class Config:
    MAP_HAS_AMBUSH = False
    ENABLE_EMOTION_REDUCE = False
    ENABLE_HP_BALANCE = False


class Campaign(CampaignBase):
    # def run(self):
    #     logger.hr(self.ENTRANCE, level=2)
    #     self.enter_map(self.ENTRANCE, mode='hard')
    #     self.map = self.MAP
    #     self.map.reset()
    #     self.hp_reset()
    #     self.hp_get()
    #
    #     if self.config.FLEET_HARD == 1:
    #         self.ensure_edge_insight(reverse=True)
    #         self.full_scan_find_boss()
    #     else:
    #         self.fleet_switch_click()
    #         self.ensure_no_info_bar()
    #         self.ensure_edge_insight()
    #         self.full_scan_find_boss()
    #
    #     try:
    #         self.clear_boss()
    #     except CampaignEnd:
    #         logger.hr('Campaign end')

    def _expected_end(self, expected):
        return 'in_stage'

    def clear_boss(self):
        grids = self.map.select(is_boss=True)
        grids = grids.add(self.map.select(may_boss=True, is_enemy=True))
        logger.info('Возможный босс: %s' % self.map.select(may_boss=True))
        logger.info('Возможный босс, распознанный как противник: %s' % self.map.select(may_boss=True, is_enemy=True))
        logger.info('Подтверждённые боссы: %s' % self.map.select(is_boss=True))
        # logger.info('Grids: %s' % grids)
        if grids:
            logger.hr('Устранение босса')
            grids = grids.sort('weight', 'cost')
            logger.info('Клетки: %s' % str(grids))
            self._goto(grids[0], expected='boss')
            raise CampaignEnd('Босс устранён.')

        logger.warning('Босс не обнаружен; проверяю все точки его появления.')
        self.clear_potential_boss()

        return False
