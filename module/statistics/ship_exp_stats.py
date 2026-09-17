"""Модуль статистики эффективности опыта кораблей.

Собирает данные распознавания опыта кораблей и времени боёв,
включая суточную статистику эффективности опыта для оценки времени прокачки.

Возможности:
- Фиксация полученного опыта и времени каждой битвы.
- Расчёт суточной эффективности опыта (опыт/час).
- Оценка времени, необходимого для достижения целевого уровня.
- Сохранение статистики в файл JSON.

Наследует структуру данных LIST_SHIP_EXP, переиспользуя определения опыта.
"""

# Этот файл используется для статистики данных проверки опыта кораблей и времени боя
# Включает ежедневную статистику эффективности опыта для оценки времени прокачки

from __future__ import annotations

import math
import time
import json
from pathlib import Path
from datetime import datetime, date
from typing import Any

from module.os.ship_exp_data import LIST_SHIP_EXP
from module.logger import logger


class ShipExpStats:
    """Класс статистики опыта кораблей.

    - Фиксирует время каждого боя и полученный опыт.
    - Рассчитывает суточную эффективность (опыт/час).
    - Сохраняет данные распознавания кораблей.
    """
    
    # Опыт за бой для каждой позиции
    EXP_PER_BATTLE = {
        1: 431,  # Флагман
        2: 288, 3: 288, 4: 288, 5: 288, 6: 288  # Остальные позиции
    }
    AVG_EXP_PER_BATTLE = 312  # Средний опыт за бой
    BATTLES_PER_ROUND = 2     # По умолчанию 2 боя за проход зоны коррозии 1
    
    MAX_BATTLE_TIME_SAMPLES = 100  # Храним последние 100 образцов времени боя
    MAX_DAILY_STATS_DAYS = 30      # Храним статистику за последние 30 дней
    
    def __init__(self, path: Path | None = None, instance_name: str | None = None):
        if path is None:
            project_root = Path(__file__).resolve().parents[2]
            instance_dir = instance_name or "default"
            self._path = project_root / "log" / "cl1" / instance_dir / "ship_exp_data.json"
        else:
            self._path = Path(path)
        self._instance_name = instance_name or "default"
        self.data = self._load()
        
        # Время начала текущего боя
        self._battle_start_time: float | None = None
    
    def _load(self) -> dict[str, Any]:
        """Загрузить файл данных."""
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding='utf-8')
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return {}
        except Exception as e:
            logger.warning(f'[Статистика — опыт] Не удалось загрузить данные опыта кораблей: {e}')
            return {}
    
    def _save(self) -> None:
        """Сохранить файл данных."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception as e:
            logger.warning(f'[Статистика — опыт] Не удалось сохранить данные опыта кораблей: {e}')
    
    # ========== Учёт времени боя ==========
    
    def on_battle_start(self) -> None:
        """Вызывается в начале боя (единая точка входа для CL1, Meowfficer Farming и др.)."""
        self._battle_start_time = time.time()
    
    def on_battle_end(self, fleet_index: int = 1, source: str = "cl1") -> float | None:
        """Вызывается по завершении боя.

        Args:
            fleet_index: Индекс флота (1-6) для определения значения опыта.
            source: Источник боя:
                - "cl1": прокачка в зоне коррозии 1.
                - "meow": Meowfficer Farming (сбор кошачьих очков).
                - Другие значения по умолчанию обрабатываются как "cl1".

        Returns:
            Длительность текущего боя в секундах либо None, если время начала не зафиксировано.
        """
        if self._battle_start_time is None:
            return None
        
        duration = time.time() - self._battle_start_time
        self._battle_start_time = None
        
        # Фильтруем аномальные значения (слишком короткие или длинные бои)
        if duration < 1 or duration > 300:
            logger.debug(f'Длительность боя {duration:.1f} с вне допустимого диапазона; запись пропущена')
            return duration
        
        # Записываем время боя отдельно для каждого источника
        source = "meow" if source == "meow" else "cl1"
        self._record_battle_time(duration, source=source)
        
        # Вычисляем опыт текущего боя (используем среднее, поскольку опыт зависит от позиции)
        # Флагман 431 + остальные позиции 288*5 = 1871, среднее 312
        avg_exp = self.AVG_EXP_PER_BATTLE
        
        # Суточная эффективность опыта используется для оценки прокачки в зоне коррозии 1, чтобы не смешивать её с затратами времени Meowfficer Farming.
        if source == "cl1":
            self._update_daily_stats(exp_gained=avg_exp, battle_duration=duration)

        logger.info(f'{source.upper()}: бой записан, длительность {duration:.1f} с, опыт: {avg_exp}')
        return duration
    
    def _record_battle_time(self, duration: float, source: str = "cl1") -> None:
        """Записать длительность одиночного боя в выборку.

        Args:
            duration: Длительность текущего боя (в секундах).
            source: Источник боя ("cl1" / "meow").
        """
        # Для разных источников используем разные ключи, чтобы не смешивать статистику зоны коррозии 1 и Meowfficer Farming
        if source == "meow":
            key = 'meow_battle_times'
            default_avg = 52.0  # Значение по умолчанию, позже заменяется реальными образцами
        else:
            key = 'battle_times'
            default_avg = 52.0
        
        if key not in self.data:
            self.data[key] = {'samples': [], 'average': default_avg}
        
        samples = self.data[key]['samples']
        samples.append(round(duration, 2))
        
        # Храним только последние N образцов
        if len(samples) > self.MAX_BATTLE_TIME_SAMPLES:
            self.data[key]['samples'] = samples[-self.MAX_BATTLE_TIME_SAMPLES:]
            samples = self.data[key]['samples']
        
        # Обновляем среднее значение
        if samples:
            self.data[key]['average'] = round(sum(samples) / len(samples), 2)
        
        self._save()
    
    def record_round_time(self, round_duration: float) -> None:
        """Записать время одного прохода зоны коррозии 1 в выборку."""
        if 'round_times' not in self.data:
            self.data['round_times'] = {'samples': [], 'average': 120.0}
        
        samples = self.data['round_times']['samples']
        samples.append(round(round_duration, 2))
        
        # Храним только последние 100 образцов
        if len(samples) > 100:
            self.data['round_times']['samples'] = samples[-100:]
            samples = self.data['round_times']['samples']
        
        # Обновляем среднее значение
        if samples:
            self.data['round_times']['average'] = round(sum(samples) / len(samples), 2)
        
        self._save()
    
    # ========== Суточная статистика эффективности опыта ==========
    
    def _update_daily_stats(self, exp_gained: int, battle_duration: float) -> None:
        """Обновить суточные статистические данные.

        Вызывается после завершения каждого боя и суммируется в статистику за сегодня.
        """
        today = date.today().isoformat()  # "2026-01-01"
        
        if 'daily_stats' not in self.data:
            self.data['daily_stats'] = {}
        
        if today not in self.data['daily_stats']:
            self.data['daily_stats'][today] = {
                'total_run_time': 0.0,
                'total_exp_gained': 0,
                'battle_count': 0,
                'exp_per_hour': 0.0
            }
        
        stats = self.data['daily_stats'][today]
        stats['total_run_time'] += battle_duration
        stats['total_exp_gained'] += exp_gained
        stats['battle_count'] += 1
        
        # Вычисляем эффективность опыта в час
        hours = stats['total_run_time'] / 3600
        if hours > 0:
            stats['exp_per_hour'] = round(stats['total_exp_gained'] / hours, 2)
        
        # Очищаем старые данные
        self._cleanup_old_daily_stats()
        
        self._save()
    
    def _cleanup_old_daily_stats(self) -> None:
        """Очистить устаревшие статистические данные старше 30 дней."""
        if 'daily_stats' not in self.data:
            return
        
        dates = sorted(self.data['daily_stats'].keys(), reverse=True)
        if len(dates) > self.MAX_DAILY_STATS_DAYS:
            for old_date in dates[self.MAX_DAILY_STATS_DAYS:]:
                del self.data['daily_stats'][old_date]
    
    def get_average_battle_time(self) -> float:
        """Получить среднюю длительность одного боя (в секундах)."""
        return self.data.get('battle_times', {}).get('average', 52.0)
    
    def get_average_meow_battle_time(self) -> float:
        """Получить среднюю длительность одного боя Meowfficer Farming (в секундах)."""
        return self.data.get('meow_battle_times', {}).get('average', self.get_average_battle_time())
    
    def get_average_round_time(self) -> float:
        """Получить среднюю длительность одного прохода зоны коррозии 1 (в секундах)."""
        if 'round_times' in self.data and self.data['round_times'].get('samples'):
            return self.data['round_times']['average']
        return self.get_average_battle_time() * 2 + 15
    
    def get_exp_per_hour(self) -> float:
        """Получить эффективность опыта (опыт/час).

        Оценивается по формуле: средний опыт за бой * 2 / средняя длительность прохода.
        """
        avg_round_time = self.get_average_round_time()
        if avg_round_time > 0:
            exp_per_hour = (
                self.AVG_EXP_PER_BATTLE
                * self.BATTLES_PER_ROUND
                * 3600
                / avg_round_time
            )
            return round(exp_per_hour, 2)

        return 22000.0  # Значение по умолчанию
    
    def get_today_stats(self) -> dict[str, Any] | None:
        """Получить статистические данные за сегодня."""
        today = date.today().isoformat()
        if 'daily_stats' not in self.data:
            return None
        return self.data['daily_stats'].get(today)
    
    # ========== Сохранение данных кораблей и расчёт прогресса ==========
    
    def save_ship_data(
        self,
        ships: list[dict[str, Any]],
        target_level: int,
        fleet_index: int,
        battle_count_at_check: int
    ) -> None:
        """Сохранить данные распознавания опыта кораблей.

        Args:
            ships: Список данных кораблей, каждая запись содержит position, level, current_exp, total_exp.
            target_level: Целевой уровень.
            fleet_index: Индекс флота.
            battle_count_at_check: Счётчик боёв на момент распознавания.
        """
        self.data['last_check_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.data['target_level'] = target_level
        self.data['fleet_index'] = fleet_index
        self.data['battle_count_at_check'] = battle_count_at_check
        self.data['ships'] = ships
        self._save()
        logger.info(f'[Статистика — опыт] Данные опыта кораблей сохранены: {len(ships)}, целевой уровень {target_level}')
    
    def calculate_progress(
        self,
        ship: dict[str, Any],
        target_level: int,
        current_battle_count: int
    ) -> dict[str, Any]:
        """Рассчитать прогресс прокачки отдельного корабля.

        Args:
            ship: Данные корабля (position, level, current_exp, total_exp).
            target_level: Целевой уровень.
            current_battle_count: Текущее число боёв.

        Returns:
            Словарь с данными прогресса.
        """
        # Обрабатываем границы уровня (1–125)
        if target_level < 1:
            target_level = 1
        elif target_level > 125:
            target_level = 125
        
        target_exp = LIST_SHIP_EXP[target_level - 1]
        current_total_exp = ship.get('total_exp', 0)
        exp_needed = max(0, target_exp - current_total_exp)
        
        # Вычисляем необходимое число оставшихся боёв
        position = ship.get('position', 1)
        exp_per_battle = self.EXP_PER_BATTLE.get(position, 288)
        battles_needed = math.ceil(exp_needed / exp_per_battle) if exp_needed > 0 else 0
        
        # Вычисляем число проведённых боёв с момента последней проверки
        battle_count_at_check = self.data.get('battle_count_at_check', 0)
        battles_done = max(0, current_battle_count - battle_count_at_check)
        
        # Вычисляем примерное время (опыт * 2 / средняя длительность прохода)
        avg_round_time = self.get_average_round_time()
        if avg_round_time > 0 and exp_needed > 0 and exp_per_battle > 0:
            ship_exp_per_hour = (
                exp_per_battle
                * self.BATTLES_PER_ROUND
                * 3600
                / avg_round_time
            )
            hours_needed = exp_needed / ship_exp_per_hour
            time_seconds = hours_needed * 3600
        else:
            time_seconds = 0
        
        return {
            'position': position,
            'level': ship.get('level', 0),
            'current_exp': ship.get('current_exp', 0),
            'total_exp': current_total_exp,
            'target_exp': target_exp,
            'battles_done': battles_done,
            'exp_needed': exp_needed,
            'battles_needed': battles_needed,
            'time_needed': self._format_time(time_seconds)
        }
    
    def get_all_progress(self, current_battle_count: int) -> list[dict[str, Any]]:
        """Получить прогресс прокачки всех кораблей.

        Args:
            current_battle_count: Текущее число боёв.

        Returns:
            Список словарей с данными прогресса по всем кораблям.
        """
        ships = self.data.get('ships', [])
        target_level = self.data.get('target_level', 125)
        
        return [
            self.calculate_progress(ship, target_level, current_battle_count)
            for ship in ships
        ]
    
    @staticmethod
    def _format_time(seconds: float) -> str:
        """Отформатировать отображение времени."""
        if seconds <= 0:
            return "0 мин"
        
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        
        if hours > 0:
            return f"{hours} ч {minutes} мин"
        return f"{minutes} мин"


# ========== Singleton и вспомогательные функции ==========

_stats_instances: dict[str, ShipExpStats] = {}


def get_ship_exp_stats(instance_name: str | None = None) -> ShipExpStats:
    """Получить экземпляр ShipExpStats."""
    global _stats_instances
    key = instance_name or "default"
    if key not in _stats_instances:
        _stats_instances[key] = ShipExpStats(instance_name=instance_name)
    else:
        # Обновляем данные
        _stats_instances[key].data = _stats_instances[key]._load()
    return _stats_instances[key]


def save_ship_exp_data(
    ships: list[dict[str, Any]],
    target_level: int,
    fleet_index: int,
    battle_count_at_check: int,
    instance_name: str | None = None
) -> None:
    """Вспомогательная функция: сохранить данные распознавания опыта кораблей."""
    get_ship_exp_stats(instance_name=instance_name).save_ship_data(
        ships=ships,
        target_level=target_level,
        fleet_index=fleet_index,
        battle_count_at_check=battle_count_at_check
    )


__all__ = [
    'ShipExpStats',
    'get_ship_exp_stats',
    'save_ship_exp_data',
]
