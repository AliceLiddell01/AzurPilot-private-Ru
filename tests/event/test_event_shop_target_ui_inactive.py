from module.webui.app_event_shop_v2 import EventShopV2Mixin


def test_inactive_new_goal_shows_full_requested_amount_after_old_purchases():
    item = {
        "id": "11",
        "stock": 100,
        "selected": 10,
        "remaining": 90,
    }
    state = {
        "priorities": {},
        "purchased": [],
        "remaining": {"11": 90},
        "target_baselines": {},
    }

    assert EventShopV2Mixin._event_shop_target_remaining(item, state) == 10


def test_active_legacy_goal_without_baseline_keeps_stock_based_fallback():
    item = {
        "id": "11",
        "stock": 100,
        "selected": 10,
        "remaining": 95,
    }
    state = {
        "priorities": {"11": 0},
        "purchased": [],
        "remaining": {"11": 95},
        "target_baselines": {},
    }

    assert EventShopV2Mixin._event_shop_target_remaining(item, state) == 5
