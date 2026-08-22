"""Ограниченные адаптеры к текущим generated и WebUI-owned источникам.

Модуль не выполняет I/O при импорте. Зависимости legacy runtime загружаются
только при явном вызове адаптера. ProcessManager остаётся физически и логически
принадлежащим `module.webui`; этот долг должен исчезнуть на будущей стадии
переноса runtime ownership, а не копированием менеджера сюда.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

from module.application.models import (
    MetadataValue,
    TaskArgumentMetadata,
    TaskGroupMetadata,
    TaskMetadata,
    TaskOption,
    TaskSummary,
)
from module.application.ports import RuntimeSnapshot


class _LegacyManager(Protocol):
    @property
    def alive(self) -> bool: ...

    @property
    def state(self) -> int: ...


class LegacyInstanceRuntimeAdapter:
    """Проецирует legacy instance/runtime API в узкий read-only порт."""

    def __init__(
        self,
        *,
        list_instances: Callable[[], Sequence[str]] | None = None,
        manager_factory: Callable[[str], _LegacyManager] | None = None,
    ):
        self._list_instances = list_instances
        self._manager_factory = manager_factory

    def list_instance_names(self) -> tuple[str, ...]:
        provider = self._list_instances or self._default_list_instances
        return tuple(provider())

    def read_instance_status(self, name: str) -> RuntimeSnapshot:
        factory = self._manager_factory or self._default_manager_factory
        manager = factory(name)
        # Порядок повторяет текущий MCP contract. Оба property-read могут
        # выполнить housekeeping устаревшего process registry.
        running = manager.alive
        state_code = manager.state
        return RuntimeSnapshot(running=running, state_code=state_code)

    @staticmethod
    def _default_list_instances() -> Sequence[str]:
        config_utils = importlib.import_module("module.config.utils")
        return config_utils.alas_instance()

    @staticmethod
    def _default_manager_factory(name: str) -> _LegacyManager:
        process_manager = importlib.import_module("module.webui.process_manager")
        return process_manager.ProcessManager.get_manager(name)


class GeneratedTaskCatalogAdapter:
    """Снимок каталога из canonical generated args/i18n sources."""

    def __init__(
        self,
        args_data: Mapping[str, Any],
        i18n_data: Mapping[str, Any],
    ):
        tasks = tuple(
            _project_task(task_name, task_data, i18n_data)
            for task_name, task_data in args_data.items()
        )
        self._tasks = tasks
        self._by_name = MappingProxyType({task.name: task for task in tasks})

    @classmethod
    def from_generated_sources(cls) -> GeneratedTaskCatalogAdapter:
        locale = importlib.import_module("module.config.locale")
        config_utils = importlib.import_module("module.config.utils")
        args_data = config_utils.read_file(config_utils.filepath_args("args"))
        i18n_data = config_utils.read_file(config_utils.filepath_i18n(locale.UI_LOCALE))
        return cls(args_data=args_data, i18n_data=i18n_data)

    def list_task_summaries(self) -> tuple[TaskSummary, ...]:
        return tuple(
            TaskSummary(
                name=task.name,
                display_name=task.display_name,
                help=task.help,
            )
            for task in self._tasks
        )

    def read_task_metadata(self, name: str) -> TaskMetadata | None:
        return self._by_name.get(name)


def _project_task(
    task_name: str,
    task_data: object,
    i18n_data: Mapping[str, Any],
) -> TaskMetadata:
    groups_data = _mapping(task_data, f"task {task_name}")
    task_i18n = _mapping(
        _mapping(i18n_data.get("Task", {}), "Task i18n").get(task_name, {}),
        f"Task.{task_name} i18n",
    )
    spec_i18n = _mapping(i18n_data.get(task_name, {}), f"{task_name} i18n")
    groups = tuple(
        _project_group(group_name, group_data, spec_i18n)
        for group_name, group_data in groups_data.items()
        if group_name != "Storage"
    )
    return TaskMetadata(
        name=task_name,
        display_name=_translated(task_i18n.get("name"), task_name),
        help=_translated(task_i18n.get("help"), ""),
        groups=groups,
    )


def _project_group(
    group_name: str,
    group_data: object,
    spec_i18n: Mapping[str, Any],
) -> TaskGroupMetadata:
    arguments_data = _mapping(group_data, f"group {group_name}")
    group_i18n = _mapping(spec_i18n.get(group_name, {}), f"{group_name} i18n")
    group_info = _mapping(group_i18n.get("_info", {}), f"{group_name}._info")
    arguments = tuple(
        _project_argument(arg_name, arg_data, group_i18n)
        for arg_name, arg_data in arguments_data.items()
    )
    return TaskGroupMetadata(
        name=group_name,
        display_name=_translated(group_info.get("name"), group_name),
        help=_translated(group_info.get("help"), ""),
        arguments=arguments,
    )


def _project_argument(
    arg_name: str,
    arg_data: object,
    group_i18n: Mapping[str, Any],
) -> TaskArgumentMetadata:
    metadata = _mapping(arg_data, f"argument {arg_name}")
    arg_i18n = _mapping(group_i18n.get(arg_name, {}), f"{arg_name} i18n")
    options = tuple(
        TaskOption(
            value=str(option),
            display_name=_translated(arg_i18n.get(str(option)), str(option)),
        )
        for option in _options(metadata.get("option", ()))
    )
    return TaskArgumentMetadata(
        name=arg_name,
        display_name=_translated(arg_i18n.get("name"), arg_name),
        help=_translated(arg_i18n.get("help"), ""),
        input_type=_translated(metadata.get("type"), "input"),
        default=_freeze_default(metadata.get("value")),
        options=options,
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} должен быть mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} содержит нестроковый ключ")
    return value


def _translated(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _options(value: object) -> Sequence[object]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(value.keys())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    raise TypeError("option должен быть list или mapping")


def _freeze_default(value: object) -> MetadataValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    ):
        return tuple(value)  # type: ignore[return-value]
    raise TypeError("default metadata содержит неподдерживаемое составное значение")
