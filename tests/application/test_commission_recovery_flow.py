from __future__ import annotations

from types import SimpleNamespace

import pytest

from module.commission import commission
from module.exception import OilMaxed, RequestHumanTakeover
from module.os_handler.action_point import (
    EmergencyActionPointPurchase,
    EmergencyActionPointPurchaseStatus,
)


def _state(
    profile: str,
    status: str,
    remaining: int | None,
    *,
    source: str | None = "game_ocr",
    last_result: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile,
        status=status,
        remaining=remaining,
        source=source,
        last_result=last_result,
    )


class _Store:
    def __init__(self, *, status: str = "unknown", remaining: int | None = None, order=None):
        self.state = _state("ap", status, remaining)
        self.observations: list[tuple[str, int, str, str | None]] = []
        self.results: list[tuple[str, str]] = []
        self.invalidations: list[tuple[str, str | None]] = []
        self.events: list[str] = []
        self.order = order
        self.read_calls = 0
        self.closed = 0

    def _record_event(self, event):
        self.events.append(event)
        if self.order is not None:
            self.order.append(event)

    def read(self, profile):
        self.read_calls += 1
        self._record_event("read")
        return self.state

    def record_observation(self, profile, remaining, *, source="game_ocr", last_result=None):
        self._record_event("record_observation")
        self.observations.append((profile, remaining, source, last_result))
        self.state = _state(
            profile,
            "confirmed",
            remaining,
            source=source,
            last_result=last_result,
        )
        return self.state

    def record_result(self, profile, result):
        self._record_event("record_result")
        self.results.append((profile, result))
        if self.state.status == "confirmed":
            self.state = _state(
                profile,
                "confirmed",
                self.state.remaining,
                source=self.state.source,
                last_result=result,
            )
        return self.state

    def invalidate(self, profile, *, last_result=None):
        self._record_event("invalidate")
        self.invalidations.append((profile, last_result))
        self.state = _state(
            profile,
            "unknown",
            None,
            source=None,
            last_result=last_result,
        )
        return self.state

    def close(self):
        self.closed += 1


class _ActionPoint:
    def __init__(self, purchase, *, ocr_remaining="from_purchase", select_oil=True, events=None):
        self.purchase = purchase
        self.ocr_remaining = ocr_remaining
        self.select_oil = select_oil
        self.events = events if events is not None else []
        self.entered = 0
        self.select_oil_calls = 0
        self.quitted = 0
        self.purchase_calls = 0
        self.remaining_args: list[int] = []

    def action_point_enter(self, *, timeout):
        self.events.append("enter")
        self.entered += 1
        return True

    def action_point_set_button(self, index):
        assert index == 0
        self.events.append("select_oil")
        self.select_oil_calls += 1
        return self.select_oil

    def action_point_get_buy_remain_optional(self, *, timeout):
        self.events.append("ocr")
        if self.ocr_remaining != "from_purchase":
            return self.ocr_remaining
        return self.purchase.remaining_before

    def action_point_buy_emergency_once(self, *, remaining):
        self.events.append(f"purchase:{remaining}")
        self.purchase_calls += 1
        self.remaining_args.append(remaining)
        return self.purchase

    def action_point_quit(self, *, timeout):
        self.events.append("quit")
        self.quitted += 1
        return True


def _commission(monkeypatch, store, action_point_handler, dorm_run, *, events=None):
    monkeypatch.setattr(commission, "ActionPointHandler", lambda *_args: action_point_handler)
    monkeypatch.setattr(
        commission,
        "RewardDorm",
        lambda *_args: SimpleNamespace(dorm_food_run=lambda **kwargs: dorm_run(kwargs["amount"])),
    )
    handler = commission.RewardCommission.__new__(commission.RewardCommission)
    handler.config = SimpleNamespace(config_name="ap")
    handler.device = object()
    handler.ui_ensure = (
        lambda page: events.append(
            "ui_reward" if page is commission.page_reward else "ui_os"
        )
        if events is not None
        else None
    )
    handler._commission_recovery_store = lambda: store
    return handler


