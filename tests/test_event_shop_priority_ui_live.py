import json

import module.webui.app_event_shop_v2 as shop_v2
from module.webui.app_event_shop_v2 import EventShopV2Mixin


def test_target_remaining_uses_observed_purchases_against_user_goal():
    item = {
        "id": "a",
        "price": 100,
        "stock": 10,
        "selected": 8,
        "remaining": 4,
    }
    state = {
        "priorities": {"a": 0},
        "purchased": [],
        "remaining": {},
    }

    assert EventShopV2Mixin._event_shop_target_remaining(item, state) == 2


def test_priority_metrics_count_only_unfulfilled_quantity_targets():
    plan = {
        "shop_items": [
            {
                "id": "a",
                "price": 100,
                "stock": 10,
                "selected": 8,
                "remaining": 4,
            },
            {
                "id": "b",
                "price": 50,
                "stock": 20,
                "selected": 5,
                "remaining": None,
            },
            {
                "id": "c",
                "price": 999,
                "stock": 1,
                "selected": 1,
                "remaining": 0,
            },
            {
                "id": "d",
                "price": 500,
                "stock": 5,
                "selected": 0,
                "remaining": 5,
            },
        ]
    }
    state = {
        "priorities": {"a": 0, "b": 1, "c": 2, "d": 3},
        "purchased": ["c"],
        "remaining": {"b": 18},
    }

    metrics = EventShopV2Mixin._event_shop_priority_metrics(plan, state)

    # a: 8 target - 6 already bought = 2 => 200
    # b: 5 target - 2 already bought = 3 => 150
    assert metrics == {"count": 2, "cost": 350}


def test_valid_priority_change_patches_dom_without_rebuilding_catalog(monkeypatch):
    plan = {
        "event": {"id": "event-test"},
        "shop_items": [
            {
                "id": "row-1",
                "price": 300,
                "stock": 5,
                "selected": 3,
                "remaining": None,
            },
        ],
    }
    state = {
        "priorities": {},
        "purchased": [],
        "remaining": {},
        "blocked": {"row-1": "Требуется новый скан"},
    }
    scripts = []
    refreshes = []

    view = EventShopV2Mixin()
    view.alas_name = "test-instance"
    view._event_write_allowed = lambda: True
    view._event_plan = lambda: plan
    view._fmt = lambda value: f"{int(value):,}".replace(",", " ")
    view._refresh_event_plan_page = lambda: refreshes.append(True)

    def save_priority(instance, event_id, row_id, priority):
        assert instance == "test-instance"
        assert event_id == "event-test"
        assert row_id == "row-1"
        if priority is None:
            state["priorities"].pop(row_id, None)
        else:
            state["priorities"][row_id] = priority

    monkeypatch.setattr(shop_v2, "set_event_shop_priority", save_priority)
    monkeypatch.setattr(
        shop_v2,
        "load_event_shop_priority",
        lambda instance, event_id: state,
    )
    monkeypatch.setattr(shop_v2, "run_js", lambda script: scripts.append(script))

    view._event_shop_priority_changed("event-test", "row-1", "abc123", 0)

    assert refreshes == []
    assert len(scripts) == 1
    script = scripts[-1]
    assert "event-shop-v2-plan-count" in script
    assert "event-shop-v2-plan-cost" in script
    assert "event-shop-v2-warning-abc123" in script
    assert '"value": "1"' in script
    assert '"value": "900"' in script
    assert '"value": "Требуется новый скан"' in script

    view._event_shop_priority_changed("event-test", "row-1", "abc123", "")

    assert refreshes == []
    assert len(scripts) == 2
    assert '"value": "0"' in scripts[-1]
    assert '"value": ""' in scripts[-1]


def test_priority_dom_payload_is_json_serialized():
    payload = {"warning": "</script><script>alert(1)</script>"}
    serialized = json.dumps(payload, ensure_ascii=False)

    assert serialized.startswith("{")
    assert serialized.endswith("}")
