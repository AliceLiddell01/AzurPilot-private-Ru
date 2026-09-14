import pytest

from module.webui.event_shop_priority import (
    EVENT_SHOP_PRIORITY_SCHEMA_VERSION,
    event_shop_target_capacity,
    load_event_shop_priority,
    save_event_shop_priority,
    set_event_shop_priority,
    update_event_shop_target_state,
)


def _state(event_id="event-lifecycle"):
    return {
        "schema_version": EVENT_SHOP_PRIORITY_SCHEMA_VERSION,
        "event_id": event_id,
        "priorities": {},
        "purchased": [],
        "completed": [],
        "remaining": {},
        "target_baselines": {},
        "blocked": {},
        "pending": {},
    }


def test_baseline_follows_target_episode_not_priority(tmp_path):
    event_id = "event-lifecycle"
    state = _state(event_id)
    state["remaining"] = {"11": 95}
    state["completed"] = ["11"]
    save_event_shop_priority("instance", state, root=tmp_path)

    update_event_shop_target_state(
        "instance", event_id, 11, 0, 10, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert state["target_baselines"]["11"] == 95
    assert state["completed"] == []

    set_event_shop_priority("instance", event_id, 11, 0, root=tmp_path)
    update_event_shop_target_state(
        "instance", event_id, 11, 10, 15, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert state["target_baselines"]["11"] == 95

    set_event_shop_priority("instance", event_id, 11, None, root=tmp_path)
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert state["target_baselines"]["11"] == 95

    set_event_shop_priority("instance", event_id, 11, 2, root=tmp_path)
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert state["target_baselines"]["11"] == 95

    update_event_shop_target_state(
        "instance", event_id, 11, 15, 0, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert "11" not in state["target_baselines"]

    update_event_shop_target_state(
        "instance", event_id, 11, 0, 10, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert state["target_baselines"]["11"] == 95


def test_priority_edit_does_not_migrate_active_legacy_goal(tmp_path):
    event_id = "event-legacy"
    state = _state(event_id)
    state["priorities"] = {"11": 0}
    state["remaining"] = {"11": 95}
    save_event_shop_priority("instance", state, root=tmp_path)

    set_event_shop_priority("instance", event_id, 11, 1, root=tmp_path)
    state = load_event_shop_priority("instance", event_id, root=tmp_path)

    assert state["priorities"]["11"] == 1
    assert "11" not in state["target_baselines"]


def test_purchased_row_rejects_new_target_without_positive_proven_stock(tmp_path):
    event_id = "event-purchased"
    state = _state(event_id)
    state["purchased"] = ["11"]
    state["remaining"] = {"11": 0}
    save_event_shop_priority("instance", state, root=tmp_path)

    set_event_shop_priority("instance", event_id, 11, 0, root=tmp_path)
    with pytest.raises(ValueError, match="доступную ёмкость товара 0"):
        update_event_shop_target_state(
            "instance", event_id, 11, 0, 1, root=tmp_path
        )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)

    assert state["purchased"] == ["11"]
    assert "11" not in state["target_baselines"]


def test_new_target_capacity_uses_proven_remaining_and_backend_rechecks(tmp_path):
    event_id = "event-capacity"
    state = _state(event_id)
    state["remaining"] = {"11": 2}
    save_event_shop_priority("instance", state, root=tmp_path)

    item = {"id": "11", "stock": 5, "selected": 0}
    assert event_shop_target_capacity(item, state) == 2

    with pytest.raises(ValueError, match="доступную ёмкость товара 2"):
        update_event_shop_target_state(
            "instance", event_id, 11, 0, 5, root=tmp_path
        )

    saved = load_event_shop_priority("instance", event_id, root=tmp_path)
    assert saved["target_baselines"] == {}


def test_active_target_capacity_keeps_baseline_after_partial_purchase(tmp_path):
    event_id = "event-active-capacity"
    state = _state(event_id)
    state["remaining"] = {"11": 2}
    state["target_baselines"] = {"11": 5}
    save_event_shop_priority("instance", state, root=tmp_path)

    item = {"id": "11", "stock": 5, "selected": 5, "remaining": 2}
    assert event_shop_target_capacity(item, state) == 5

    saved = update_event_shop_target_state(
        "instance", event_id, 11, 5, 5, root=tmp_path
    )
    assert saved["target_baselines"]["11"] == 5


def test_pending_purchase_preserves_existing_baseline_during_target_edit(tmp_path):
    event_id = "event-pending"
    state = _state(event_id)
    state["priorities"] = {"11": 0}
    state["remaining"] = {"11": 100}
    state["target_baselines"] = {"11": 100}
    state["pending"] = {
        "row_id": "11",
        "before_remaining": 100,
        "expected_remaining": 95,
    }
    save_event_shop_priority("instance", state, root=tmp_path)

    update_event_shop_target_state(
        "instance", event_id, 11, 10, 0, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)

    assert state["target_baselines"]["11"] == 100
    assert state["pending"]["expected_remaining"] == 95
