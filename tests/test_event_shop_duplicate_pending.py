from types import SimpleNamespace

import pytest

import module.webui.event_shop_priority as priority
from module.webui.event_shop_priority import (
    confirm_event_shop_purchase,
    load_event_shop_priority,
    prepare_event_shop_runtime_items,
    set_event_shop_priority,
)


class FakeTaskStop(Exception):
    pass


class FakeConfig:
    config_name = "duplicate-pending"
    SERVER = "EN"

    def __init__(self):
        self.overrides = {}
        self.task_calls = []

    def override(self, **kwargs):
        self.overrides.update(kwargs)

    def task_call(self, task, force_call=True):
        self.task_calls.append((task, force_call))
        return True

    @staticmethod
    def task_stop(message=""):
        raise FakeTaskStop(message)


def runtime_item(*, remaining):
    return SimpleNamespace(
        group="Cat",
        sub_genre="T2",
        tier="",
        price=500,
        total_count=5,
        count=remaining,
        cost="pt",
        amount=1,
    )


def event_spec():
    return {
        "id": "event-smoke",
        "server": "EN",
        "currencies": [{"id": 1, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 4139,
                "event_shop_filter": "CatT2",
                "price": 500,
                "stock": 5,
                "currency_id": 1,
                "amount": 1,
            }
        ],
    }


def test_consistent_overlap_duplicate_confirms_pending_purchase(monkeypatch, tmp_path):
    spec = event_spec()
    config = FakeConfig()
    target = {"4139": 1}
    cleared = []

    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    monkeypatch.setattr(
        priority,
        "_selected_targets",
        lambda _config, event_id: dict(target),
    )

    def clear_target(_config, event_id, row_id, expected_selected):
        assert event_id == "event-smoke"
        assert row_id == "4139"
        assert expected_selected == 1
        if target[row_id] != expected_selected:
            return False
        target[row_id] = 0
        cleared.append(row_id)
        return True

    monkeypatch.setattr(priority, "_clear_selected_target", clear_target)

    set_event_shop_priority(
        config.config_name,
        spec["id"],
        4139,
        0,
        root=tmp_path,
    )

    before = runtime_item(remaining=5)
    prepared = prepare_event_shop_runtime_items(
        config,
        [before],
        root=tmp_path,
    )
    assert list(prepared) == [before]
    assert config.overrides["EventShop_CustomFilter"] == "CatT2:1"

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            before,
            full_purchase=False,
            remaining_after=4,
            root=tmp_path,
        )

    first = runtime_item(remaining=4)
    duplicate = runtime_item(remaining=4)
    prepared = prepare_event_shop_runtime_items(
        config,
        [first, duplicate],
        root=tmp_path,
    )
    state = load_event_shop_priority(
        config.config_name,
        spec["id"],
        root=tmp_path,
    )

    assert list(prepared) == []
    assert prepared.observation_items == [first, duplicate]
    assert target["4139"] == 0
    assert cleared == ["4139"]
    assert "4139" not in state["priorities"]
    assert state["completed"] == ["4139"]
    assert state["purchased"] == []
    assert state["remaining"]["4139"] == 4
    assert state["pending"] == {}
    assert state["blocked"] == {}
    assert config.overrides["EventShop_CustomFilter"] == ""
