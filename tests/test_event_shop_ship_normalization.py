from types import SimpleNamespace

import pytest

from module.exception import GameStuckError
from module.shop_event.clerk import EventShopClerk


@pytest.mark.parametrize(
    ("amount", "expected_buys", "full_purchase", "remaining_after"),
    [
        (None, 2, True, 0),
        (1, 1, False, 1),
    ],
)
def test_ship_purchase_uses_normalized_count_for_loop(
    monkeypatch, amount, expected_buys, full_purchase, remaining_after
):
    live_item = SimpleNamespace(count="2", is_ship=True)
    purchases = []
    confirmations = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        def _reidentify_event_shop_item(self, item_to_buy):
            return live_item

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            purchases.append((item, amount))

    def confirm(config, item, *, full_purchase, remaining_after):
        confirmations.append((config, item, full_purchase, remaining_after))

    monkeypatch.setattr("module.shop_event.clerk.confirm_event_shop_purchase", confirm)

    shop = ProbeClerk()
    shop.event_shop_buy_item(SimpleNamespace(), amount=amount)

    assert purchases == [(live_item, 1)] * expected_buys
    assert confirmations == [
        (shop.config, live_item, full_purchase, remaining_after)
    ]


def test_non_ship_purchase_clamps_requested_amount_to_live_count(monkeypatch):
    live_item = SimpleNamespace(count="2", is_ship=False)
    purchases = []
    confirmations = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        def _reidentify_event_shop_item(self, item_to_buy):
            return live_item

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            purchases.append((item, amount))

    def confirm(config, item, *, full_purchase, remaining_after):
        confirmations.append((config, item, full_purchase, remaining_after))

    monkeypatch.setattr("module.shop_event.clerk.confirm_event_shop_purchase", confirm)

    shop = ProbeClerk()
    shop.event_shop_buy_item(SimpleNamespace(), amount=5)

    assert purchases == [(live_item, 2)]
    assert confirmations == [(shop.config, live_item, True, 0)]


@pytest.mark.parametrize(
    ("count", "amount"),
    [
        ("invalid", 1),
        ("2", "invalid"),
        ("0", 1),
        ("2", 0),
    ],
)
def test_purchase_rejects_invalid_or_nonpositive_quantity_before_execute(
    monkeypatch, count, amount
):
    live_item = SimpleNamespace(count=count, is_ship=False)
    purchases = []

    class ProbeClerk(EventShopClerk):
        def __init__(self):
            self.config = SimpleNamespace(config_name="probe")

        def _reidentify_event_shop_item(self, item_to_buy):
            return live_item

        @staticmethod
        def event_shop_buy_item_execute(item, amount):
            purchases.append((item, amount))

    monkeypatch.setattr(
        "module.shop_event.clerk.confirm_event_shop_purchase",
        lambda *args, **kwargs: pytest.fail("Подтверждение не должно вызываться"),
    )

    with pytest.raises(GameStuckError, match="покупка заблокирована"):
        ProbeClerk().event_shop_buy_item(SimpleNamespace(), amount=amount)

    assert purchases == []
