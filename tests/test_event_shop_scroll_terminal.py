from concurrent.futures import ThreadPoolExecutor
from threading import Event

from module.shop_event.ui import EVENT_SHOP_SCROLL
from module.ui.scroll import Scroll


def test_event_shop_scroll_requires_near_terminal_position(monkeypatch):
    monkeypatch.setattr(EVENT_SHOP_SCROLL, "cal_position", lambda main: 0.90)
    assert EVENT_SHOP_SCROLL.at_bottom(object()) is False

    monkeypatch.setattr(EVENT_SHOP_SCROLL, "cal_position", lambda main: 0.99)
    assert EVENT_SHOP_SCROLL.at_bottom(object()) is True


def test_event_shop_scroll_uses_coarse_threshold_between_edges():
    assert EVENT_SHOP_SCROLL.drag_threshold == 0.1
    assert EVENT_SHOP_SCROLL._drag_threshold_for_target(0.69) == 0.1
    assert EVENT_SHOP_SCROLL._drag_threshold_for_target(0.90) == 0.1


def test_event_shop_scroll_uses_strict_threshold_near_terminal_edges():
    assert EVENT_SHOP_SCROLL._drag_threshold_for_target(0.0) <= EVENT_SHOP_SCROLL.edge_threshold
    assert EVENT_SHOP_SCROLL._drag_threshold_for_target(0.99) <= EVENT_SHOP_SCROLL.edge_threshold
    assert EVENT_SHOP_SCROLL._drag_threshold_for_target(1.0) <= EVENT_SHOP_SCROLL.edge_threshold
    assert EVENT_SHOP_SCROLL.edge_threshold <= 0.02


def test_event_shop_scroll_restores_default_threshold_after_set(monkeypatch):
    observed = []

    def fake_set(self, position, main, random_range=(-0.05, 0.05), distance_check=True, skip_first_screenshot=True):
        observed.append(self.drag_threshold)
        return 1

    monkeypatch.setattr(Scroll, "set", fake_set)

    assert EVENT_SHOP_SCROLL.set(0.69, object()) == 1
    assert observed == [0.1]
    assert EVENT_SHOP_SCROLL.drag_threshold == 0.1

    observed.clear()
    assert EVENT_SHOP_SCROLL.set(1.0, object()) == 1
    assert observed == [0.02]
    assert EVENT_SHOP_SCROLL.drag_threshold == 0.1


def test_event_shop_scroll_precise_reidentification_has_no_random_offset(monkeypatch):
    observed = []

    def fake_set(self, position, main, random_range=(-0.05, 0.05), distance_check=True, skip_first_screenshot=True):
        observed.append((position, self.drag_threshold, random_range))
        return 1

    monkeypatch.setattr(Scroll, "set", fake_set)

    assert EVENT_SHOP_SCROLL.set_precise(0.3007513823848454, object()) == 1
    assert observed == [
        (0.3007513823848454, 0.02, (0.0, 0.0)),
    ]
    assert EVENT_SHOP_SCROLL.drag_threshold == 0.1


def test_event_shop_scroll_serializes_temporary_threshold_override(monkeypatch):
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    observed = []

    def fake_set(self, position, main, random_range=(-0.05, 0.05), distance_check=True, skip_first_screenshot=True):
        observed.append((position, self.drag_threshold))
        if position == 1.0:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return position

    monkeypatch.setattr(Scroll, "set", fake_set)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(EVENT_SHOP_SCROLL.set, 1.0, object())
        assert first_entered.wait(timeout=5)
        second = executor.submit(EVENT_SHOP_SCROLL.set, 0.69, object())
        try:
            assert not second_entered.wait(timeout=0.1)
        finally:
            release_first.set()

        assert first.result(timeout=5) == 1.0
        assert second.result(timeout=5) == 0.69

    assert observed == [(1.0, 0.02), (0.69, 0.1)]
    assert EVENT_SHOP_SCROLL.drag_threshold == 0.1
