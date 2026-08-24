"""Совместимые пользовательские проекции production PostgreSQL."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from module.application.runtime_storage import CommissionEntry, get_runtime_storage
from module.application.storage_models import MonthlyMetric


def _timestamp(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def get_monthly_stats(instance: str, year: int, month: int) -> dict[str, Any]:
    """Собрать прежнюю форму чтения из нормализованных PostgreSQL-таблиц."""

    projection = get_runtime_storage().monthly_statistics(instance, year, month)
    currency_groups: dict[tuple[datetime | None, str], dict[str, Any]] = {}
    yellow_snapshots = []
    for item in projection.currency_snapshots:
        if item.currency_code == "yellow_coin":
            yellow_snapshots.append(
                {
                    "ts": _timestamp(item.observed_at),
                    "yellow_coin": item.amount,
                    "source": item.source,
                }
            )
        if item.source == "dashboard":
            continue
        key = (item.observed_at, item.source)
        row = currency_groups.setdefault(
            key,
            {"ts": _timestamp(item.observed_at), "source": item.source},
        )
        if item.currency_code == "yellow_coin":
            row["yellow_coins"] = item.amount
        elif item.currency_code == "purple_coin":
            row["purple_coins"] = item.amount

    hazards = {}
    for item in projection.meow_hazards:
        hazards[str(item.hazard_level)] = {
            "battle_raw_count": item.battle_count,
            "effective_rounds": float(item.effective_rounds),
            "round_times": [float(value) for value in item.round_times],
            "battle_times": [float(value) for value in item.battle_times],
        }
    siren_meow = {
        str(item.hazard_level): item.siren_research_devices
        for item in projection.meow_hazards
        if item.siren_research_devices
    }
    return {
        "battle_count": int(projection.metric(MonthlyMetric.BATTLE_COUNT)),
        "akashi_encounters": int(projection.metric(MonthlyMetric.AKASHI_ENCOUNTERS)),
        "akashi_ap": int(projection.metric(MonthlyMetric.AKASHI_AP)),
        "akashi_ap_entries": [
            {
                "ts": _timestamp(item.observed_at),
                "amount": item.amount,
                "base": item.base_amount,
                "count": item.purchase_count,
                "source": item.source,
            }
            for item in projection.ap_purchases
        ],
        "ap_snapshots": [
            {
                "ts": _timestamp(item.observed_at),
                "ap": item.ap,
                "ap_total": item.ap_total,
                "asset": float(item.asset) if item.asset is not None else None,
                "yellow_coin": item.yellow_coin,
                "distance": item.distance,
                "source": item.source,
            }
            for item in projection.ap_snapshots
        ],
        "yellow_coin_snapshots": yellow_snapshots,
        "coins_snapshots": list(currency_groups.values()),
        "meow_battle_raw_count": int(
            projection.metric(MonthlyMetric.MEOW_BATTLE_RAW_COUNT)
        ),
        "meow_battle_count": float(projection.metric(MonthlyMetric.MEOW_BATTLE_COUNT)),
        "meow_round_times": [
            {
                "duration": float(item.duration_seconds),
                "hazard_level": item.hazard_level,
            }
            for item in projection.meow_timings
            if item.sample_kind == "round"
        ],
        "meow_battle_times": [
            float(item.duration_seconds)
            for item in projection.meow_timings
            if item.sample_kind == "battle"
        ],
        "meow_hazard_stats": hazards,
        "siren_research_devices": {
            "cl1": projection.siren_cl1_devices,
            "meow": siren_meow,
        },
    }


def get_meow_stats(
    instance: str,
    year: int | None = None,
    month: int | None = None,
    *,
    hazard_level: int | None = None,
) -> dict[str, Any]:
    """Вернуть детерминированную Meow-проекцию без догадок по отсутствующим данным."""

    now = get_runtime_storage().current_datetime()
    year = now.year if year is None else year
    month = now.month if month is None else month
    data = get_monthly_stats(instance, year, month)
    round_entries = data["meow_round_times"]
    round_times = [float(item["duration"]) for item in round_entries]
    battle_times = [float(value) for value in data["meow_battle_times"]]
    hazards = data["meow_hazard_stats"]
    by_hazard = {}
    siren_by_hazard = data["siren_research_devices"]["meow"]
    levels = (
        {int(key) for key in hazards}
        | {int(key) for key in siren_by_hazard}
        | {
            int(item["hazard_level"])
            for item in round_entries
            if item.get("hazard_level") is not None
        }
        | {3, 5}
    )
    for level in sorted(levels):
        bucket = hazards.get(str(level), {})
        level_rounds = [
            float(item["duration"])
            for item in round_entries
            if item.get("hazard_level") == level
        ]
        level_battles = bucket.get("battle_times", [])
        effective = float(bucket.get("effective_rounds", 0) or 0)
        siren = int(siren_by_hazard.get(str(level), 0) or 0)
        by_hazard[str(level)] = {
            "hazard_level": level,
            "battle_count": int(bucket.get("battle_raw_count", 0) or 0),
            "effective_rounds": round(effective, 2),
            "avg_round_time": round(mean(level_rounds), 2) if level_rounds else 0.0,
            "avg_battle_time": round(mean(level_battles), 2) if level_battles else 0.0,
            "sample_count": len(level_rounds),
            "source": "exact" if bucket or level_rounds else "none",
            "siren_research_devices": siren,
            "siren_research_rate": round(siren / effective, 4) if effective else 0.0,
        }
    effective_rounds = float(data["meow_battle_count"])
    result = {
        "month": f"{year:04d}-{month:02d}",
        "battle_count": int(data["meow_battle_raw_count"]),
        "effective_rounds": round(effective_rounds, 2),
        "round_times": round_entries,
        "avg_round_time": round(mean(round_times), 2) if round_times else 0.0,
        "battle_times": battle_times,
        "avg_battle_time": round(mean(battle_times), 2) if battle_times else 0.0,
        "siren_research_devices": sum(int(value) for value in siren_by_hazard.values()),
        "siren_research_rate": 0.0,
        "by_hazard": by_hazard,
    }
    if effective_rounds:
        result["siren_research_rate"] = round(
            result["siren_research_devices"] / effective_rounds, 4
        )
    if hazard_level is not None:
        selected = by_hazard.get(str(hazard_level))
        if selected is None:
            result.update(
                battle_count=0,
                effective_rounds=0.0,
                avg_round_time=0.0,
                avg_battle_time=0.0,
                siren_research_devices=0,
                siren_research_rate=0.0,
            )
        else:
            result.update(
                battle_count=selected["battle_count"],
                effective_rounds=selected["effective_rounds"],
                avg_round_time=selected["avg_round_time"],
                avg_battle_time=selected["avg_battle_time"],
                siren_research_devices=selected["siren_research_devices"],
                siren_research_rate=selected["siren_research_rate"],
            )
    return result


def get_commission_entries(
    instance: str, year: int, month: int
) -> list[dict[str, Any]]:
    """Вернуть записи комиссии за настроенный календарный месяц runtime."""

    entries = get_runtime_storage().commission_entries_for_month(instance, year, month)
    return [_commission_dict(entry) for entry in entries]


def _commission_dict(entry: CommissionEntry) -> dict[str, Any]:
    return {
        "ts": _timestamp(entry.observed_at),
        "items": {item.item_code: item.amount for item in entry.items},
        "commission_count": entry.commission_count,
    }


def get_commission_reward_stats(instance: str) -> dict[str, dict[str, int]]:
    """Суммировать награды комиссии за текущие день, неделю и месяц."""

    storage = get_runtime_storage()
    now = storage.current_datetime()
    month_entries = get_commission_entries(instance, now.year, now.month)
    week_start = now.date() - timedelta(days=now.weekday())
    if week_start.month != now.month or week_start.year != now.year:
        previous = now.replace(day=1) - timedelta(days=1)
        month_entries += get_commission_entries(instance, previous.year, previous.month)
    result = {
        "today": defaultdict(int),
        "week": defaultdict(int),
        "month": defaultdict(int),
    }
    for entry in month_entries:
        raw_timestamp = entry["ts"]
        if not raw_timestamp:
            continue
        timestamp = storage.to_runtime_timezone(datetime.fromisoformat(raw_timestamp))
        for item, value in entry["items"].items():
            if timestamp.year == now.year and timestamp.month == now.month:
                result["month"][item] += int(value)
            if timestamp.date() == now.date():
                result["today"][item] += int(value)
            if week_start <= timestamp.date() <= now.date():
                result["week"][item] += int(value)
    for period in result.values():
        period.setdefault("Gem", 0)
        period.setdefault("Cube", 0)
    return {key: dict(value) for key, value in result.items()}


__all__ = [
    "get_commission_entries",
    "get_commission_reward_stats",
    "get_meow_stats",
    "get_monthly_stats",
]
