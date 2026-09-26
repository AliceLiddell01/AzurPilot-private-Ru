from __future__ import annotations

from types import SimpleNamespace

import pytest

from module.application import commission_recovery
from module.os import action_point_policy
from module.os_handler import action_point


def _handler(
    monkeypatch,
    weekly_observations,
    *,
    ap_observations=(200, 300),
    oil_observations=(25000, 24000),
):
    handler = action_point.ActionPointHandler.__new__(action_point.ActionPointHandler)
    handler._action_point_box = [25000, 0, 0, 0]
    clicks: list[object] = []
    events: list[object] = []

    class _Device:
        image = "cached-frame"

        def screenshot(self):
            self.image = f"fresh-frame-{len([event for event in events if event == 'screenshot']) + 1}"
            events.append("screenshot")

        def click(self, button):
            events.append("click")
            clicks.append(button)

    handler.device = _Device()
    monkeypatch.setattr(handler, "action_point_set_button", lambda _index: True)

    ap_iterator = iter(ap_observations)
    oil_iterator = iter(oil_observations)

    def safe_get():
        ap = next(ap_iterator)
        oil = next(oil_iterator)
        events.append(("safe_get", handler.device.image, ap))
        handler._action_point_box = [oil, 0, 0, 0]
        return ap

    monkeypatch.setattr(handler, "action_point_safe_get", safe_get)
    monkeypatch.setattr(handler, "appear", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        handler,
        "loop",
        lambda **_kwargs: iter(range(max(len(weekly_observations) - 1, 1))),
    )
    iterator = iter(weekly_observations)
    monkeypatch.setattr(
        handler,
        "action_point_get_buy_remain_optional",
        lambda **_kwargs: next(iterator),
    )
    return handler, clicks, events


def test_action_point_purchase_policy_has_one_neutral_domain_owner() -> None:
    assert (
        action_point.get_action_point_purchase_policy
        is action_point_policy.get_action_point_purchase_policy
    )
    assert (
        commission_recovery.get_action_point_purchase_policy
        is action_point_policy.get_action_point_purchase_policy
    )
    assert {
        remaining: (policy.oil_cost, policy.ap_gain)
        for remaining in range(1, 6)
        if (
            policy := action_point_policy.get_action_point_purchase_policy(remaining)
        )
        is not None
    } == {
        5: (1000, 100),
        4: (1000, 100),
        3: (2000, 200),
        2: (2000, 200),
        1: (4000, 400),
    }
    assert action_point_policy.get_action_point_purchase_policy(0) is None
    assert action_point_policy.get_action_point_purchase_policy(6) is None
    assert action_point_policy.get_action_point_purchase_policy(None) is None
    assert action_point_policy.get_action_point_purchase_policy(True) is None


@pytest.mark.parametrize(
    ("remaining", "oil_cost", "ap_gain"),
    [
        (5, 1000, 100),
        (4, 1000, 100),
        (3, 2000, 200),
        (2, 2000, 200),
        (1, 4000, 400),
    ],
)
def test_emergency_purchase_uses_the_matching_weekly_tier(
    monkeypatch, remaining, oil_cost, ap_gain
):
    handler, clicks, _events = _handler(
        monkeypatch,
        [remaining, remaining - 1],
        ap_observations=(200, 200 + ap_gain),
        oil_observations=(25000, 25000 - oil_cost),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=remaining)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.remaining_before == remaining
    assert result.remaining_after == remaining - 1
    assert result.oil_cost == oil_cost
    assert result.ap_gain == ap_gain
    assert result.click_count == 1
    assert len(clicks) == 1


def test_emergency_purchase_accepts_live_remaining_three_tier(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [3, 2],
        ap_observations=(200, 400),
        oil_observations=(25000, 23000),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=3)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.remaining_before == 3
    assert result.remaining_after == 2
    assert result.oil_cost == 2000
    assert result.oil_before == 25000
    assert result.oil_after == 23000
    assert result.ap_before == 200
    assert result.ap_after == 400
    assert result.ap_gain == 200
    assert result.click_count == 1
    assert len(clicks) == 1


