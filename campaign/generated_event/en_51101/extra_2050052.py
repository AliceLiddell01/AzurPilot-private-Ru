from module.campaign.campaign_base import CampaignBase
from module.map.map_base import CampaignMap

MAP = CampaignMap('EXTRA')
MAP.shape = 'E7'
MAP.camera_data = ['C4']
MAP.camera_data_spawn_point = ['C4']
MAP.map_data = """
    -- -- ++ -- --
    -- ++ ++ ++ --
    -- ++ ++ ++ --
    ++ -- MB -- ++
    ++ -- -- -- ++
    ++ ++ SP ++ ++
    ++ ++ -- ++ ++
"""
MAP.weight_data = """
    50 50 50 50 50
    50 50 50 50 50
    50 50 50 50 50
    50 50 50 50 50
    50 50 50 50 50
    50 50 50 50 50
    50 50 50 50 50
"""
MAP.spawn_data = [{'battle': 0, 'boss': 1}]

class Config:
    # Только факты карты; runtime policy задаётся отдельно.
    MAP_HAS_MAP_STORY = False
    MAP_HAS_FLEET_STEP = False
    MAP_HAS_AMBUSH = False
    MAP_HAS_MYSTERY = False
    MAP_HAS_PORTAL = False
    MAP_HAS_LAND_BASED = False
    MAP_HAS_SIREN = False
    MAP_HAS_MOVABLE_ENEMY = False
    STAR_REQUIRE_1 = 0
    STAR_REQUIRE_2 = 0
    STAR_REQUIRE_3 = 0

class Campaign(CampaignBase):
    MAP = MAP

    def battle_0(self):
        return self.clear_boss()

