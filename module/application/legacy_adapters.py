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

from module.application.game_models import (
    REDACTED_CONFIG_VALUE,
    ConfigArgumentDefinition,
    DashboardResource,
    DashboardResources,
    freeze_payload,
)
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
        *,
        excluded_groups: frozenset[str] = frozenset({"Storage"}),
    ):
        if not isinstance(args_data, Mapping) or not isinstance(i18n_data, Mapping):
            raise TypeError("generated sources должны быть mapping")
        self._args_data = args_data
        self._i18n_data = i18n_data
        self._excluded_groups = excluded_groups
        self._sensitive_paths = _collect_sensitive_paths(args_data)
        tasks = tuple(
            _project_task(task_name, task_data, i18n_data, excluded_groups)
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

    def read_argument_metadata(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> Mapping[str, Any] | None:
        """Вернуть raw metadata из того же generated args snapshot."""
        if group in self._excluded_groups:
            return None
        task_data = self._args_data.get(task)
        if not isinstance(task_data, Mapping):
            return None
        group_data = task_data.get(group)
        if not isinstance(group_data, Mapping):
            return None
        argument_data = group_data.get(argument)
        if not isinstance(argument_data, Mapping):
            return None
        return argument_data

    def read_argument_definition(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> ConfigArgumentDefinition | None:
        """Вернуть typed definition из того же generated args snapshot."""
        argument_data = self.read_argument_metadata(task, group, argument)
        if argument_data is None:
            return None
        raw_type = argument_data.get("type", "input")
        input_type = raw_type if isinstance(raw_type, str) else "input"
        raw_options = argument_data.get("option", ())
        options = tuple(
            freeze_payload(option, field_name="config option")
            for option in _options(raw_options)
        )
        try:
            default = freeze_payload(
                argument_data.get("value"),
                field_name="config default",
            )
            validation = freeze_payload(
                argument_data.get("validate"),
                field_name="config validation",
            )
        except TypeError:
            raise TypeError("generated config default имеет неподдерживаемый тип") from None
        return ConfigArgumentDefinition(
            task=task,
            group=group,
            argument=argument,
            input_type=input_type,
            default=default,
            options=options,
            validation=validation,
            sensitive=argument_data.get("sensitive") is True,
        )

    def redact_config(self, config_data: Mapping[str, Any]) -> Mapping[str, Any]:
        """Удалить чувствительные значения по generated metadata."""
        if not isinstance(config_data, Mapping):
            raise TypeError("config должен быть mapping")
        redacted = _copy_mapping(config_data)
        for path in self._sensitive_paths:
            _replace_mapping_path(redacted, path, REDACTED_CONFIG_VALUE)
        return redacted

    def list_schedulable_task_names(self) -> tuple[str, ...]:
        """Использовать canonical generated Scheduler.Command registry."""
        from module.config.task_priority import get_scheduler_tasks

        return tuple(
            task
            for task in get_scheduler_tasks(dict(self._args_data))
            if task in self._args_data
        )

    def read_dashboard_resources(
        self,
        config_data: Mapping[str, Any],
    ) -> DashboardResources:
        """Спроецировать dashboard в immutable application model."""
        dashboard = config_data.get("Dashboard", {})
        if dashboard is None:
            dashboard = {}
        if not isinstance(dashboard, Mapping):
            raise TypeError("Dashboard должен быть mapping")
        gui_data = self._i18n_data.get("Gui", {})
        dashboard_i18n = (
            gui_data.get("Dashboard", {})
            if isinstance(gui_data, Mapping)
            else {}
        )
        if not isinstance(dashboard_i18n, Mapping):
            dashboard_i18n = {}
        resources = []
        for key, raw_data in dashboard.items():
            if not isinstance(key, str) or not isinstance(raw_data, Mapping):
                continue
            if "Value" not in raw_data:
                continue
            label = dashboard_i18n.get(key, key)
            if not isinstance(label, str):
                label = key
            resources.append(
                DashboardResource(
                    key=key,
                    label=label,
                    value=freeze_payload(raw_data["Value"], field_name=f"Dashboard.{key}.Value"),
                    limit=(
                        freeze_payload(raw_data["Limit"], field_name=f"Dashboard.{key}.Limit")
                        if "Limit" in raw_data
                        else None
                    ),
                    total=(
                        freeze_payload(raw_data["Total"], field_name=f"Dashboard.{key}.Total")
                        if "Total" in raw_data
                        else None
                    ),
                    last_update=(
                        freeze_payload(raw_data["Record"], field_name=f"Dashboard.{key}.Record")
                        if "Record" in raw_data
                        else None
                    ),
                )
            )
        return DashboardResources(items=tuple(resources))


def _project_task(
    task_name: str,
    task_data: object,
    i18n_data: Mapping[str, Any],
    excluded_groups: frozenset[str],
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
        if group_name not in excluded_groups
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


def _collect_sensitive_paths(args_data: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []
    for task, task_data in args_data.items():
        if not isinstance(task, str) or not isinstance(task_data, Mapping):
            continue
        for group, group_data in task_data.items():
            if not isinstance(group, str) or not isinstance(group_data, Mapping):
                continue
            for argument, argument_data in group_data.items():
                if (
                    isinstance(argument, str)
                    and isinstance(argument_data, Mapping)
                    and argument_data.get("sensitive") is True
                ):
                    paths.append((task, group, argument))
    return tuple(paths)


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _copy_payload(item) for key, item in value.items()}


def _copy_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_payload(item) for item in value)
    return value


def _replace_mapping_path(
    data: dict[str, Any],
    path: tuple[str, ...],
    replacement: object,
) -> None:
    current: object = data
    for key in path[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(key)
    if isinstance(current, dict) and path[-1] in current:
        current[path[-1]] = replacement


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
