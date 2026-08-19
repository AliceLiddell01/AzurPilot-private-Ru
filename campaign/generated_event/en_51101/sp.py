from module.campaign.campaign_base import CampaignBase
from module.map.map_base import CampaignMap

MAP = CampaignMap('SP')
MAP.shape = 'I9'
MAP.camera_data = ['D4', 'D6', 'F4', 'F6']
MAP.camera_data_spawn_point = ['D4', 'F4']
MAP.map_data = """
    ++ ++ ++ -- ++ -- ++ ++ ++
    -- ++ -- ++ ++ ++ -- ++ --
    -- ME -- -- ++ -- -- ME --
    ME -- -- SP -- SP -- -- ME
    -- ME -- -- __ -- -- ME --
    ++ -- ME -- MS -- ME -- ++
    ++ ME -- MS -- MS -- ME ++
    ++ ++ ME -- MB -- ME ++ ++
    ++ ++ -- ++ -- ++ -- ++ ++
"""
MAP.weight_data = """
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
"""
MAP.spawn_data = [{'battle': 0, 'enemy': 12, 'siren': 3}, {'battle': 1}, {'battle': 2}, {'battle': 3}, {'battle': 4}, {'battle': 5}, {'battle': 6}, {'battle': 7, 'boss': 1}]

class Config:
    # Только структурные факты карты из ShareCfg.
    MAP_HAS_MAP_STORY = False
    MAP_HAS_FLEET_STEP = True
    MAP_HAS_AMBUSH = False
    MAP_HAS_MYSTERY = False
    MAP_HAS_PORTAL = False
    MAP_HAS_LAND_BASED = False
    MAP_HAS_SIREN = True
    MAP_HAS_MOVABLE_ENEMY = True
    STAR_REQUIRE_1 = 0
    STAR_REQUIRE_2 = 0
    STAR_REQUIRE_3 = 0
    MOVABLE_ENEMY_TURN = (2,)
    # Проверенные runtime-факты из ограниченной policy generated package.
    MAP_SIREN_TEMPLATE = ['BonhommeRichard_SS']
    MAP_IS_ONE_TIME_STAGE = True
    MAP_HAS_MODE_SWITCH = False
    INTERNAL_LINES_FIND_PEAKS_PARAMETERS = {'height': (80.0, 238.0), 'prominence': 10.0, 'distance': 35.0, 'width': (0.9, 10.0)}
    EDGE_LINES_FIND_PEAKS_PARAMETERS = {'height': (238.0, 255.0), 'prominence': 10.0, 'distance': 50.0, 'wlen': 1000.0}
    MAP_SWIPE_MULTIPLY = (1.149, 1.17)
    MAP_SWIPE_MULTIPLY_MINITOUCH = (1.111, 1.132)
    MAP_SWIPE_MULTIPLY_MAATOUCH = (1.079, 1.098)
    MAP_ENSURE_EDGE_INSIGHT_CORNER = 'bottom'

class Campaign(CampaignBase):
    MAP = MAP
    ENEMY_FILTER = '1L > 1M > 1E > 1C > 2L > 2M > 2E > 2C > 3L > 3M > 3E > 3C'

    def battle_0(self):
        if self.clear_siren():
            return True
        if self.clear_filter_enemy(self.ENEMY_FILTER, preserve=2):
            return True

        return self.battle_default()

    def battle_5(self):
        if self.clear_siren():
            return True
        if self.clear_filter_enemy(self.ENEMY_FILTER, preserve=0):
            return True

        return self.battle_default()

    def battle_7(self):
        return self.fleet_boss.clear_boss()

