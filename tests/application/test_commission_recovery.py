from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from module.application.commission_recovery import (
    CommissionRecoveryStore,
    next_en_weekly_reset,
)
from module.application.runtime_cache import RuntimeCacheHealth, RuntimeCacheStatus


class _MemoryCache:
    def __init__(self, status: RuntimeCacheStatus = RuntimeCacheStatus.READY):
        self.status = status
        self.values: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, datetime | None]] = []
        self.closed = False

    def health(self) -> RuntimeCacheHealth:
        return RuntimeCacheHealth(self.status)

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def set(self, key: str, value: bytes, *, expires_at: datetime | None = None) -> None:
        self.values[key] = value
        self.set_calls.append((key, value, expires_at))

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None

    def close(self) -> None:
        self.closed = True


def test_en_weekly_reset_uses_server_boundary_not_local_midnight():
    before_reset = datetime(2026, 9, 20, 16, 59, 59, tzinfo=UTC)
    at_reset = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)

    assert next_en_weekly_reset(before_reset) == datetime(2026, 9, 21, 7, tzinfo=UTC)
    assert next_en_weekly_reset(at_reset) == datetime(2026, 9, 28, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    ("remaining", "cost", "gain"),
    [
        (5, 1000, 100),
        (4, 1000, 100),
        (3, 2000, 100),
        (2, 2000, 100),
        (1, 4000, 100),
        (0, None, None),
    ],
)
def test_record_observation_persists_canonical_cost_and_gain(remaining, cost, gain):
    now = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
    cache = _MemoryCache()
    store = CommissionRecoveryStore(cache, now=lambda: now)

    state = store.record_observation("ap", remaining)

    assert state.status == "confirmed"
    assert state.remaining == remaining
    assert state.used == 5 - remaining
    assert state.next_oil_cost == cost
    assert state.next_ap_gain == gain
    assert state.reset_at == datetime(2026, 9, 21, 7, tzinfo=UTC)
    assert cache.set_calls[0][0] == "commission/recovery/ap"
    assert cache.set_calls[0][2] == state.reset_at


def test_cache_miss_is_unknown_and_never_false_zero():
    store = CommissionRecoveryStore(
        _MemoryCache(),
        now=lambda: datetime(2026, 9, 20, 16, tzinfo=UTC),
    )

    state = store.read("ap")

    assert state.status == "unknown"
    assert state.remaining is None
    assert state.used is None
    assert state.next_oil_cost is None


def test_stale_state_reconciles_to_unknown_after_reset():
    now = datetime(2026, 9, 20, 16, tzinfo=UTC)
    current = [now]
    cache = _MemoryCache()
    store = CommissionRecoveryStore(cache, now=lambda: current[0])
    store.record_observation("ap", 4)

    current[0] = datetime(2026, 9, 21, 7, tzinfo=UTC) + timedelta(seconds=1)
    state = store.read("ap")

    assert state.status == "unknown"
    assert state.remaining is None


def test_profile_scope_is_distinct():
    assert CommissionRecoveryStore.key("ap") != CommissionRecoveryStore.key("second")
    assert CommissionRecoveryStore.key("profile with space") == (
        "commission/recovery/profile%20with%20space"
    )


def test_unavailable_cache_is_explicit_and_does_not_write():
    cache = _MemoryCache(RuntimeCacheStatus.UNAVAILABLE)
    store = CommissionRecoveryStore(cache, now=lambda: datetime.now(UTC))

    state = store.record_observation("ap", 3)

    assert state.status == "unavailable"
    assert state.remaining == 3
    assert state.error == RuntimeCacheStatus.UNAVAILABLE.value
    assert cache.set_calls == []
