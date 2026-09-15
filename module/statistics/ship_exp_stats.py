"""舰船经验效率统计模块。

统计舰船经验检测数据和战斗时间，包含每日经验效率统计，
用于预估升级时间。

功能：
- 记录每次战斗的经验获取量和战斗时间
- 计算每日经验效率（经验/小时）
- 预估达到目标等级所需时间
- 持久化统计数据到 JSON 文件

继承自 LIST_SHIP_EXP 数据，复用经验数据定义。
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
    """
    舰船经验统计类
    - 记录每场战斗时间和经验
    - 统计每日效率 (经验/小时)
    - 保存舰船检测数据
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
        """加载数据文件"""
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
        """保存数据文件"""
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
        """战斗开始时调用（侵蚀1 / 耄耋相接等统一入口）"""
        self._battle_start_time = time.time()
    
    def on_battle_end(self, fleet_index: int = 1, source: str = "cl1") -> float | None:
        """
        战斗结束时调用
        
        Args:
            fleet_index: 舰队索引 (1-6), 用于确定经验值
            source: 战斗来源:
                - "cl1": 侵蚀1练级
                - "meow": 耄耋相接
                - 其他值默认按 "cl1" 处理
        
        Returns:
            本场战斗耗时(秒), 如果未记录开始时间则返回None
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
        """记录单场战斗时间到样本
        
        Args:
            duration: 本场战斗时长（秒）
            source: 战斗来源 ("cl1" / "meow")
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
        """记录单轮侵蚀1时间到样本"""
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
        """
        更新每日统计数据
        每场战斗结束后调用，累加到当天的统计
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
        """清理超过30天的旧统计数据"""
        if 'daily_stats' not in self.data:
            return
        
        dates = sorted(self.data['daily_stats'].keys(), reverse=True)
        if len(dates) > self.MAX_DAILY_STATS_DAYS:
            for old_date in dates[self.MAX_DAILY_STATS_DAYS:]:
                del self.data['daily_stats'][old_date]
    
    def get_average_battle_time(self) -> float:
        """获取平均每场战斗时间(秒)"""
        return self.data.get('battle_times', {}).get('average', 52.0)
    
    def get_average_meow_battle_time(self) -> float:
        """获取耄耋相接平均每场战斗时间(秒)"""
        return self.data.get('meow_battle_times', {}).get('average', self.get_average_battle_time())
    
    def get_average_round_time(self) -> float:
        """获取平均每轮侵蚀1时间(秒)"""
        if 'round_times' in self.data and self.data['round_times'].get('samples'):
            return self.data['round_times']['average']
        return self.get_average_battle_time() * 2 + 15
    
    def get_exp_per_hour(self) -> float:
        """
        获取经验效率 (经验/小时)
        使用公式估算：平均每场经验 * 2 / 平均一轮时长
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
        """获取今日统计数据"""
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
        """
        保存舰船经验检测数据
        
        Args:
            ships: 舰船数据列表, 每项包含 position, level, current_exp, total_exp
            target_level: 目标等级
            fleet_index: 舰队索引
            battle_count_at_check: 检测时的战斗场次
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
        """
        计算单艘舰船的升级进度
        
        Args:
            ship: 舰船数据 (position, level, current_exp, total_exp)
            target_level: 目标等级
            current_battle_count: 当前战斗场次
        
        Returns:
            进度数据字典
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
        """
        获取所有舰船的升级进度
        
        Args:
            current_battle_count: 当前战斗场次
        
        Returns:
            所有舰船的进度数据列表
        """
        ships = self.data.get('ships', [])
        target_level = self.data.get('target_level', 125)
        
        return [
            self.calculate_progress(ship, target_level, current_battle_count)
            for ship in ships
        ]
    
    @staticmethod
    def _format_time(seconds: float) -> str:
        """格式化时间显示"""
        if seconds <= 0:
            return "0分钟"
        
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        return f"{minutes}分钟"


# ========== Singleton и вспомогательные функции ==========

_stats_instances: dict[str, ShipExpStats] = {}


def get_ship_exp_stats(instance_name: str | None = None) -> ShipExpStats:
    """获取 ShipExpStats 实例"""
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
    """便捷函数: 保存舰船经验检测数据"""
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
