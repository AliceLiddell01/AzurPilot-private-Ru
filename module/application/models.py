"""Неизменяемые read models прикладного слоя."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TypeAlias


MetadataScalar: TypeAlias = str | int | float | bool | None
MetadataValue: TypeAlias = MetadataScalar | tuple[MetadataScalar, ...]


class RuntimeState(IntEnum):
    """Стабильная интерпретация legacy-кодов состояния ProcessManager."""

    RUNNING = 1
    STOPPED = 2
    WARNING = 3
    UPDATING = 4


@dataclass(frozen=True, slots=True)
class InstanceReference:
    name: str


@dataclass(frozen=True, slots=True)
class InstanceStatus:
    name: str
    running: bool
    state: RuntimeState


@dataclass(frozen=True, slots=True)
class TaskSummary:
    name: str
    display_name: str
    help: str


@dataclass(frozen=True, slots=True)
class TaskOption:
    value: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TaskArgumentMetadata:
    name: str
    display_name: str
    help: str
    input_type: str
    default: MetadataValue
    options: tuple[TaskOption, ...]


@dataclass(frozen=True, slots=True)
class TaskGroupMetadata:
    name: str
    display_name: str
    help: str
    arguments: tuple[TaskArgumentMetadata, ...]


@dataclass(frozen=True, slots=True)
class TaskMetadata:
    name: str
    display_name: str
    help: str
    groups: tuple[TaskGroupMetadata, ...]
