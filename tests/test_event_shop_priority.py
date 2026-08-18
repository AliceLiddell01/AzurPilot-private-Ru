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


def patch_runtime_context(monkeypatch, spec, targets):
    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    monkeypatch.setattr(
        priority,
        "_selected_targets",
        lambda _config, event_id: {
            str(row_id): int(value)
            for row_id, value in targets.items()
            if str(event_id) == str(spec["id"])
        },
    )


def test_priority_runtime_authorizes_only_one_item_per_complete_scan(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 10, 12: 5})

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


def test_partial_quantity_target_compiles_to_amount_limited_runtime_filter(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 3})
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepared = prepare_event_shop_runtime_items(config, [chip], root=tmp_path)

    assert list(prepared) == [chip]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:3"


def test_quantity_target_stops_after_verified_goal_without_buying_rest(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    targets = {11: 3}
    cleared_targets = []
    patch_runtime_context(monkeypatch, spec, targets)

    def clear_selected_target(_config, event_id, row_id, expected_selected):
        assert _config is config
        assert event_id == spec["id"]
        assert row_id == "11"
        assert expected_selected == targets[11] == 3
        targets[11] = 0
        cleared_targets.append(row_id)
        return True

    monkeypatch.setattr(priority, "_clear_selected_target", clear_selected_target)
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepare_event_shop_runtime_items(config, [chip], root=tmp_path)
    assert config.overrides["EventShop_CustomFilter"] == "Chip:3"

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            chip,
            full_purchase=False,
            remaining_after=7,
            root=tmp_path,
        )

    rescanned = runtime_item(group="Chip", price=300, stock=10, remaining=7)
    prepared = prepare_event_shop_runtime_items(
        config,
        [rescanned],
        root=tmp_path,
    )
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert targets == {11: 0}
    assert cleared_targets == ["11"]
    assert "11" not in state["priorities"]
    assert state["completed"] == ["11"]
    assert state["remaining"]["11"] == 7
    assert state["pending"] == {}
    assert config.overrides["EventShop_CustomFilter"] == ""


def test_duplicate_runtime_token_uses_proven_source_row_identity(monkeypatch, tmp_path):
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
    patch_runtime_context(monkeypatch, spec, {21: 2, 22: 2})

    set_event_shop_priority(config.config_name, spec["id"], 21, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 22, 1, root=tmp_path)

    first = runtime_item(group="Chip", price=300, stock=10)
    second = runtime_item(group="Chip", price=600, stock=4)
    prepared = prepare_event_shop_runtime_items(
        config,
        [first, second],
        root=tmp_path,
    )
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == [first]
    assert first.catalog_row_id == 21
    assert second.catalog_row_id == 22
    assert state["blocked"] == {}
    assert config.overrides["EventShop_CustomFilter"] == "Chip:2"


def test_priority_without_quantity_target_never_authorizes_purchase(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 0})
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepared = prepare_event_shop_runtime_items(config, [chip], root=tmp_path)

    assert list(prepared) == []
    assert config.overrides["EventShop_CustomFilter"] == ""


def test_full_purchase_is_marked_only_after_fresh_scan(monkeypatch, tmp_path):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 10})
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


def test_partial_affordable_purchase_keeps_only_unfulfilled_target(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 8})
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)

    chip = runtime_item(group="Chip", price=300, stock=10)
    prepare_event_shop_runtime_items(config, [chip], root=tmp_path)
    assert config.overrides["EventShop_CustomFilter"] == "Chip:8"

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
    assert config.overrides["EventShop_CustomFilter"] == "Chip:4"
    assert state["priorities"]["11"] == 0
    assert state["purchased"] == []
    assert state["remaining"]["11"] == 6
    assert state["pending"] == {}


def test_failed_post_purchase_verification_blocks_all_new_clicks(
    monkeypatch, tmp_path
):
    spec = base_spec()
    config = FakeConfig()
    patch_runtime_context(monkeypatch, spec, {11: 8, 12: 5})
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


def test_urpt_priority_is_blocked_until_safe_priority_runtime_support(
    monkeypatch, tmp_path
):
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
    patch_runtime_context(monkeypatch, spec, {31: 1})
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