def _purchase(
    status: EmergencyActionPointPurchaseStatus,
    *,
    before: int | None = 4,
    after: int | None = None,
    clicks: int = 1,
    ap_before: int | None = 200,
    ap_after: int | None = 300,
    ap_gain: int | None = 100,
    oil_before: int | None = 25000,
    oil_after: int | None = 24000,
) -> EmergencyActionPointPurchase:
    return EmergencyActionPointPurchase(
        status=status,
        remaining_before=before,
        remaining_after=after,
        oil_cost=1000,
        oil_before=oil_before,
        oil_after=oil_after,
        ap_before=ap_before,
        ap_after=ap_after,
        ap_gain=ap_gain,
        click_count=clicks,
    )


def test_confirmed_positive_state_is_entry_authority_and_records_readback(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.AP_RECOVERED
    assert store.events[0] == "read"
    assert store.observations == [("ap", 3, "emergency_ap_purchase", "ap_purchase")]
    assert store.state.status == "confirmed"
    assert store.state.remaining == 3
    assert store.state.source == "emergency_ap_purchase"
    assert store.state.last_result == "ap_purchase"
    assert store.read_calls >= 2
    assert ap.remaining_args == [4]
    assert dorm_calls == []


def test_confirmed_zero_uses_dorm_without_opening_ap(monkeypatch):
    store = _Store(status="confirmed", remaining=0)
    dorm_calls: list[int] = []

    def unexpected_action_point(*_args):
        raise AssertionError("ActionPointHandler не нужен для подтверждённого нулевого state")

    handler = _commission(monkeypatch, store, None, dorm_calls.append)
    monkeypatch.setattr(commission, "ActionPointHandler", unexpected_action_point)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.DORM_RECOVERED
    assert dorm_calls == [10]
    assert store.results == [("ap", "dorm_fallback")]
    assert store.events == ["read", "record_result"]


def test_unknown_state_bootstraps_from_ocr_before_ap_decision(monkeypatch):
    events: list[str] = []
    store = _Store(order=events)
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3),
        events=events,
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append, events=events)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.AP_RECOVERED
    assert store.observations[0][1:] == (4, "game_ocr", None)
    assert events.index("enter") < events.index("select_oil") < events.index("ocr")
    assert events.index("ocr") < events.index("record_observation") < events.index("read", 1)
    assert events.index("read", 1) < events.index("purchase:4")
    assert events.index("ocr") < events.index("purchase:4")
    assert store.events.index("record_observation") < store.events.index("read", 1)
    assert dorm_calls == []


def test_oil_selection_failure_blocks_before_ocr_redis_or_mutation(monkeypatch):
    events: list[str] = []
    store = _Store(order=events)
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3),
        select_oil=False,
        events=events,
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append, events=events)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.BLOCKED
    assert ap.select_oil_calls == 1
    assert "ocr" not in events
    assert store.observations == []
    assert ap.purchase_calls == 0
    assert dorm_calls == []


def test_unknown_state_bootstraps_zero_then_allows_dorm(monkeypatch):
    store = _Store()
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.UNAVAILABLE, before=0),
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.DORM_RECOVERED
    assert store.observations == [("ap", 0, "game_ocr", "ap_unavailable")]
    assert store.read_calls >= 2
    assert ap.purchase_calls == 0
    assert dorm_calls == [10]


def test_unavailable_cache_fails_closed_without_ap_or_dorm(monkeypatch):
    store = _Store(status="unavailable")
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.BLOCKED
    assert ap.entered == 0
    assert ap.purchase_calls == 0
    assert dorm_calls == []


def test_ocr_mismatch_updates_canonical_state_before_purchase(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, before=3, after=2))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.AP_RECOVERED
    assert store.observations[0][1:] == (3, "game_ocr", None)
    assert ap.remaining_args == [3]
    assert dorm_calls == []


def test_unknown_pre_mutation_ocr_blocks_without_state_fabrication(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.UNKNOWN, before=4),
        ocr_remaining=None,
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.BLOCKED
    assert ap.purchase_calls == 0
    assert store.observations == []
    assert dorm_calls == []
    assert store.state.status == "confirmed"
    assert store.state.remaining == 4


def test_ambiguous_postcondition_invalidates_confirmed_state_and_never_uses_dorm(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.UNKNOWN, before=4, clicks=1),
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.AMBIGUOUS_MUTATION
    assert ap.purchase_calls == 1
    assert dorm_calls == []
    assert store.invalidations == [("ap", "ambiguous_ap_purchase")]
    assert store.read("ap").status == "unknown"


