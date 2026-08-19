from module.webui.app_event_shop_live import EventShopLiveMixin


def test_event_shop_live_fingerprint_tracks_target_baseline():
    user_state = {
        "source_event_id": "event-test",
        "shop_selections": {"11": 10},
    }
    priority_state = {
        "priorities": {"11": 0},
        "purchased": [],
        "completed": [],
        "remaining": {"11": 90},
        "target_baselines": {},
        "blocked": {},
        "pending": {},
    }

    before = EventShopLiveMixin._event_shop_live_plan_fingerprint(
        user_state,
        priority_state,
    )
    priority_state["target_baselines"] = {"11": 90}
    after = EventShopLiveMixin._event_shop_live_plan_fingerprint(
        user_state,
        priority_state,
    )
    repeated = EventShopLiveMixin._event_shop_live_plan_fingerprint(
        user_state,
        priority_state,
    )

    assert after != before
    assert repeated == after
