from types import SimpleNamespace

import module.shop_event.shop_event as shop_module
import module.webui.event_shop_priority as priority
from module.shop_event.shop_event import EventShop
from module.webui.event_shop_priority import (
    prepare_event_shop_runtime_items,
    set_event_shop_priority,
    wake_event_shop_after_currency_increase,
)


class PriorityConfig:
    config_name = "test-instance"
    SERVER = "EN"

    def __init__(self):
        self.overrides = {}

    def override(self, **kwargs):
        self.overrides.update(kwargs)


class WakeConfig:
    config_name = "test-instance"
    SERVER = "EN"

    def __init__(self):
        self.task_calls = []

    @staticmethod
    def is_task_enabled(task):
        assert task == "EventShop"
        return True

    def task_call(self, task, force_call=True):
        self.task_calls.append((task, force_call))
        return True


def _runtime_item(group, price, stock, *, remaining=None):
    return SimpleNamespace(
        name=group,
        group=group,
        sub_genre="",
        tier="",
        price=price,
        total_count=stock,
        count=stock if remaining is None else remaining,
        cost="pt",
        amount=1,
    )


def _priority_spec():
    return {
        "id": "event-test",
        "server": "EN",
        "currencies": [{"id": 1, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 11,
                "event_shop_filter": "Expensive",
                "price": 8000,
                "stock": 1,
                "currency_id": 1,
                "amount": 1,
            },
            {
                "row_id": 12,
                "event_shop_filter": "Affordable",
                "price": 1000,
                "stock": 3,
                "currency_id": 1,
                "amount": 1,
            },
            {
                "row_id": 13,
                "event_shop_filter": "CheapLater",
                "price": 100,
                "stock": 1,
                "currency_id": 1,
                "amount": 1,
            },
        ],
    }


def _patch_priority_context(monkeypatch, spec, targets):
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


def test_prepare_exposes_only_highest_active_priority_group(monkeypatch, tmp_path):
    spec = _priority_spec()
    config = PriorityConfig()
    _patch_priority_context(monkeypatch, spec, {11: 1, 12: 3, 13: 1})
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 12, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 13, 1, root=tmp_path)

    expensive = _runtime_item("Expensive", 8000, 1)
    affordable = _runtime_item("Affordable", 1000, 3)
    cheap_later = _runtime_item("CheapLater", 100, 1)

    prepared = prepare_event_shop_runtime_items(
        config,
        [expensive, affordable, cheap_later],
        root=tmp_path,
    )

    assert list(prepared) == [expensive, affordable]
    assert config.overrides["EventShop_CustomFilter"] == "Expensive > Affordable"


def test_quantity_target_larger_than_current_remaining_is_clamped(monkeypatch, tmp_path):
    spec = _priority_spec()
    spec["shop_items"] = [
        {
            "row_id": 21,
            "event_shop_filter": "Chip",
            "price": 1000,
            "stock": 10,
            "currency_id": 1,
            "amount": 1,
        }
    ]
    config = PriorityConfig()
    _patch_priority_context(monkeypatch, spec, {21: 10})
    set_event_shop_priority(config.config_name, spec["id"], 21, 0, root=tmp_path)

    chip = _runtime_item("Chip", 1000, 10, remaining=2)
    prepared = prepare_event_shop_runtime_items(config, [chip], root=tmp_path)

    assert list(prepared) == [chip]
    assert config.overrides["EventShop_CustomFilter"] == "Chip"


def test_priority_runtime_skips_unaffordable_item_within_same_group(monkeypatch):
    expensive = _runtime_item("Expensive", 8000, 1)
    affordable = _runtime_item("Affordable", 1000, 1)
    purchased = []

    class ProbeEventShop(EventShop):
        def __init__(self):
            self.config = SimpleNamespace(
                config_name="probe",
                EventShop_BuyURShip=0,
                EventShop_UnlockSSRShip=False,
                EventShop_PresetFilter="custom",
                EventShop_CustomFilter="Expensive > Affordable",
                EventShop_PriorityMode=True,
            )
            self.pt = 1245
            self.pt_preserved = 0

        @staticmethod
        def _begin_event_shop_pass_context():
            return None

        @staticmethod
        def event_shop_load_ensure():
            return None

        def get_current_pts(self):
            self.pt = 1245

        @staticmethod
        def _current_event_artifact():
            return None

        @staticmethod
        def scan_all():
            return [expensive, affordable]

        @staticmethod
        def handle_items_related_with_urpt(items, _count):
            return list(items), []

        @staticmethod
        def handle_unobtained_items(items, _enabled):
            return list(items), []

        @staticmethod
        def event_shop_buy_item(item, amount=None):
            purchased.append((item.group, amount))

    monkeypatch.setattr(
        shop_module,
        "FILTER",
        SimpleNamespace(load=lambda _value: None, apply=lambda items: list(items)),
    )

    assert ProbeEventShop()._run() is True
    assert purchased == [("Affordable", None)]


def test_currency_growth_wakes_only_on_top_group_affordability_crossing(
    monkeypatch, tmp_path
):
    spec = _priority_spec()
    config = WakeConfig()
    _patch_priority_context(monkeypatch, spec, {11: 1, 12: 1, 13: 1})
    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 12, 0, root=tmp_path)
    set_event_shop_priority(config.config_name, spec["id"], 13, 1, root=tmp_path)

    assert not wake_event_shop_after_currency_increase(
        config=config,
        event_id=spec["id"],
        previous_value=50,
        current_value=500,
        source="dashboard_ocr",
        root=tmp_path,
    )
    assert config.task_calls == []

    assert wake_event_shop_after_currency_increase(
        config=config,
        event_id=spec["id"],
        previous_value=900,
        current_value=1100,
        source="dashboard_ocr",
        root=tmp_path,
    )
    assert config.task_calls == [("EventShop", False)]

    assert not wake_event_shop_after_currency_increase(
        config=config,
        event_id=spec["id"],
        previous_value=1100,
        current_value=1500,
        source="dashboard_ocr",
        root=tmp_path,
    )
    assert config.task_calls == [("EventShop", False)]
