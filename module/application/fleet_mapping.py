"""Явное отображение logical task roles на физические Surface Fleets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from module.formation.model import validate_surface_fleet_index


@dataclass(frozen=True, slots=True)
class WorkingFleetBinding:
    """Связь роли задачи с физическим Formation Surface Fleet."""

    task: str
    role: str
    logical_fleet_index: int
    physical_fleet_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("task должен быть непустой строкой")
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("role должен быть непустой строкой")
        if type(self.logical_fleet_index) is not int or self.logical_fleet_index not in {
            1,
            2,
        }:
            raise ValueError("logical_fleet_index должен быть 1 или 2")
        if type(self.physical_fleet_index) is not int or not 1 <= self.physical_fleet_index <= 6:
            raise ValueError("physical_fleet_index должен быть int в диапазоне 1..6")


def physical_fleet_index(config: Any, logical_fleet_index: int) -> int:
    """Прочитать physical Fleet mapping, не подставляя значение по умолчанию."""

    if type(logical_fleet_index) is not int or logical_fleet_index not in {1, 2}:
        raise ValueError("logical_fleet_index должен быть 1 или 2")
    value = getattr(config, f"Fleet_Fleet{logical_fleet_index}", None)
    return validate_surface_fleet_index(value)


def _role_for(order: str, logical_fleet_index: int) -> str:
    if order == "fleet1_mob_fleet2_boss":
        return "mob" if logical_fleet_index == 1 else "boss"
    if order == "fleet1_boss_fleet2_mob":
        return "boss" if logical_fleet_index == 1 else "mob"
    if order == "fleet1_all_fleet2_standby":
        return "all"
    if order == "fleet1_standby_fleet2_all":
        return "all"
    raise ValueError(f"Неизвестный порядок флотов: {order}")


def working_fleet_bindings(config: Any, *, task: str | None = None) -> tuple[WorkingFleetBinding, ...]:
    """Вернуть только physical fleets, реально используемые текущей задачей."""

    task_name = task or getattr(getattr(config, "task", None), "command", None)
    if not isinstance(task_name, str) or not task_name.strip():
        task_name = getattr(config, "config_name", None)
    if not isinstance(task_name, str) or not task_name.strip():
        raise ValueError("Нельзя определить task для Fleet mapping")
    order = getattr(config, "Fleet_FleetOrder", None)
    if not isinstance(order, str):
        raise TypeError("Fleet_FleetOrder должен быть строкой")
    logical_indices = (1,) if order == "fleet1_all_fleet2_standby" else (
        (2,) if order == "fleet1_standby_fleet2_all" else (1, 2)
    )
    bindings: list[WorkingFleetBinding] = []
    for logical_index in logical_indices:
        physical = physical_fleet_index(config, logical_index)
        bindings.append(
            WorkingFleetBinding(
                task=task_name,
                role=_role_for(order, logical_index),
                logical_fleet_index=logical_index,
                physical_fleet_index=physical,
            )
        )
    if len({item.physical_fleet_index for item in bindings}) != len(bindings):
        raise ValueError("Две logical роли не могут ссылаться на один physical Fleet")
    return tuple(bindings)


def working_fleet_bindings_from_data(
    data: Mapping[str, Any],
    task: str,
) -> tuple[WorkingFleetBinding, ...]:
    """Построить то же отображение из raw profile data на границе WebUI."""

    if not isinstance(data, Mapping) or not isinstance(task, str) or not task.strip():
        raise TypeError("data и task должны иметь корректные типы")
    task_data = data.get(task)
    if task_data is None:
        raise ValueError(f"В профиле отсутствует task mapping: {task}")
    if not isinstance(task_data, Mapping):
        raise TypeError(f"task mapping должен быть Mapping: {task}")
    fleet = task_data.get("Fleet")
    if fleet is None:
        raise ValueError(f"В task отсутствует Fleet mapping: {task}")
    if not isinstance(fleet, Mapping):
        raise TypeError(f"Fleet mapping должен быть Mapping: {task}")
    order = fleet.get("FleetOrder")
    if not isinstance(order, str):
        raise TypeError("Fleet_FleetOrder должен быть строкой")
    logical_indices = (1,) if order == "fleet1_all_fleet2_standby" else (
        (2,) if order == "fleet1_standby_fleet2_all" else (1, 2)
    )
    bindings: list[WorkingFleetBinding] = []
    for logical_index in logical_indices:
        raw = fleet.get(f"Fleet{logical_index}")
        physical = validate_surface_fleet_index(raw)
        bindings.append(
            WorkingFleetBinding(
                task=task,
                role=_role_for(order, logical_index),
                logical_fleet_index=logical_index,
                physical_fleet_index=physical,
            )
        )
    if len({item.physical_fleet_index for item in bindings}) != len(bindings):
        raise ValueError("Две logical роли не могут ссылаться на один physical Fleet")
    return tuple(bindings)


__all__ = (
    "WorkingFleetBinding",
    "physical_fleet_index",
    "working_fleet_bindings",
    "working_fleet_bindings_from_data",
)
