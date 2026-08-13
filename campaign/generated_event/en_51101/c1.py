from module.campaign.campaign_base import CampaignBase
from module.map.map_base import CampaignMap

MAP = CampaignMap('C1')
MAP.shape = 'I7'
MAP.camera_data = ['D2', 'D5', 'F2', 'F5']
MAP.camera_data_spawn_point = ['D2']
MAP.map_data = """
    ++ SP SP ++ ++ ++ ME -- ME
    ++ -- -- -- Me -- -- MB --
    Me -- -- __ -- -- -- -- ME
    -- MS MS -- ++ -- ++ ++ ++
    ME -- -- -- Me -- ME ++ --
    ++ ++ ME -- ++ -- -- ME --
    ++ ++ -- ME -- -- ME -- --
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
MAP.spawn_data = [{'battle': 0, 'enemy': 2, 'siren': 2}, {'battle': 1, 'enemy': 1}, {'battle': 2, 'enemy': 2}, {'battle': 3, 'enemy': 1}, {'battle': 4, 'enemy': 1, 'boss': 1}]

class Config:
    # Только факты карты; runtime policy задаётся отдельно.
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
    MAP_SIREN_TEMPLATE = ['emotion_qz']
    MOVABLE_ENEMY_TURN = (2,)

class Campaign(CampaignBase):
    MAP = MAP

    def battle_4(self):
        return self.clear_boss()

