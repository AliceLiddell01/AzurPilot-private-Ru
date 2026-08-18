from module.webui.app_event_planner import EventPlannerMixin
from module.webui.app_event_shop_v2 import EventShopV2Mixin


def test_event_shop_plan_dom_patcher_is_owned_only_by_v2():
    assert "_patch_event_shop_plan_values" not in EventPlannerMixin.__dict__
    assert "_patch_event_shop_plan_values" in EventShopV2Mixin.__dict__