def test_emergency_purchase_200_to_300_is_purchased_with_one_click(monkeypatch):
    handler, clicks, _events = _handler(monkeypatch, [5, 4])

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.remaining_before == 5
    assert result.remaining_after == 4
    assert result.oil_cost == 1000
    assert result.oil_before == 25000
    assert result.oil_after == 24000
    assert result.ap_before == 200
    assert result.ap_after == 300
    assert result.ap_gain == 100
    assert result.click_count == 1
    assert len(clicks) == 1


def test_emergency_purchase_allows_ap_below_max_without_clamp(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 4],
        ap_observations=(115, 215),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.ap_before == 115
    assert result.ap_after == 215
    assert result.ap_gain == 100
    assert len(clicks) == 1


def test_emergency_purchase_at_ap_max_allows_200_to_300(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 4],
        ap_observations=(250, 350),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.ap_before == 250
    assert result.ap_after == 350
    assert result.ap_after > result.ap_before
    assert len(clicks) == 1


def test_emergency_purchase_waits_through_unchanged_post_click_frame(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 5, 4],
        ap_observations=(200, 200, 300),
        oil_observations=(25000, 25000, 24000),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.remaining_after == 4
    assert result.ap_gain == 100
    assert result.oil_after == 24000
    assert len(clicks) == 1


def test_emergency_purchase_waits_through_partial_weekly_decrement(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 4],
        ap_observations=(200, 200),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.UNKNOWN
    assert result.click_count == 1
    assert result.ap_before == 200
    assert result.ap_after == 200
    assert result.ap_gain == 0
    assert len(clicks) == 1


def test_emergency_purchase_rejects_unknown_ap_after_click(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 4, 4],
        ap_observations=(200, None, None),
        oil_observations=(25000, 24000, 24000),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.UNKNOWN
    assert result.click_count == 1
    assert result.ap_before == 200
    assert result.ap_after is None
    assert len(clicks) == 1


def test_emergency_purchase_waits_through_partial_ap_delta(monkeypatch):
    handler, clicks, _events = _handler(monkeypatch, [5, 5])

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.UNKNOWN
    assert result.click_count == 1
    assert result.ap_before == 200
    assert result.ap_after == 300
    assert result.ap_gain == 100
    assert len(clicks) == 1


def test_emergency_purchase_fails_on_contradictory_ap_increase(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [5, 5],
        ap_observations=(200, 301),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.FAILED
    assert result.ap_gain == 101
    assert result.click_count == 1
    assert len(clicks) == 1


def test_emergency_purchase_ignores_stale_cached_ap_for_before_observation(monkeypatch):
    handler, _clicks, events = _handler(monkeypatch, [5, 4])
    handler._action_point_current = 115

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    safe_gets = [event for event in events if isinstance(event, tuple) and event[0] == "safe_get"]
    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    assert result.ap_before == 200
    assert safe_gets[0] == ("safe_get", "fresh-frame-1", 200)
    assert safe_gets[1] == ("safe_get", "fresh-frame-3", 300)


def test_emergency_purchase_reads_ap_after_fresh_post_click_frame(monkeypatch):
    handler, _clicks, events = _handler(monkeypatch, [5, 4])

    result = handler.action_point_buy_emergency_once(expected_remaining=5)

    assert result.status is action_point.EmergencyActionPointPurchaseStatus.PURCHASED
    click_index = events.index("click")
    post_safe_get = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "safe_get" and event[2] == 300
    )
    assert click_index < post_safe_get
    assert events[click_index + 1] == "screenshot"
    assert events[post_safe_get - 1] == "screenshot"


def test_emergency_purchase_does_not_retry_after_unknown_postcondition(monkeypatch):
    handler, clicks, _events = _handler(
        monkeypatch,
        [4, 4, 4, 4, 4],
        ap_observations=(200, None, None, None, None),
        oil_observations=(25000, 24000, 24000, 24000, 24000),
    )

    result = handler.action_point_buy_emergency_once(expected_remaining=4, wait_timeout=0.01)

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
