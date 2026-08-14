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
    config_name = "test-instance"
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


def runtime_item(
    *,
    group,
    price,
    stock,
    remaining=None,
    cost="pt",
    amount=1,
):
    return SimpleNamespace(
        group=group,
        sub_genre="",
        tier="",
        price=price,
        total_count=stock,
        count=stock if remaining is None else remaining,
        cost=cost,
        amount=amount,
    )


def base_spec():
    return {
        "id": "event-test",
        "currencies": [
            {"id": 1, "runtime_token": "pt"},
        ],
        "shop_items": [
            {
                "row_id": 11,
                "event_shop_filter": "Chip",
                "price": 300,
                "stock": 10,
                "currency_id": 1,
                "amount": 1,
            },
            {
                "row_id": 12,
                "event_shop_filter": "Oil",
                "price": 450,
                "stock": 5,
                "currency_id": 1,
                "amount": 1,
            },
        ],
    }


def test_priority_runtime_authorizes_only_one_item_per_complete_scan(monkeypatch, tmp_path):
    spec = base_spec()
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)

    set_event_shop_priority(config.config_name, spec["id"], 11, 2, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 12, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    oil = runtime_item(group="Oil", price=450, stock=5)
    prepared = prepare_event_shop_runtime_items(
        config,
        [chip, oil],
        root=tmp_path,
    )

    assert list(prepared) == [oil]
    assert prepared.observation_items == [chip, oil]
    assert config.overrides["EventShop_PriorityMode"] is True
    assert config.overrides["EventShop_PresetFilter"] == "custom"
    assert config.overrides["EventShop_CustomFilter"] == "Oil"
    assert config.overrides["EventShop_BuyURShip"] == 0
    assert config.overrides["EventShop_UnlockSSRShip"] is False


def test_duplicate_runtime_token_is_blocked_fail_closed(monkeypatch, tmp_path):
    spec = base_spec()
    spec["shop_items"] = [
        {
            "row_id": 21,
            "event_shop_filter": "Chip",
            "price": 300,
            "stock": 10,
            "currency_id": 1,
            "amount": 1,
        },
        {
            "row_id": 22,
            "event_shop_filter": "Chip",
            "price": 600,
            "stock": 4,
            "currency_id": 1,
            "amount": 1,
        },
    ]
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)

    set_event_shop_priority(config.config_name, spec["id"], 21, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 22, 1, root=tmp_path)

    prepared = prepare_event_shop_runtime_items(
        config,
        [
            runtime_item(group="Chip", price=300, stock=10),
            runtime_item(group="Chip", price=600, stock=4),
        ],
        root=tmp_path,
    )
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert set(state["blocked"]) == {"21", "22"}
    assert config.overrides["EventShop_CustomFilter"] == ""


def test_full_purchase_is_marked_only_after_fresh_scan(monkeypatch, tmp_path):
    spec = base_spec()
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepare_event_shop_runtime_items(config, [chip], root=tmp_path)

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            chip,
            full_purchase=True,
            remaining_after=0,
            root=tmp_path,
        )

    pending = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)
    assert pending["priorities"]["11"] == 0
    assert pending["purchased"] == []
    assert pending["pending"] == {
        "row_id": "11",
        "before_remaining": 10,
        "expected_remaining": 0,
    }
    assert config.task_calls == [("EventShop", True)]

    prepare_event_shop_runtime_items(config, [], root=tmp_path)
    verified = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert "11" not in verified["priorities"]
    assert verified["purchased"] == ["11"]
    assert verified["remaining"]["11"] == 0
    assert verified["pending"] == {}


def test_partial_purchase_keeps_priority_after_verified_rescan(monkeypatch, tmp_path):
    spec = base_spec()
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepare_event_shop_runtime_items(config, [chip], root=tmp_path)
    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            chip,
            full_purchase=False,
            remaining_after=6,
            root=tmp_path,
        )

    rescanned = runtime_item(group="Chip", price=300, stock=10, remaining=6)
    prepared = prepare_event_shop_runtime_items(config, [rescanned], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [rescanned]
    assert state["priorities"]["11"] == 0
    assert state["purchased"] == []
    assert state["remaining"]["11"] == 6
    assert state["pending"] == {}


def test_failed_post_purchase_verification_blocks_all_new_clicks(monkeypatch, tmp_path):
    spec = base_spec()
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 12, 1, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    oil = runtime_item(group="Oil", price=450, stock=5)
    prepare_event_shop_runtime_items(config, [chip, oil], root=tmp_path)
    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            chip,
            full_purchase=False,
            remaining_after=6,
            root=tmp_path,
        )

    prepared = prepare_event_shop_runtime_items(config, [chip, oil], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert state["pending"]["row_id"] == "11"
    assert "не подтвердил" in state["blocked"]["11"]
    assert config.overrides["EventShop_CustomFilter"] == ""


def test_event_rotation_does_not_reuse_old_priorities(tmp_path):
    set_event_shop_priority("instance", "old-event", 11, 0, root=tmp_path)

    state = load_event_shop_priority("instance", "new-event", root=tmp_path)

    assert state["event_id"] == "new-event"
    assert state["priorities"] == {}
    assert state["purchased"] == []
    assert state["pending"] == {}


def test_urpt_priority_is_blocked_until_safe_priority_runtime_support(monkeypatch, tmp_path):
    spec = {
        "id": "event-ur",
        "currencies": [{"id": 2, "runtime_token": "URpt"}],
        "shop_items": [
            {
                "row_id": 31,
                "event_shop_filter": "ShipUR",
                "price": 200,
                "stock": 1,
                "currency_id": 2,
                "amount": 1,
            }
        ],
    }
    config = FakeConfig()
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    set_event_shop_priority(config.config_name, spec["id"], 31, 0, root=tmp_path)

    prepared = prepare_event_shop_runtime_items(
        config,
        [
            runtime_item(
                group="ShipUR",
                price=200,
                stock=1,
                cost="URpt",
            )
        ],
        root=tmp_path,
    )
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert "UR-очки" in state["blocked"]["31"]


def test_event_shop_v2_uses_data_driven_display_name():
    assert (
        EventShopV2Mixin._event_shop_display_name("Game item 2:30387")
        == "Gear Skin Box (Seaside Speedstars)"
    )


def test_event_shop_v2_does_not_render_old_technical_status_copy():
    import inspect

    source = inspect.getsource(EventShopV2Mixin)
    assert "Расширенные настройки — автоматизация магазина" not in source
    assert "Нет наблюдения" not in source
    assert "Автоматизация на паузе" not in source
