"""Единая точка записи runtime-статистики Operation Siren."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from module.application.errors import StorageError
from module.application.runtime_storage import get_runtime_storage
from module.application.storage_models import MonthlyMetric
from module.logger import logger


# Код задач сообщает только доменные события; детали хранения остаются здесь.
CL1_TASK = "OpsiHazard1Leveling"
MEOW_TASK = "OpsiMeowfficerFarming"
MEOW_HAZARD_LEVELS = {2, 3, 4, 5, 6}


def instance_name_from_config(config: Any, default: str = "default") -> str:
    return getattr(config, "config_name", None) or default


def battle_source_from_config(config: Any) -> str | None:
    """Вернуть источник статистики для текущей задачи."""
    command = getattr(getattr(config, "task", None), "command", None)
    if command == CL1_TASK:
        return "cl1"
    if command == MEOW_TASK:
        return "meow"
    return None


def start_battle_timer(config: Any) -> str | None:
    """Начать замер боя и вернуть источник для завершения замера."""
    source = battle_source_from_config(config)
    if source is None:
        return None

    try:
        from module.statistics.ship_exp_stats import get_ship_exp_stats

        get_ship_exp_stats(instance_name=instance_name_from_config(config)).on_battle_start()
        return source
    except Exception:
        logger.debug(f"[Статистика — Operation Siren] Не удалось запустить таймер боя {source}: ошибка", exc_info=True)
        return None


def finish_battle_timer(config: Any, source: str | None) -> float | None:
    """Завершить ранее начатый замер отдельного боя."""
    if source not in {"cl1", "meow"}:
        return None

    try:
        from module.statistics.ship_exp_stats import get_ship_exp_stats

        return get_ship_exp_stats(
            instance_name=instance_name_from_config(config)
        ).on_battle_end(source=source)
    except Exception:
        logger.debug(f"[Статистика — Operation Siren] Не удалось завершить замер боя {source}: ошибка", exc_info=True)
        return None


def refresh_action_point(main: Any) -> bool:
    """Обновить кэш очков действия Operation Siren."""
    if hasattr(main, "get_current_ap"):
        main.get_current_ap()
        return True

    main.action_point_enter()
    main.action_point_safe_get()
    main.action_point_quit()
    return True


def record_ap_snapshot(config: Any, ap_current: int, source: str, distance: int = None, ap_total: int = None) -> None:
    """Записать снимок очков действия с явным источником."""
    get_runtime_storage().record_ap_snapshot(
        instance_name_from_config(config),
        ap_current,
        source=source,
        distance=distance,
        ap_total=ap_total,
    )


def record_cl1_auto_search_battle(
    config: Any,
    cl1_battle_count: int,
    round_started_at: float | int | None,
) -> float | int | None:
    """Записать бой CL1 и поддержать замер цикла из двух боёв."""
    instance_name = instance_name_from_config(config)
    get_runtime_storage().increment_monthly_counter(
        instance_name, MonthlyMetric.BATTLE_COUNT
    )

    # В CL1 два боя образуют один цикл; нечётный бой начинает новый цикл.
    if cl1_battle_count % 2 != 1:
        return round_started_at

    now = time.time()
    if round_started_at:
        cost = round(now - float(round_started_at), 2)
        logger.attr("Длительность раунда CL1", f"{cost}s/round")
        try:
            from module.statistics.ship_exp_stats import get_ship_exp_stats

            get_ship_exp_stats(instance_name=instance_name).record_round_time(cost)
        except Exception:
            logger.exception("[Статистика — Operation Siren] Не удалось записать длительность раунда в зоне коррозии 1")
    return now


def meow_hazard_level_from_runtime(main: Any) -> int | None:
    """Определить уровень коррозии Meow из карты или конфигурации."""
    hazard_level = None
    try:
        hazard_level = getattr(getattr(main, "zone", None), "hazard_level", None)
    except Exception:
        logger.debug("[Статистика — Operation Siren] Не удалось получить уровень коррозии из текущей зоны")

    if hazard_level not in MEOW_HAZARD_LEVELS:
        try:
            hazard_level = main.config.cross_get(
                keys="OpsiMeowfficerFarming.OpsiMeowfficerFarming.HazardLevel"
            )
        except Exception:
            hazard_level = None

    try:
        hazard_level = int(hazard_level)
    except (TypeError, ValueError):
        return None

    return hazard_level if hazard_level in MEOW_HAZARD_LEVELS else None


def meow_battles_per_round(hazard_level: int | None) -> int:
    """Вернуть число боёв в одном эффективном цикле Meow."""
    if hazard_level in {4, 5, 6}:
        return 3
    return 2


def record_meow_auto_search_battle(
    main: Any,
    battle_started_at: float | int | None,
) -> float:
    """Записать бой Meow и вернуть начало следующего замера."""
    hazard_level = meow_hazard_level_from_runtime(main)
    instance_name = instance_name_from_config(main.config)

    if hazard_level is None:
        raise RuntimeError(
            "Уровень коррозии Meow не подтверждён; запись статистики остановлена."
        )
    get_runtime_storage().record_meow_battle(instance_name, hazard_level)

    now = time.time()
    if battle_started_at:
        battle_duration = round(now - float(battle_started_at), 2)
        if 5 < battle_duration < 600:
            logger.attr("Длительность боя фарма мяуфицеров", f"{battle_duration:.1f}s")
            get_runtime_storage().record_meow_timing(
                instance_name,
                "battle",
                Decimal(str(battle_duration)),
                hazard_level,
            )
        else:
            logger.debug(
                f"[Статистика — Operation Siren] Длительность боя фарма мяуфицеров {battle_duration:.1f} с вне допустимого диапазона; запись пропущена"
            )
    return now


def start_meow_search_timer(main: Any) -> tuple[float, int | None]:
    """Зафиксировать начало поиска Meow и текущие очки действия."""
    try:
        refresh_action_point(main)
        start_ap = main._action_point_total
        logger.debug(f"[Статистика — Operation Siren] Начат поиск фарма мяуфицеров, очки действия: {start_ap}")
    except Exception:
        start_ap = None
        logger.debug("[Статистика — Operation Siren] Не удалось получить начальное число очков действия")

    logger.debug("[Статистика — Operation Siren] Начат поиск фарма мяуфицеров; таймер сброшен")
    return time.time(), start_ap


def finish_meow_search_timer(
    main: Any,
    search_started_at: float,
    battle_count: int,
) -> float | None:
    """Записать длительность эффективного цикла завершённого поиска Meow."""
    try:
        refresh_action_point(main)
    except Exception:
        logger.debug("[Статистика — Operation Siren] Не удалось получить итоговое число очков действия")
    else:
        try:
            record_ap_snapshot(
                main.config,
                ap_current=main._action_point_current,
                ap_total=main._action_point_total,
                source="meow",
            )
        except StorageError:
            raise
        except Exception:
            logger.debug("Не удалось записать снимок очков действия фарма мяуфицеров", exc_info=True)

    duration = time.time() - search_started_at
    hazard_level = meow_hazard_level_from_runtime(main)
    battles_per_round = meow_battles_per_round(hazard_level)
    logger.debug(f"[Статистика — Operation Siren] Уровень коррозии: {hazard_level}, боёв за раунд: {battles_per_round}")

    # Один поиск может содержать несколько боёв; приводим время к одному циклу.
    if battle_count > 0:
        rounds = battle_count / battles_per_round
        duration = duration / rounds
        logger.debug(
            f"[Статистика — Operation Siren] Общая длительность поиска фарма мяуфицеров: {time.time() - search_started_at:.1f} с, "
            f"боёв: {battle_count}, раундов: {rounds}, на раунд: {duration:.1f} с"
        )

    if duration < 1 or duration > 1800:
        logger.debug(f"[Статистика — Operation Siren] Длительность поиска фарма мяуфицеров {duration:.1f} с вне допустимого диапазона; запись пропущена")
        return None

    logger.attr("Длительность поиска фарма мяуфицеров", f"{duration:.1f}s")
    get_runtime_storage().record_meow_timing(
        instance_name_from_config(main.config),
        "round",
        Decimal(str(round(duration, 2))),
        hazard_level,
    )

    return duration


def record_cl1_akashi_encounter(config: Any) -> int | None:
    """Записать встречу с Акаси в CL1 и вернуть месячный итог."""
    instance_name = instance_name_from_config(config)
    aggregate = get_runtime_storage().increment_monthly_counter(
        instance_name, MonthlyMetric.AKASHI_ENCOUNTERS
    )
    encounters = int(aggregate.value)
    logger.attr("Месячное число встреч с Акаси в зоне коррозии 1", encounters)
    return encounters


def record_siren_research_device(main: Any) -> None:
    """Записать устройство Сирен с точным источником и уровнем коррозии."""
    source = battle_source_from_config(main.config)
    if source not in {"cl1", "meow"}:
        return

    hazard_level = meow_hazard_level_from_runtime(main) if source == "meow" else None
    if source == "meow" and hazard_level is None:
        raise RuntimeError(
            "Уровень коррозии Meow не подтверждён; Siren event не записан."
        )
    get_runtime_storage().record_siren_research_device(
        instance_name_from_config(main.config),
        source=source,
        hazard_level=hazard_level,
    )
    label = "cl1" if source == "cl1" else f"meow-{hazard_level}"
    logger.attr("Исследовательское устройство Сирен", label)