def test_purchase_without_ap_postcondition_invalidates_after_one_click(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(
        _purchase(
            EmergencyActionPointPurchaseStatus.PURCHASED,
            before=4,
            after=3,
            ap_before=200,
            ap_after=200,
            ap_gain=0,
        )
    )
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    outcome = handler._recover_commission_oil_overflow()

    assert outcome is commission.CommissionRecoveryOutcome.AMBIGUOUS_MUTATION
    assert ap.purchase_calls == 1
    assert store.invalidations == [("ap", "ambiguous_ap_purchase")]
    assert dorm_calls == []


@pytest.mark.parametrize(
    "status",
    [
        EmergencyActionPointPurchaseStatus.FAILED,
        EmergencyActionPointPurchaseStatus.UNSAFE,
        EmergencyActionPointPurchaseStatus.INSUFFICIENT_OIL,
    ],
)
def test_failed_ap_outcomes_fail_closed_without_dorm_or_blind_retry(monkeypatch, status):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(status, before=4, clicks=0))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    first = handler._recover_commission_oil_overflow()
    second = handler._recover_commission_oil_overflow()

    assert first is commission.CommissionRecoveryOutcome.BLOCKED
    assert second is commission.CommissionRecoveryOutcome.BLOCKED
    assert ap.purchase_calls == 1
    assert dorm_calls == []
    assert store.state.status == "confirmed"
    assert store.state.remaining == 4


def test_repeated_overflow_after_successful_purchase_has_no_second_purchase_or_dorm(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)

    first = handler._recover_commission_oil_overflow()
    second = handler._recover_commission_oil_overflow()

    assert first is commission.CommissionRecoveryOutcome.AP_RECOVERED
    assert second is commission.CommissionRecoveryOutcome.BLOCKED
    assert ap.purchase_calls == 1
    assert dorm_calls == []
    assert store.state.remaining == 3


def test_commission_receive_does_not_retry_after_fail_closed_recovery(monkeypatch):
    store = _Store(status="unavailable")
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    handler = _commission(monkeypatch, store, ap, lambda _amount: None)
    calls = 0

    def oil_maxed():
        nonlocal calls
        calls += 1
        raise OilMaxed

    handler._commission_receive = oil_maxed

    with pytest.raises(RequestHumanTakeover):
        handler.commission_receive()

    assert calls == 1
    assert ap.purchase_calls == 0


def test_commission_receive_retries_once_after_successful_ap_recovery(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    handler = _commission(monkeypatch, store, ap, lambda _amount: None)
    calls = 0

    def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OilMaxed
        return True

    handler._commission_receive = receive

    assert handler.commission_receive() is True
    assert calls == 2
    assert ap.purchase_calls == 1


def test_commission_receive_fails_closed_on_second_oil_maxed_after_ap_recovery(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(_purchase(EmergencyActionPointPurchaseStatus.PURCHASED, after=3))
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, ap, dorm_calls.append)
    calls = 0

    def receive():
        nonlocal calls
        calls += 1
        raise OilMaxed

    handler._commission_receive = receive

    with pytest.raises(RequestHumanTakeover):
        handler.commission_receive()

    assert calls == 2
    assert ap.purchase_calls == 1
    assert dorm_calls == []
    assert store.state.remaining == 3


def test_commission_receive_retries_once_after_confirmed_zero_dorm(monkeypatch):
    store = _Store(status="confirmed", remaining=0)
    dorm_calls: list[int] = []
    handler = _commission(monkeypatch, store, None, dorm_calls.append)
    calls = 0

    def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OilMaxed
        return True

    handler._commission_receive = receive

    assert handler.commission_receive() is True
    assert calls == 2
    assert dorm_calls == [10]


def test_commission_receive_does_not_retry_after_ambiguous_purchase(monkeypatch):
    store = _Store(status="confirmed", remaining=4)
    ap = _ActionPoint(
        _purchase(EmergencyActionPointPurchaseStatus.UNKNOWN, before=4, clicks=1),
    )
    handler = _commission(monkeypatch, store, ap, lambda _amount: None)
    calls = 0

    def receive():
        nonlocal calls
        calls += 1
        raise OilMaxed

    handler._commission_receive = receive

    with pytest.raises(RequestHumanTakeover):
        handler.commission_receive()

    assert calls == 1
    assert ap.purchase_calls == 1
