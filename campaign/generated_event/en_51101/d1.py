from module.campaign.campaign_base import CampaignBase
from module.map.map_base import CampaignMap

MAP = CampaignMap('D1')
MAP.shape = 'I7'
MAP.camera_data = ['D2', 'D5', 'F2', 'F5']
MAP.camera_data_spawn_point = ['F2', 'D2']
MAP.map_data = """
    ME ++ -- SP -- SP -- ++ ++
    -- -- -- -- -- -- -- ME ++
    -- ME ++ -- MS -- Me -- Me
    -- -- Me MS -- MS -- -- ++
    ++ -- -- ++ __ ++ ME -- ME
    ME -- Me ++ -- ++ -- ME --
    -- ++ ++ ++ MB ++ ++ ++ --
"""
MAP.weight_data = """
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
    50 50 50 50 50 50 50 50 50
"""
MAP.spawn_data = [{'battle': 0, 'enemy': 2, 'siren': 2}, {'battle': 1, 'enemy': 1}, {'battle': 2, 'enemy': 2}, {'battle': 3, 'enemy': 1}, {'battle': 4, 'enemy': 2}, {'battle': 5, 'enemy': 1, 'boss': 1}]

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
    STAR_REQUIRE_1 = 1
    STAR_REQUIRE_2 = 2
    STAR_REQUIRE_3 = 3
    MOVABLE_ENEMY_TURN = (2,)
    # Проверенные runtime-факты из ограниченной policy generated package.
    MAP_SIREN_TEMPLATE = ['BonhommeRichard_BB', 'BonhommeRichard_CV']
    INTERNAL_LINES_FIND_PEAKS_PARAMETERS = {'height': (80.0, 238.0), 'prominence': 10.0, 'distance': 35.0, 'width': (0.9, 10.0)}
    EDGE_LINES_FIND_PEAKS_PARAMETERS = {'height': (238.0, 255.0), 'prominence': 10.0, 'distance': 50.0, 'wlen': 1000.0}
    MAP_SWIPE_MULTIPLY = (1.136, 1.158)
    MAP_SWIPE_MULTIPLY_MINITOUCH = (1.099, 1.119)
    MAP_SWIPE_MULTIPLY_MAATOUCH = (1.067, 1.086)
    MAP_WALK_USE_CURRENT_FLEET = True

class Campaign(CampaignBase):
    MAP = MAP
    ENEMY_FILTER = '1L > 1M > 1E > 1C > 2L > 2M > 2E > 2C > 3L > 3M > 3E > 3C'

    def battle_0(self):
        if self.clear_siren():
            return True
        if self.clear_filter_enemy(self.ENEMY_FILTER, preserve=0):
            return True

        return self.battle_default()

    def battle_5(self):
        return self.fleet_boss.clear_boss()

