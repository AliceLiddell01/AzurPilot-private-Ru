from types import SimpleNamespace

import pytest

from module.exception import GameStuckError
from module.shop_event.clerk import EventShopClerk


def make_target(*, scroll_pos=0.23581560283687944, button=(400, 480, 460, 560)):
    return SimpleNamespace(
        scroll_pos=scroll_pos,
        button=button,
        name="AugmentEnhanceT2",
        count=49,
        total_count=50,
        amount=1,
        price=90,
        is_ship=False,
    )


def make_live_target():
    return SimpleNamespace(
        name="AugmentEnhanceT2",
        count=49,
        total_count=50,
        amount=1,
        price=90,
        is_ship=False,
    )


class FakeScroll:
    reidentify_drag_threshold = 0.02
    edge_threshold = 0.02

    def __init__(self):
        self.positions = []

    def set_precise(self, position, main):
        self.positions.append(float(position))
        return 1

    def cal_position(self, main):
        return self.positions[-1]


def test_reidentify_positions_bias_lower_row_forward(monkeypatch):
    scroll = FakeScroll()
    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", scroll)

    target = make_target()
    positions = EventShopClerk._purchase_reidentify_positions(target)

    assert positions == pytest.approx([
        target.scroll_pos,
        target.scroll_pos + 0.04,
        target.scroll_pos - 0.04,
    ])


def test_reidentify_positions_bias_upper_row_backward(monkeypatch):
    scroll = FakeScroll()
    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", scroll)

    target = make_target(button=(400, 220, 460, 300))
    positions = EventShopClerk._purchase_reidentify_positions(target)

    assert positions == pytest.approx([
        target.scroll_pos,
        target.scroll_pos - 0.04,
        target.scroll_pos + 0.04,
    ])


def test_purchase_recovers_target_on_neighbor_probe_before_any_click(monkeypatch):
    scroll = FakeScroll()
    target = make_target()
    live = make_live_target()
    distractor = SimpleNamespace(
        name="CatT3",
        count=2,
        price=3000,
        is_ship=False,
    )
    calls = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(
                config_name="probe",
                SHOP_EXTRACT_TEMPLATE=True,
            )

        def event_shop_get_items(self):
            calls.append(("scan", len(scroll.positions), getattr(self, "_scan_extract_templates", None)))
            if len(scroll.positions) == 1:
                return [distractor]
            return [live]

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            calls.append(("buy", item, amount))

    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", scroll)
    monkeypatch.setattr(
        "module.shop_event.clerk.confirm_event_shop_purchase",
        lambda config, item, full_purchase, remaining_after: calls.append(
            ("confirm", item, full_purchase, remaining_after)
        ),
    )

    shop = ProbeClerk()
    shop.event_shop_buy_item(target, amount=1)

    assert scroll.positions == pytest.approx([
        target.scroll_pos,
        target.scroll_pos + 0.04,
    ])
    assert calls[0] == ("scan", 1, False)
    assert calls[1] == ("scan", 2, False)
    assert calls[2] == ("buy", live, 1)
    assert calls[3] == ("confirm", live, False, 48)
    assert "_scan_extract_templates" not in shop.__dict__


def test_purchase_fails_closed_after_bounded_probes_without_click(monkeypatch):
    scroll = FakeScroll()
    target = make_target()
    calls = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(
                config_name="probe",
                SHOP_EXTRACT_TEMPLATE=False,
            )

        @staticmethod
        def event_shop_get_items():
            return [
                SimpleNamespace(
                    name="CatT3",
                    count=2,
                    price=3000,
                    is_ship=False,
                )
            ]

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            calls.append((item, amount))

    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", scroll)

    with pytest.raises(GameStuckError, match="нажатие покупки заблокировано"):
        ProbeClerk().event_shop_buy_item(target, amount=1)

    assert scroll.positions == pytest.approx([
        target.scroll_pos,
        target.scroll_pos + 0.04,
        target.scroll_pos - 0.04,
    ])
    assert calls == []

def test_purchase_match_requires_total_count_and_amount():
    target = make_target()
    live = make_live_target()

    assert EventShopClerk._purchase_item_matches(live, target) is True

    live.total_count = 51
    assert EventShopClerk._purchase_item_matches(live, target) is False

    live.total_count = 50
    live.amount = 2
    assert EventShopClerk._purchase_item_matches(live, target) is False


def test_purchase_fails_closed_on_multiple_reidentified_matches(monkeypatch):
    scroll = FakeScroll()
    target = make_target()
    calls = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(
                config_name="probe",
                SHOP_EXTRACT_TEMPLATE=False,
            )

        @staticmethod
        def event_shop_get_items():
            return [make_live_target(), make_live_target()]

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            calls.append((item, amount))

    monkeypatch.setattr("module.shop_event.clerk.EVENT_SHOP_SCROLL", scroll)

    with pytest.raises(
        GameStuckError, match="неоднозначна.*нажатие покупки заблокировано"
    ):
        ProbeClerk().event_shop_buy_item(target, amount=1)

    assert scroll.positions == pytest.approx([target.scroll_pos])
    assert calls == []
