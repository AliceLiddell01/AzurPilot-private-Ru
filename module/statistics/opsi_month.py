"""Модуль ежемесячной статистики Operation Siren (Большой мир).
Считывает боевые данные из зашифрованной базы SQLite,
рассчитывает сводные показатели эффективности прокачки, расхода ресурсов и числа боев.
"""

# Этот файл предназначен для статистического анализа месячной эффективности прокачки и расхода ресурсов в Operation Siren.
# Он читает статистику из зашифрованной базы SQLite и рассчитывает сводные и подробные показатели.
from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, Optional

from module.statistics.postgresql_stats import get_monthly_stats


class OpsiMonthStats:
    def __init__(self, instance_name: str | None = None) -> None:
        self._instance_name = instance_name or "default"

    def summary(
        self, year: int | None = None, month: int | None = None
    ) -> Dict[str, Any]:
        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            month = now.month
        key = f"{year:04d}-{month:02d}"

        # Читаем данные из базы данных
        data = get_monthly_stats(self._instance_name, year, month)

        total = int(data.get("battle_count", 0))
        akashi = int(data.get("akashi_encounters", 0))
        siren_research_devices = int(data["siren_research_devices"]["cl1"])

        return {
            "month": key,
            "total_battles": total,
            "akashi_encounters": akashi,
            "siren_research_devices": siren_research_devices,
            "raw": data,
        }

    def get_detailed_summary(
        self, year: int | None = None, month: int | None = None
    ) -> Dict[str, Any]:
        """
        Возвращает подробную статистическую сводку, содержащую все расчетные показатели.
        """
        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            month = now.month
        key = f"{year:04d}-{month:02d}"

        # Читаем данные из базы данных
        data = get_monthly_stats(self._instance_name, year, month)

        # Базовые данные
        battle_count = int(data.get("battle_count", 0))
        akashi_encounters = int(data.get("akashi_encounters", 0))
        akashi_ap = int(data.get("akashi_ap", 0))
        siren_research_devices = int(data["siren_research_devices"]["cl1"])

        # Вычисляем производные показатели
        battle_rounds = battle_count // 2
        sortie_cost = battle_rounds * 120

        akashi_probability = (
            round(akashi_encounters / battle_rounds, 4) if battle_rounds > 0 else 0.0
        )
        siren_research_probability = (
            round(siren_research_devices / battle_rounds, 4)
            if battle_rounds > 0
            else 0.0
        )
        average_stamina = (
            round(akashi_ap / akashi_encounters, 2) if akashi_encounters > 0 else 0.0
        )

        return {
            "month": key,
            "battle_count": battle_count,
            "battle_rounds": battle_rounds,
            "sortie_cost": sortie_cost,
            "akashi_encounters": akashi_encounters,
            "akashi_probability": akashi_probability,
            "siren_research_devices": siren_research_devices,
            "siren_research_probability": siren_research_probability,
            "average_stamina": average_stamina,
            "net_stamina_gain": akashi_ap,
        }


_singleton: Dict[str, OpsiMonthStats] = {}


def get_opsi_stats(instance_name: str | None = None) -> OpsiMonthStats:
    global _singleton
    key = instance_name or "default"
    if key not in _singleton:
        _singleton[key] = OpsiMonthStats(instance_name=instance_name)
    return _singleton[key]


def compute_monthly_cl1_akashi_ap(
    year: int | None = None,
    month: int | None = None,
    campaign: str = "opsi_akashi",
    instance_name: str | None = None,
) -> int:
    """
    Рассчитывает суммарный объем очков действия (AP), купленный в магазине Акаси за указанный месяц.
    """
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    key_prefix = f"{year:04d}-{month:02d}"

    instance_name = instance_name or "default"
    data = get_monthly_stats(instance_name, year, month)

    return int(data.get("akashi_ap", 0))


def get_ap_timeline(
    year: int | None = None, month: int | None = None, instance_name: str | None = None
) -> list:
    """
    Получает временной ряд изменения очков действия (фактический остаток выносливости) для построения графика расхода.

    Возвращает упорядоченный по времени список точек данных, каждая из которых содержит:
    - ts: метка времени в формате ISO
    - ap: остаток очков действия на тот момент
    - source: источник данных (cl1 / meow)

    Args:
        year: Год, по умолчанию текущий
        month: Месяц, по умолчанию текущий
        instance_name: Имя инстанса

    Returns:
        list[dict]: Точки данных временного ряда
    """
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    key_prefix = f"{year:04d}-{month:02d}"

    instance_name = instance_name or "default"
    data = get_monthly_stats(instance_name, year, month)

    snapshots = data.get("ap_snapshots", [])
    if not snapshots:
        return []

    # Сортируем по времени
    try:
        snapshots_sorted = sorted(snapshots, key=lambda e: e.get("ts", ""))
    except Exception:
        snapshots_sorted = snapshots

    return snapshots_sorted


def get_coins_timeline(
    year: int | None = None, month: int | None = None, instance_name: str | None = None
) -> list:
    """
    Получает временной ряд изменения жетонов (желтые/фиолетовые монеты снабжения/обмена) для построения графика.

    Возвращает упорядоченный по времени список точек данных, каждая из которых содержит:
    - ts: метка времени в формате ISO
    - yellow_coins: количество жетонов снабжения (желтые монеты) на тот момент
    - purple_coins: количество жетонов особого обмена (фиолетовые монеты) на тот момент
    - source: источник данных (cl1 / meow / other)

    Args:
        year: Год, по умолчанию текущий
        month: Месяц, по умолчанию текущий
        instance_name: Имя инстанса

    Returns:
        list[dict]: Точки данных временного ряда
    """
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    key_prefix = f"{year:04d}-{month:02d}"

    instance_name = instance_name or "default"
    data = get_monthly_stats(instance_name, year, month)

    snapshots = data.get("coins_snapshots", [])
    if not snapshots:
        return []

    try:
        return sorted(snapshots, key=lambda e: e.get("ts", ""))
    except Exception:
        return snapshots


__all__ = [
    "get_opsi_stats",
    "OpsiMonthStats",
    "compute_monthly_cl1_akashi_ap",
    "get_ap_timeline",
    "get_coins_timeline",
    "get_asset_timeline",
    "get_resource_timeline",
]


def get_resource_timeline(
    instance_name: str | None = None, limit: int = 500
) -> list:
    """
    Получает моментальные снимки временного ряда всех ресурсов для построения графика динамики.

    Возвращает упорядоченный по времени список точек данных, каждая из которых содержит:
    - ts: метка времени в формате ISO
    - oil, coin, gem, pt, cube, core, medal, merit, guild_coin,
      action_point, yellow_coin, purple_coin: числовые значения каждого ресурса (может быть None)

    Args:
        instance_name: Имя инстанса
        limit: Максимальное количество возвращаемых записей

    Returns:
        list[dict]: Точки данных временного ряда
    """
    from module.statistics.resource_stats import get_resource_timeline as _get_timeline

    instance_name = instance_name or "default"
    return _get_timeline(instance=instance_name, limit=limit)


def get_asset_timeline(
    year: int | None = None, month: int | None = None, instance_name: str | None = None
) -> list:
    """Возвращает временную шкалу активов из снимков AP."""
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    key_prefix = f"{year:04d}-{month:02d}"

    data = get_monthly_stats(instance_name or "default", year, month)
    snapshots = data.get("ap_snapshots", [])
    return sorted([s for s in snapshots if s.get("ts")], key=lambda e: e.get("ts", ""))
