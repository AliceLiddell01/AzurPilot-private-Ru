from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from module.application.commission_recovery import CommissionRecoveryState
from module.webui import api


class _Store:
    def __init__(self, status="confirmed"):
        self.closed = False
        self.status = status

    def read(self, profile):
        if self.status == "unknown":
            return CommissionRecoveryState(
                profile=profile,
                status="unknown",
                remaining=None,
                used=None,
                next_oil_cost=None,
                next_ap_gain=None,
                confirmed_at=None,
                reset_at=datetime(2026, 9, 21, 7, tzinfo=UTC),
                source=None,
                last_result=None,
                cache_status="READY",
            )
        if self.status == "unavailable":
            return CommissionRecoveryState(
                profile=profile,
                status="unavailable",
                remaining=None,
                used=None,
                next_oil_cost=None,
                next_ap_gain=None,
                confirmed_at=None,
                reset_at=datetime(2026, 9, 21, 7, tzinfo=UTC),
                source=None,
                last_result=None,
                cache_status="UNAVAILABLE",
                error="UNAVAILABLE",
            )
        return CommissionRecoveryState(
            profile=profile,
            status="confirmed",
            remaining=4,
            used=1,
            next_oil_cost=1000,
            next_ap_gain=100,
            confirmed_at=datetime(2026, 9, 20, 16, tzinfo=UTC),
            reset_at=datetime(2026, 9, 21, 7, tzinfo=UTC),
            source="game_ocr",
            last_result="ap_purchase",
            cache_status="READY",
        )

    def close(self):
        self.closed = True


def test_commission_recovery_api_is_read_only_and_profile_scoped(monkeypatch):
    store = _Store()
    monkeypatch.setattr(
        api.CommissionRecoveryStore,
        "from_environment",
        classmethod(lambda cls: store),
    )

    response = api.api_commission_recovery(SimpleNamespace(query_params={"instance": "ap"}))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["success"] is True
    assert payload["data"]["profile"] == "ap"
    assert payload["data"]["status"] == "confirmed"
    assert payload["data"]["remaining"] == 4
    assert store.closed is True


def test_commission_recovery_api_rejects_invalid_profile_without_cache_access(monkeypatch):
    called = False

    def fail_factory():
        nonlocal called
        called = True
        raise AssertionError("cache не должен создаваться")

    monkeypatch.setattr(
        api.CommissionRecoveryStore,
        "from_environment",
        classmethod(lambda cls: fail_factory()),
    )

    response = api.api_commission_recovery(SimpleNamespace(query_params={"instance": "../secret"}))

    assert response.status_code == 400
    assert json.loads(response.body) == {"success": False, "error": "INVALID_PROFILE"}
    assert called is False


def test_commission_recovery_api_preserves_unknown_and_unavailable_states(monkeypatch):
    for status in ("unknown", "unavailable"):
        store = _Store(status)
        monkeypatch.setattr(
            api.CommissionRecoveryStore,
            "from_environment",
            classmethod(lambda cls, store=store: store),
        )

        response = api.api_commission_recovery(
            SimpleNamespace(query_params={"instance": "ap"})
        )

        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["success"] is True
        assert payload["data"]["status"] == status
        assert payload["data"]["remaining"] is None
        assert store.closed is True
