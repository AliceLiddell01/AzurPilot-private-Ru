from module.webui.event_shop_priority import (
    load_event_shop_priority,
    save_event_shop_priority,
    set_event_shop_priority,
    update_event_shop_target_state,
)


def _state(event_id="event-lifecycle"):
    return {
        "schema_version": 4,
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


def test_purchased_row_is_not_reopened_by_priority_or_new_target(tmp_path):
    event_id = "event-purchased"
    state = _state(event_id)
    state["purchased"] = ["11"]
    state["remaining"] = {"11": 0}
    save_event_shop_priority("instance", state, root=tmp_path)

    set_event_shop_priority("instance", event_id, 11, 0, root=tmp_path)
    update_event_shop_target_state(
        "instance", event_id, 11, 0, 1, root=tmp_path
    )
    state = load_event_shop_priority("instance", event_id, root=tmp_path)

    assert state["purchased"] == ["11"]
    assert "11" not in state["target_baselines"]


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
