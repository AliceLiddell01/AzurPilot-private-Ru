from module.shop_event.ui import EVENT_SHOP_SCROLL


def test_event_shop_scroll_requires_near_terminal_position(monkeypatch):
    monkeypatch.setattr(EVENT_SHOP_SCROLL, "cal_position", lambda main: 0.90)
    assert EVENT_SHOP_SCROLL.at_bottom(object()) is False

    monkeypatch.setattr(EVENT_SHOP_SCROLL, "cal_position", lambda main: 0.99)
    assert EVENT_SHOP_SCROLL.at_bottom(object()) is True


def test_event_shop_scroll_drag_threshold_can_reach_terminal_zone():
    assert EVENT_SHOP_SCROLL.drag_threshold <= EVENT_SHOP_SCROLL.edge_threshold
    assert EVENT_SHOP_SCROLL.edge_threshold <= 0.02
