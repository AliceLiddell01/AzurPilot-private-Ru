"""Явное отображение logical task roles на физические Surface Fleets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
    if order in {"fleet1_all_fleet2_standby", "fleet1_standby_fleet2_all"}:
        return "all"
    raise ValueError(f"Неизвестный порядок флотов: {order}")


def _working_logical_indices(order: str) -> tuple[int, ...]:
    try:
        return {
            "fleet1_all_fleet2_standby": (1,),
            "fleet1_standby_fleet2_all": (2,),
            "fleet1_mob_fleet2_boss": (1, 2),
            "fleet1_boss_fleet2_mob": (1, 2),
        }[order]
    except KeyError as exc:
        raise ValueError(f"Неизвестный порядок флотов: {order}") from exc


def _build_working_fleet_bindings(
    task: str,
    order: str,
    read_physical_fleet: Callable[[int], object],
) -> tuple[WorkingFleetBinding, ...]:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Нельзя определить task для Fleet mapping")
    bindings = tuple(
        WorkingFleetBinding(
            task=task,
            role=_role_for(order, logical_index),
            logical_fleet_index=logical_index,
            physical_fleet_index=validate_surface_fleet_index(
                read_physical_fleet(logical_index)
            ),
        )
        for logical_index in _working_logical_indices(order)
    )
    if len({item.physical_fleet_index for item in bindings}) != len(bindings):
        raise ValueError("Две logical роли не могут ссылаться на один physical Fleet")
    return bindings


def working_fleet_bindings(config: Any, *, task: str | None = None) -> tuple[WorkingFleetBinding, ...]:
    """Вернуть только physical fleets, реально используемые текущей задачей."""

    task_name = task if task is not None else getattr(getattr(config, "task", None), "command", None)
    if not isinstance(task_name, str) or not task_name.strip():
        raise ValueError("Нельзя определить task для Fleet mapping")
    order = getattr(config, "Fleet_FleetOrder", None)
    if not isinstance(order, str):
        raise TypeError("Fleet_FleetOrder должен быть строкой")
    return _build_working_fleet_bindings(
        task_name,
        order,
        lambda logical_index: physical_fleet_index(config, logical_index),
    )


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
    return _build_working_fleet_bindings(
        task,
        order,
        lambda logical_index: fleet.get(f"Fleet{logical_index}"),
    )


__all__ = (
    "WorkingFleetBinding",
    "physical_fleet_index",
    "working_fleet_bindings",
    "working_fleet_bindings_from_data",
)
