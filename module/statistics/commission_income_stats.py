# -*- coding: utf-8 -*-
"""Агрегация комиссионных наград из production PostgreSQL."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from module.application.runtime_storage import get_runtime_storage
from module.statistics.postgresql_stats import get_commission_entries

COMMISSION_TRACKED_ITEMS = ['Gem', 'Cube', 'Chip', 'Oil', 'Coin']

COMMISSION_ITEM_META = {
    'Gem':  {'color': '#ff4757', 'order': 0},
    'Cube': {'color': '#3742fa', 'order': 1},
    'Chip': {'color': '#8854d0', 'order': 2},
    'Oil':  {'color': '#2d3436', 'order': 3},
    'Coin': {'color': '#ffa502', 'order': 4},
}

COMMISSION_ITEM_NAME_MAP = {
    'Gems': 'Gem',
    'Cubes': 'Cube',
    'CognitiveChips': 'Chip',
    'Coins': 'Coin',
}


def _parse_ts(ts_str: str, storage=None) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(ts_str)
    except (TypeError, ValueError):
        return None
    return (storage or get_runtime_storage()).to_runtime_timezone(parsed)


def _filter_entries_by_period(
    entries: List[Dict[str, Any]],
    period: str,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Отфильтровать записи по дню, неделе или месяцу."""
    if now is None:
        now = get_runtime_storage().current_datetime()

    if period == 'month':
        return entries

    storage = get_runtime_storage()
    filtered = []
    for entry in entries:
        ts = _parse_ts(entry.get('ts', ''), storage)
        if ts is None:
            continue
        if period == 'day':
            if ts.date() == now.date():
                filtered.append(entry)
        elif period == 'week':
            week_start = now.date() - timedelta(days=now.weekday())
            if week_start <= ts.date() <= now.date():
                filtered.append(entry)

    return filtered


def get_commission_income_summary(
    instance: str,
    period: str = 'month',
    year: int = None,
    month: int = None,
) -> Dict[str, Any]:
    """Вернуть агрегированную сводку комиссионных наград."""
    now = get_runtime_storage().current_datetime()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    entries = get_commission_entries(instance, year, month)
    if period == 'week':
        week_start = now.date() - timedelta(days=now.weekday())
        if (week_start.year, week_start.month) != (year, month):
            entries = get_commission_entries(
                instance, week_start.year, week_start.month
            ) + entries
    filtered = _filter_entries_by_period(entries, period, now)

    totals: Dict[str, int] = {}
    counts: Dict[str, int] = {}
    total_commissions = 0

    for entry in filtered:
        total_commissions += entry.get('commission_count', 1)
        items = entry.get('items', {})
        for item_name, amount in items.items():
            mapped_name = COMMISSION_ITEM_NAME_MAP.get(item_name, item_name)
            if mapped_name not in COMMISSION_TRACKED_ITEMS:
                continue
            totals[mapped_name] = totals.get(mapped_name, 0) + int(amount)
            counts[mapped_name] = counts.get(mapped_name, 0) + 1

    items_summary = {}
    detail_rows = []
    for item_name in COMMISSION_TRACKED_ITEMS:
        total = totals.get(item_name, 0)
        count = counts.get(item_name, 0)
        avg = round(total / count, 1) if count > 0 else 0
        meta = COMMISSION_ITEM_META.get(item_name, {'color': '#888', 'order': 99})

        items_summary[item_name] = {
            'total': total,
            'count': count,
            'avg': avg,
        }
        detail_rows.append({
            'name': item_name,
            'color': meta['color'],
            'total': total,
            'count': count,
            'avg': avg,
        })

    return {
        'period': period,
        'total_commissions': total_commissions,
        'items': items_summary,
        'detail_rows': detail_rows,
    }


def get_recent_commission_entries(
    instance: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Вернуть последние записи комиссий в обратном порядке времени."""
    storage = get_runtime_storage()
    now = storage.current_datetime()
    all_entries = []
    year = now.year
    month = now.month
    for _ in range(3):
        entries = get_commission_entries(instance, year, month)
        for entry in entries:
            ts = _parse_ts(entry.get('ts', ''), storage)
            if ts is not None:
                all_entries.append(entry)
        if len(all_entries) >= limit:
            break
        month -= 1
        if month == 0:
            year -= 1
            month = 12

    all_entries.sort(key=lambda e: e.get('ts', ''), reverse=True)
    return all_entries[:limit]
