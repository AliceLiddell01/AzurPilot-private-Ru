from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from module.application import InstanceQueryService, RuntimeState, TaskCatalogService
from module.application.legacy_adapters import (
    GeneratedTaskCatalogAdapter,
    LegacyInstanceRuntimeAdapter,
)
from module.application.models import TaskMetadata
from module.config import locale as config_locale
from module.config import utils as config_utils
from module.config.mcp_helper import McpConfigHelper
from module.webui import worker_registry


def _legacy_task_dict(task: TaskMetadata) -> dict[str, object]:
    return {
        "task_name": task.name,
        "display_name": task.display_name,
        "help": task.help,
        "groups": {
            group.name: {
                "display_name": group.display_name,
                "help": group.help,
                "arguments": {
                    argument.name: {
                        "display_name": argument.display_name,
                        "help": argument.help,
                        "type": argument.input_type,
                        "default": (
                            list(argument.default)
                            if isinstance(argument.default, tuple)
                            else argument.default
                        ),
                        "options": (
                            {
                                option.value: option.display_name
                                for option in argument.options
                            }
                            if argument.options
                            else None
                        ),
                    }
                    for argument in group.arguments
                },
            }
            for group in task.groups
        },
    }


def test_generated_task_catalog_matches_current_mcp_projection_for_every_task():
    generated_paths = (
        Path(config_utils.filepath_args("args")),
        Path(config_utils.filepath_i18n(config_locale.UI_LOCALE)),
    )
    assert all(path.is_file() for path in generated_paths), (
        f"Сначала требуется генерация config/i18n: {generated_paths}"
    )
    legacy = McpConfigHelper()
    service = TaskCatalogService(GeneratedTaskCatalogAdapter.from_generated_sources())

    task_names = tuple(task.name for task in service.list_tasks())
    assert task_names == tuple(legacy.get_tasks())
    for task_name in task_names:
        assert _legacy_task_dict(service.get_task_metadata(task_name)) == (
            legacy.get_task_details(task_name)
        )


def test_legacy_instance_adapter_preserves_current_instance_order(monkeypatch):
    names = ["secondary", "ap", "third"]
    monkeypatch.setattr(config_utils, "alas_instance", lambda: list(names))
    adapter = LegacyInstanceRuntimeAdapter()

    assert adapter.list_instance_names() == tuple(names)


def test_legacy_runtime_adapter_reads_alive_before_state_without_leaking_manager():
    events: list[str] = []
    received_names: list[str] = []

    class Manager:
        @property
        def alive(self) -> bool:
            events.append("alive")
            return True

        @property
        def state(self) -> int:
            events.append("state")
            return 1

    def manager_factory(name: str) -> Manager:
        received_names.append(name)
        return Manager()

    adapter = LegacyInstanceRuntimeAdapter(
        list_instances=lambda: ("ap",),
        manager_factory=manager_factory,
    )
    status = InstanceQueryService(adapter).get_status("ap")

    assert received_names == ["ap"]
    assert events == ["alive", "state"]
    assert status.name == "ap"
    assert status.running is True
    assert status.state is RuntimeState.RUNNING


def test_default_legacy_runtime_adapter_reads_registry_without_process_housekeeping():
    adapter = LegacyInstanceRuntimeAdapter(list_instances=lambda: ("ap",))

    with (
        patch.object(
            worker_registry,
            "get_worker_read_only",
            return_value=None,
        ) as read_only,
        patch.object(worker_registry, "process_matches") as matches,
        patch.object(worker_registry, "_locked_registry", side_effect=AssertionError) as locked,
    ):
        status = InstanceQueryService(adapter).get_status("ap")

    read_only.assert_called_once_with("ap")
    matches.assert_not_called()
    locked.assert_not_called()
    assert status.running is False
    assert status.state is RuntimeState.STOPPED
