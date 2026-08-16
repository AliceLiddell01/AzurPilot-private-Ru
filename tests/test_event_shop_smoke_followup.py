from types import SimpleNamespace

import pytest

import module.webui.event_shop_priority as priority
from module.shop_event.clerk import EventShopClerk
from module.shop_event.shop_event import EventShop
from module.webui.event_shop_priority import (
    PriorityRuntimeItems,
    confirm_event_shop_purchase,
    load_event_shop_priority,
    prepare_event_shop_runtime_items,
    set_event_shop_priority,
)


class FakeTaskStop(Exception):
    pass


class FakeConfig:
    config_name = "smoke-followup"
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


def runtime_item(*, remaining=100):
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


def event_spec():
    return {
        "id": "event-smoke",
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


def test_explicit_small_amount_does_not_take_max_then_decrement_path():
    assert EventShopClerk._prefer_amount_max(1, 10, 100) is False
    assert EventShopClerk._prefer_amount_max(1, 95, 100) is True


def test_event_shop_buy_item_reidentifies_at_precise_scroll_position(monkeypatch):
    calls = []
    target = SimpleNamespace(
        scroll_pos=0.3007513823848454,
        name="AugmentEnhanceT2",
        count=49,
        price=90,
        is_ship=False,
    )
    live = SimpleNamespace(
        name="AugmentEnhanceT2",
        count=49,
        price=90,
        is_ship=False,
    )

    class FakeScroll:
        @staticmethod
        def set_precise(position, main):
            calls.append(("set_precise", position, main))
            return 1

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        @staticmethod
        def event_shop_get_items():
            return [live]

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            calls.append(("buy", item, amount))

    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", FakeScroll())
    monkeypatch.setattr(
        "module.shop_event.clerk.confirm_event_shop_purchase",
        lambda config, item, full_purchase, remaining_after: calls.append(
            ("confirm", full_purchase, remaining_after)
        ),
    )

    shop = ProbeClerk()
    shop.event_shop_buy_item(target, amount=1)

    assert calls[0] == ("set_precise", target.scroll_pos, shop)
    assert calls[1] == ("buy", live, 1)
    assert calls[2] == ("confirm", False, 48)


def test_scan_all_preserves_observation_snapshot_when_priority_prepare_fails(
    monkeypatch,
):
    observed = [SimpleNamespace(button=(0, 100, 10, 110), name="Chip")]

    class FakeScroll:
        @staticmethod
        def set_top(main):
            return None

        @staticmethod
        def cal_position(main):
            return 0.0

        @staticmethod
        def at_bottom(main):
            return True

        @staticmethod
        def next_page(main, page):
            raise AssertionError("Одноэкранный тест не должен прокручивать магазин")

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace()
            self.device = SimpleNamespace(click_record_clear=lambda: None)

        def event_shop_get_items(self, scroll_pos=None):
            assert scroll_pos == 0.0
            return list(observed)

    def fail_prepare(*args, **kwargs):
        raise ValueError("искусственный сбой подготовки приоритетов")

    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", FakeScroll())
    monkeypatch.setattr(
        "module.shop_event.clerk.prepare_event_shop_runtime_items",
        fail_prepare,
    )

    result = ProbeClerk().scan_all()

    assert list(result) == []
    assert result.observation_items == observed


def test_verification_only_pass_reads_pt_before_empty_purchase_set(monkeypatch):
    calls = []
    warnings = []

    class ProbeEventShop(EventShop):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        def event_shop_load_ensure(self):
            calls.append("load")

        def get_current_pts(self):
            calls.append("pt")

        def scan_all(self):
            calls.append("scan")
            return []

    monkeypatch.setattr(
        "module.event_datamine.registry.EventArtifactRegistry.resolve_current",
        lambda self, server, now: None,
    )
    monkeypatch.setattr(
        "module.shop_event.shop_event.logger.warning",
        warnings.append,
    )

    assert ProbeEventShop()._run() is True
    assert calls == ["load", "pt", "scan"]
    assert warnings == ["[Магазин события] Товары в магазине события не найдены"]


def test_verification_only_pass_distinguishes_observed_shop_from_purchase_targets(
    monkeypatch,
):
    infos = []
    warnings = []
    observed = [runtime_item(remaining=90)]

    class ProbeEventShop(EventShop):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        @staticmethod
        def event_shop_load_ensure():
            return None

        @staticmethod
        def get_current_pts():
            return None

        @staticmethod
        def scan_all():
            return PriorityRuntimeItems([], observation_items=observed)

    monkeypatch.setattr(
        "module.event_datamine.registry.EventArtifactRegistry.resolve_current",
        lambda self, server, now: None,
    )
    monkeypatch.setattr(
        "module.shop_event.shop_event.logger.info",
        infos.append,
    )
    monkeypatch.setattr(
        "module.shop_event.shop_event.logger.warning",
        warnings.append,
    )

    assert ProbeEventShop()._run() is True
    assert infos == [
        "[Магазин события] Нет товаров, требующих покупки по текущим целям и приоритетам"
    ]
    assert warnings == []


def test_event_shop_pt_updates_dashboard_log(monkeypatch):
    recorded = []

    class FakeLogRes:
        def __init__(self, config):
            object.__setattr__(self, "config", config)

        def __setattr__(self, key, value):
            if key == "Pt":
                recorded.append(value)
            object.__setattr__(self, key, value)

    class ProbeEventShop(EventShop):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")
            self.__dict__["event_shop_has_urpt"] = False

        @staticmethod
        def event_shop_get_pt():
            return 5210

    monkeypatch.setattr("module.log_res.log_res.LogRes", FakeLogRes)
    monkeypatch.setattr(
        "module.event_datamine.registry.EventArtifactRegistry.resolve_current",
        lambda self, server, now: None,
    )

    shop = ProbeEventShop()
    shop.get_current_pts()

    assert shop.pt == 5210
    assert recorded == [5210]


def test_verified_partial_goal_clears_goal_and_priority(monkeypatch, tmp_path):
    spec = event_spec()
    config = FakeConfig()
    target = {"11": 10}
    cleared = []

    monkeypatch.setattr(priority, "_current_spec", lambda _config: spec)
    monkeypatch.setattr(priority, "_selected_targets", lambda _config, event_id: dict(target))

    def clear_target(_config, event_id, row_id, expected_selected):
        assert event_id == "event-smoke"
        assert row_id == "11"
        assert expected_selected == 10
        if target[row_id] != expected_selected:
            return False
        target[row_id] = 0
        cleared.append(row_id)
        return True

    monkeypatch.setattr(priority, "_clear_selected_target", clear_target)

    set_event_shop_priority(config.config_name, spec["id"], 11, 0, root=tmp_path)
    before = runtime_item(remaining=100)
    prepared = prepare_event_shop_runtime_items(config, [before], root=tmp_path)

    assert list(prepared) == [before]
    assert config.overrides["EventShop_CustomFilter"] == "Chip:10"

    with pytest.raises(FakeTaskStop):
        confirm_event_shop_purchase(
            config,
            before,
            full_purchase=False,
            remaining_after=90,
            root=tmp_path,
        )

    rescanned = runtime_item(remaining=90)
    prepared = prepare_event_shop_runtime_items(config, [rescanned], root=tmp_path)
    state = load_event_shop_priority(config.config_name, spec["id"], root=tmp_path)

    assert list(prepared) == []
    assert target["11"] == 0
    assert cleared == ["11"]
    assert "11" not in state["priorities"]
    assert state["completed"] == ["11"]
    assert state["purchased"] == []
    assert state["remaining"]["11"] == 90
    assert state["pending"] == {}
    assert config.overrides["EventShop_CustomFilter"] == ""
