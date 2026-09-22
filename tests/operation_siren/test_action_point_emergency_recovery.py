from __future__ import annotations

from types import SimpleNamespace

from module.os_handler import action_point


def _handler(monkeypatch, observations):
    handler = action_point.ActionPointHandler.__new__(action_point.ActionPointHandler)
    handler._action_point_box = [25000, 0, 0, 0]
    handler.device = SimpleNamespace(click=lambda *_args, **_kwargs: None)
    clicks: list[object] = []
    handler.device.click = lambda button: clicks.append(button)
    monkeypatch.setattr(handler, "action_point_set_button", lambda _index: True)
    monkeypatch.setattr(handler, "action_point_safe_get", lambda: None)
    monkeypatch.setattr(handler, "appear", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(handler, "loop", lambda **_kwargs: iter(range(len(observations))))
    iterator = iter(observations)
    monkeypatch.setattr(
        handler,
        "action_point_get_buy_remain_optional",
        lambda **_kwargs: next(iterator),
    )
    return handler, clicks


def test_emergency_purchase_clicks_once_and_requires_decrement(monkeypatch):
    handler, clicks = _handler(monkeypatch, [3])

    result = handler.action_point_buy_emergency_once(remaining=4)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.remaining_before == 4
    assert result.remaining_after == 3
    assert result.oil_cost == 1000
    assert result.click_count == 1
    assert len(clicks) == 1


def test_emergency_purchase_does_not_retry_after_unknown_postcondition(monkeypatch):
    handler, clicks = _handler(monkeypatch, [4, 4, 4])

    result = handler.action_point_buy_emergency_once(remaining=4, wait_timeout=0.01)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.UNKNOWN
    assert result.click_count == 1
    assert len(clicks) == 1


def test_action_point_set_button_switches_from_box_m_to_oil(monkeypatch):
    handler = action_point.ActionPointHandler.__new__(action_point.ActionPointHandler)
    clicks: list[object] = []
    handler.device = SimpleNamespace(
        click=lambda button: clicks.append(button),
        sleep=lambda _seconds: None,
    )
    active_buttons = iter([2, 0])
    monkeypatch.setattr(handler, "action_point_get_active_button", lambda: next(active_buttons))
    monkeypatch.setattr(handler, "loop", lambda **_kwargs: iter((None, None)))

    assert handler.action_point_set_button(0) is True
    assert clicks == [action_point.ACTION_POINT_GRID[0, 0]]


def test_action_point_set_button_does_not_click_when_oil_is_already_selected(monkeypatch):
    handler = action_point.ActionPointHandler.__new__(action_point.ActionPointHandler)
    clicks: list[object] = []
    handler.device = SimpleNamespace(click=lambda button: clicks.append(button))
    monkeypatch.setattr(handler, "action_point_get_active_button", lambda: 0)
    monkeypatch.setattr(handler, "loop", lambda **_kwargs: iter((None,)))

    assert handler.action_point_set_button(0) is True
    assert clicks == []
