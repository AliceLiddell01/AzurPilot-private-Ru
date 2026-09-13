from types import SimpleNamespace

import pytest

import module.webui.event_shop_priority as priority
from module.webui.app_event_shop_v2 import EventShopV2Mixin
from module.webui.event_shop_priority import (
    confirm_event_shop_purchase,
    load_event_shop_priority,
    prepare_event_shop_runtime_items,
    set_event_shop_priority,
)


class FakeTaskStop(Exception):
    pass


class FakeConfig:
    config_name = "target-baseline"
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


def event_spec():
    return {
        "id": "event-target-baseline",
        "server": "EN",
        "currencies": [{"id": 1, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 11,
                "event_shop_filter": "Chip",
                "price": 15,
                "stock": 100,
                "currency_id": 1,
                "amount": 1,
            }
        ],
    }


def runtime_item(remaining):
    return SimpleNamespace(
        group="Chip",
        sub_genre="",
        tier="",
        price=15,
        total_count=100,
        count=remaining,
        cost="pt",
        amount=1,
    )


def patch_targets(monkeypatch, targets):
    spec = event_spec()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    monkeypatch.setattr(
        priority,
        "_selected_targets",
        lambda _config, event_id: dict(targets)
        if event_id == spec["id"]
        else {},
    )

    def clear_target(_config, event_id, row_id, expected_selected):
        assert event_id == spec["id"]
        if targets.get(row_id) != expected_selected:
            return False
        targets[row_id] = 0
        return True

    monkeypatch.setattr(priority, "_clear_selected_target", clear_target)
    return spec


def test_repeated_goal_starts_from_current_remaining(monkeypatch, tmp_path):
    targets = {"11": 10}
    spec = patch_targets(monkeypatch, targets)
    config = FakeConfig()

    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    before = runtime_item(100)
    prepared = prepare_event_shop_runtime_items(config, [before], root=tmp_path)

    assert list(prepared) == [before]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:10"
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)
    assert state["target_baselines"]["11"] == 100

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            before,
            full_purchase=False,
            remaining_after=90,
            root=tmp_path,
        )

    current = runtime_item(90)
    prepared = prepare_event_shop_runtime_items(config, [current], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert targets["11"] == 0
    assert "11" not in state["target_baselines"]
    assert "11" not in state["priorities"]

    targets["11"] = 10
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    prepared = prepare_event_shop_runtime_items(config, [current], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [current]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:10"
    assert state["target_baselines"]["11"] == 90

    item = {"id": "11", "stock": 100, "selected": 10, "remaining": 90}
    assert EventShopV2Mixin._event_shop_target_remaining(item, state) == 10


def test_increasing_active_goal_buys_only_difference(monkeypatch, tmp_path):
    targets = {"11": 10}
    spec = patch_targets(monkeypatch, targets)
    config = FakeConfig()

    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    before = runtime_item(100)
    prepare_event_shop_runtime_items(config, [before], root=tmp_path)

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            before,
            full_purchase=False,
            remaining_after=95,
            root=tmp_path,
        )

    current = runtime_item(95)
    prepared = prepare_event_shop_runtime_items(config, [current], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [current]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:5"
    assert state["target_baselines"]["11"] == 100

    targets["11"] = 15
    prepared = prepare_event_shop_runtime_items(config, [current], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [current]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:10"
    assert state["target_baselines"]["11"] == 100
