from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from module.application.errors import StorageError
from module.application.runtime_storage import OpsiItemProjection
from module.log_res.log_res import LogRes
from module.statistics import commission_income_stats
from module.statistics.azurstats import AzurStats


def _runtime(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        current_datetime=lambda: now,
        to_runtime_timezone=lambda value: value.astimezone(now.tzinfo),
    )


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


def test_commission_timestamp_does_not_hide_storage_failure(monkeypatch):
    class BrokenStorage:
        @staticmethod
        def to_runtime_timezone(_value):
            raise StorageError("Синтетический сбой хранилища.")

    monkeypatch.setattr(
        commission_income_stats,
        "get_runtime_storage",
        lambda: BrokenStorage(),
    )

    with pytest.raises(StorageError, match="Синтетический сбой"):
        commission_income_stats._parse_ts("2026-08-23T12:00:00+00:00")


def test_opsi_projection_skips_legacy_row_without_timestamp(monkeypatch):
    rows = (
        OpsiItemProjection(
            None,
            "legacy-without-time",
            "opsi_meowfficer_farming",
            "OperationCoin",
            10,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        OpsiItemProjection(
            datetime(2026, 8, 23, 12, tzinfo=UTC),
            "dated-row",
            "opsi_meowfficer_farming",
            "OperationCoin",
            20,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        "module.statistics.azurstats.get_runtime_storage",
        lambda: SimpleNamespace(opsi_items=lambda *_args, **_kwargs: rows),
    )

    loaded = AzurStats._load_local_opsi_items()

    assert [row["imgid"] for row in loaded] == ["dated-row"]


def test_resource_snapshot_is_not_written_without_resolved_values(monkeypatch):
    config = SimpleNamespace(
        config_name="profile",
        modified={},
        data={"Dashboard": {"Unknown": {"Value": "not-a-number"}}},
    )
    log_res = LogRes(config)
    log_res.__dict__["groups"] = {"Unknown": {}}

    monkeypatch.setattr(
        "module.log_res.log_res.get_runtime_storage",
        lambda: pytest.fail("Пустой снимок ресурсов не должен записываться."),
    )

    log_res._record_all_resource_snapshot()
