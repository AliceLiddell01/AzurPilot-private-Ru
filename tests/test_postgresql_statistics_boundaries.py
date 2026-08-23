from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from module.statistics import commission_income_stats


def _runtime(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(current_datetime=lambda: now)


def test_week_summary_loads_previous_calendar_month(monkeypatch):
    now = datetime(2026, 3, 1, 12, tzinfo=ZoneInfo("Asia/Novosibirsk"))
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        commission_income_stats,
        "get_runtime_storage",
        lambda: _runtime(now),
    )
    monkeypatch.setattr(
        commission_income_stats,
        "get_commission_entries",
        lambda _instance, year, month: calls.append((year, month)) or [],
    )

    commission_income_stats.get_commission_income_summary(
        "profile", period="week", year=2026, month=3
    )

    assert calls == [(2026, 3), (2026, 2)]


def test_recent_entries_walk_three_adjacent_calendar_months(monkeypatch):
    now = datetime(2026, 3, 1, 12, tzinfo=ZoneInfo("Asia/Novosibirsk"))
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        commission_income_stats,
        "get_runtime_storage",
        lambda: _runtime(now),
    )
    monkeypatch.setattr(
        commission_income_stats,
        "get_commission_entries",
        lambda _instance, year, month: calls.append((year, month)) or [],
    )

    commission_income_stats.get_recent_commission_entries("profile", limit=10)

    assert calls == [(2026, 3), (2026, 2), (2026, 1)]
