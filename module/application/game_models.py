"""Транспортно-независимые модели legacy game/runtime capabilities."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

type PayloadScalar = str | int | float | bool | None | date | datetime | Decimal
type FrozenPayload = (
    PayloadScalar
    | tuple["FrozenPayload", ...]
    | Mapping[str, "FrozenPayload"]
)

REDACTED_CONFIG_VALUE = "<скрыто>"


def freeze_payload(value: object, *, field_name: str = "payload") -> FrozenPayload:
    """Проверить и рекурсивно заморозить динамическое прикладное значение."""

    if value is None or isinstance(value, (str, int, bool, date, datetime, Decimal)):
        return value  # type: ignore[return-value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{field_name} содержит нечисловой float")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenPayload] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} содержит нестроковый ключ")
            frozen[key] = freeze_payload(item, field_name=f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_payload(item, field_name=f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(f"{field_name} содержит неподдерживаемый тип")


def thaw_payload(value: object) -> object:
    """Вернуть безопасную копию payload для сериализации конкретным consumer."""

    if isinstance(value, Mapping):
        return {key: thaw_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_payload(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ConfigArgumentDefinition:
    """Описание одного аргумента из generated config metadata."""

    task: str
    group: str
    argument: str
    input_type: str
    default: FrozenPayload
    options: tuple[FrozenPayload, ...] = ()
    validation: FrozenPayload | None = None
    sensitive: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("task", self.task),
            ("group", self.group),
            ("argument", self.argument),
            ("input_type", self.input_type),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} должен быть непустой строкой")
        object.__setattr__(self, "default", freeze_payload(self.default, field_name="default"))
        object.__setattr__(
            self,
            "options",
            tuple(
                freeze_payload(value, field_name="options")
                for value in self.options
            ),
        )
        if self.validation is not None:
            object.__setattr__(
                self,
                "validation",
                freeze_payload(self.validation, field_name="validation"),
            )
        if type(self.sensitive) is not bool:
            raise TypeError("sensitive должен быть bool")


@dataclass(frozen=True, slots=True)
class DashboardResource:
    """Один ресурс из dashboard без зависимости от legacy mapping."""

    key: str
    label: str
    value: FrozenPayload
    limit: FrozenPayload | None = None
    total: FrozenPayload | None = None
    last_update: FrozenPayload | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("key ресурса должен быть непустой строкой")
        if not isinstance(self.label, str):
            raise TypeError("label ресурса должен быть строкой")
        for name in ("value", "limit", "total", "last_update"):
            current = getattr(self, name)
            if current is not None:
                object.__setattr__(
                    self,
                    name,
                    freeze_payload(current, field_name=f"resource.{name}"),
                )


@dataclass(frozen=True, slots=True)
class DashboardResources:
    """Упорядоченный immutable projection dashboard resources."""

    items: tuple[DashboardResource, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, DashboardResource) for item in self.items
        ):
            raise TypeError("items должен быть tuple DashboardResource")
        keys = tuple(item.key for item in self.items)
        if len(keys) != len(set(keys)):
            raise ValueError("Ресурсы dashboard не должны повторяться")


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Снимок конфигурации выбранного экземпляра."""

    instance: str
    task: str | None
    data: Mapping[str, FrozenPayload]

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if self.task is not None and not isinstance(self.task, str):
            raise ValueError("task должен быть строкой или None")
        frozen = freeze_payload(self.data, field_name="config")
        if not isinstance(frozen, Mapping):
            raise TypeError("config должен быть mapping")
        object.__setattr__(self, "data", frozen)


@dataclass(frozen=True, slots=True)
class ConfigUpdateRequest:
    """Типизированный запрос изменения config task/group/argument."""

    instance: str
    task: str
    group: str
    argument: str
    value: FrozenPayload

    def __post_init__(self) -> None:
        for name in ("instance", "task", "group", "argument"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} должен быть непустой строкой")
        object.__setattr__(
            self,
            "value",
            freeze_payload(self.value, field_name="value"),
        )

    @property
    def path(self) -> str:
        return f"{self.task}.{self.group}.{self.argument}"


@dataclass(frozen=True, slots=True)
class ConfigUpdateResult:
    """Подтверждённое изменение config без transport-specific details."""

    request: ConfigUpdateRequest
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request, ConfigUpdateRequest):
            raise TypeError("request должен быть ConfigUpdateRequest")
        if type(self.verified) is not bool:
            raise TypeError("verified должен быть bool")


@dataclass(frozen=True, slots=True)
class RuntimeLogTail:
    """Ограниченный tail журнала без публикации имени файла."""

    instance: str
    lines: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if not isinstance(self.lines, tuple) or any(
            not isinstance(line, str) for line in self.lines
        ):
            raise TypeError("lines должен быть tuple строк")

    @property
    def text(self) -> str:
        return "".join(self.lines)


@dataclass(frozen=True, slots=True)
class CurrentTaskSnapshot:
    """Последняя определённая задача текущего runtime."""

    instance: str
    task: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("task должен быть непустой строкой")


