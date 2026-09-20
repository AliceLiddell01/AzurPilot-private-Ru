from __future__ import annotations

from types import SimpleNamespace

from module.commission import commission
from module.os_handler.action_point import (
    EmergencyActionPointPurchase,
    EmergencyActionPointPurchaseStatus,
)


class _Store:
    def __init__(self):
        self.observations: list[tuple[str, int, str, str | None]] = []
        self.results: list[tuple[str, str]] = []
        self.closed = 0

    def read(self, profile):
        return SimpleNamespace(status="unknown", remaining=None, source=None)

    def record_observation(self, profile, remaining, *, source="game_ocr", last_result=None):
        self.observations.append((profile, remaining, source, last_result))
        return SimpleNamespace(status="confirmed")

    def record_result(self, profile, result):
        self.results.append((profile, result))
        return SimpleNamespace(status="confirmed")

    def close(self):
        self.closed += 1


class _ActionPoint:
    def __init__(self, purchase):
        self.purchase = purchase
        self.entered = 0
        self.quitted = 0
        self.purchase_calls = 0

    def action_point_enter(self, *, timeout):
        self.entered += 1
        return True

    def action_point_get_buy_remain_optional(self, *, timeout):
        return self.purchase.remaining_before

    def action_point_buy_emergency_once(self, *, remaining):
        self.purchase_calls += 1
        return self.purchase

    def action_point_quit(self, *, timeout):
        self.quitted += 1
        return True


def _commission(monkeypatch, store, action_point_handler, dorm_run):
    monkeypatch.setattr(commission, "ActionPointHandler", lambda *_args: action_point_handler)
    monkeypatch.setattr(
        commission,
        "RewardDorm",
        lambda *_args: SimpleNamespace(dorm_food_run=lambda **kwargs: dorm_run(kwargs["amount"])),
    )
    handler = commission.RewardCommission.__new__(commission.RewardCommission)
    handler.config = SimpleNamespace(config_name="ap")
    handler.device = object()
    handler.ui_ensure = lambda page: None
    handler._commission_recovery_store = lambda: store
    return handler


def test_successful_ap_recovery_records_only_proven_postcondition(monkeypatch):
    store = _Store()
    ap = _ActionPoint(
        EmergencyActionPointPurchase(
            status=EmergencyActionPointPurchaseStatus.PURCHASED,
            remaining_before=4,
            remaining_after=3,
            oil_cost=1000,
            click_count=1,
        )
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    assert handler._recover_commission_oil_overflow() is True
    assert store.observations == [("ap", 3, "emergency_ap_purchase", "ap_purchase")]
    assert store.results == []
    assert dorm_calls == []
    assert ap.entered == 1
    assert ap.quitted == 1
    assert store.closed == 1


def test_unknown_ap_postcondition_uses_one_dorm_fallback(monkeypatch):
    store = _Store()
    ap = _ActionPoint(
        EmergencyActionPointPurchase(
            status=EmergencyActionPointPurchaseStatus.UNKNOWN,
            remaining_before=4,
            oil_cost=1000,
            click_count=1,
        )
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    assert handler._recover_commission_oil_overflow() is False
    assert store.observations == []
    assert store.results == []
    assert dorm_calls == [10]
    assert ap.quitted == 1
    assert store.closed == 1


def test_repeated_overflow_does_not_repeat_emergency_purchase(monkeypatch):
    store = _Store()
    ap = _ActionPoint(
        EmergencyActionPointPurchase(
            status=EmergencyActionPointPurchaseStatus.PURCHASED,
            remaining_before=4,
            remaining_after=3,
            oil_cost=1000,
            click_count=1,
        )
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    assert handler._recover_commission_oil_overflow() is True
    assert ap.purchase_calls == 1

    assert handler._recover_commission_oil_overflow() is False
    assert ap.purchase_calls == 1
    assert dorm_calls == [10]


def test_zero_remaining_uses_dorm_without_emergency_purchase(monkeypatch):
    store = _Store()
    ap = _ActionPoint(
        EmergencyActionPointPurchase(
            status=EmergencyActionPointPurchaseStatus.UNAVAILABLE,
            remaining_before=0,
        )
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    assert handler._recover_commission_oil_overflow() is False
    assert ap.purchase_calls == 0
    assert store.observations == [("ap", 0, "game_ocr", "ap_unavailable")]
    assert dorm_calls == [10]