@dataclass(frozen=True, slots=True)
class SchedulerEntry:
    """Одна schedulable task в очереди runtime."""

    task: str
    next_run: FrozenPayload

    def __post_init__(self) -> None:
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("task должен быть непустой строкой")
        object.__setattr__(
            self,
            "next_run",
            freeze_payload(self.next_run, field_name="next_run"),
        )


@dataclass(frozen=True, slots=True)
class SchedulerQueueSnapshot:
    """Упорядоченная очередь schedulable tasks."""

    instance: str
    entries: tuple[SchedulerEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, SchedulerEntry) for entry in self.entries
        ):
            raise TypeError("entries должен быть tuple SchedulerEntry")
        tasks = tuple(entry.task for entry in self.entries)
        if len(tasks) != len(set(tasks)):
            raise ValueError("Очередь scheduler не должна содержать дубликаты")


@dataclass(frozen=True, slots=True)
class ScheduleTaskRequest:
    """Запрос немедленного планирования одной задачи."""

    instance: str
    task: str

    def __post_init__(self) -> None:
        for name in ("instance", "task"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} должен быть непустой строкой")


@dataclass(frozen=True, slots=True)
class ScheduleTaskResult:
    request: ScheduleTaskRequest
    scheduled_at: FrozenPayload
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request, ScheduleTaskRequest):
            raise TypeError("request должен быть ScheduleTaskRequest")
        object.__setattr__(
            self,
            "scheduled_at",
            freeze_payload(self.scheduled_at, field_name="scheduled_at"),
        )
        if type(self.verified) is not bool:
            raise TypeError("verified должен быть bool")


@dataclass(frozen=True, slots=True)
class SchedulerQueueClearResult:
    instance: str
    cleared_tasks: tuple[str, ...]
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if not isinstance(self.cleared_tasks, tuple) or any(
            not isinstance(task, str) or not task for task in self.cleared_tasks
        ):
            raise TypeError("cleared_tasks должен быть tuple имён задач")
        if len(self.cleared_tasks) != len(set(self.cleared_tasks)):
            raise ValueError("cleared_tasks не должен содержать дубликаты")
        if type(self.verified) is not bool:
            raise TypeError("verified должен быть bool")


class LifecycleOutcome(StrEnum):
    STARTED = "started"
    STOPPED = "stopped"
    ALREADY_RUNNING = "already_running"
    ALREADY_STOPPED = "already_stopped"


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    instance: str
    outcome: LifecycleOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        if not isinstance(self.outcome, LifecycleOutcome):
            raise TypeError("outcome должен быть LifecycleOutcome")


@dataclass(frozen=True, slots=True)
class EmulatorRestartResult:
    instance: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")


@dataclass(frozen=True, slots=True)
class GameApplicationState:
    """Подтверждённое состояние приложения на exact ADB target."""

    adb_ready: bool
    game_running: bool | None
    game_foreground: bool | None

    def __post_init__(self) -> None:
        if type(self.adb_ready) is not bool:
            raise TypeError("adb_ready должен быть bool")
        if self.game_running is not None and type(self.game_running) is not bool:
            raise TypeError("game_running должен быть bool или None")
        if self.game_foreground is not None and type(self.game_foreground) is not bool:
            raise TypeError("game_foreground должен быть bool или None")


@dataclass(frozen=True, slots=True)
class GameRuntimeRestartResult:
    """Подтверждённое восстановление эмулятора и игрового приложения."""

    instance: str
    emulator_verified: bool
    adb_ready: bool
    game_running: bool
    game_foreground: bool

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")
        for name in (
            "emulator_verified",
            "adb_ready",
            "game_running",
            "game_foreground",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} должен быть bool")
            if getattr(self, name) is not True:
                raise ValueError(f"{name} должен подтверждать успешное состояние")


@dataclass(frozen=True, slots=True)
class AdbRestartResult:
    instance: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or not self.instance:
            raise ValueError("instance должен быть непустой строкой")


@dataclass(frozen=True, slots=True)
class MediaFrame:
    """Кадр изображения до преобразования в MCP content/Base64."""

    data: bytes
    media_type: str
    metadata: Mapping[str, FrozenPayload] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("data должен быть непустым bytes")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("media_type должен быть непустой строкой")
        frozen = freeze_payload(self.metadata, field_name="metadata")
        if not isinstance(frozen, Mapping):
            raise TypeError("metadata должен быть mapping")
        object.__setattr__(self, "metadata", frozen)


__all__ = [
    "REDACTED_CONFIG_VALUE",
    "AdbRestartResult",
    "ConfigArgumentDefinition",
    "ConfigSnapshot",
    "ConfigUpdateRequest",
    "ConfigUpdateResult",
    "CurrentTaskSnapshot",
    "DashboardResource",
    "DashboardResources",
    "EmulatorRestartResult",
    "FrozenPayload",
    "GameApplicationState",
    "GameRuntimeRestartResult",
    "LifecycleOutcome",
    "LifecycleResult",
    "MediaFrame",
    "RuntimeLogTail",
    "ScheduleTaskRequest",
    "ScheduleTaskResult",
    "SchedulerEntry",
    "SchedulerQueueClearResult",
    "SchedulerQueueSnapshot",
    "freeze_payload",
    "thaw_payload",
]
