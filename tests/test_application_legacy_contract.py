from __future__ import annotations

from module.application import InstanceQueryService, RuntimeState, TaskCatalogService
from module.application.legacy_adapters import (
    GeneratedTaskCatalogAdapter,
    LegacyInstanceRuntimeAdapter,
)
from module.application.models import TaskMetadata
from module.config.mcp_helper import McpConfigHelper
from module.config import utils as config_utils


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

    class Manager:
        @property
        def alive(self) -> bool:
            events.append("alive")
            return True

        @property
        def state(self) -> int:
            events.append("state")
            return 1

    adapter = LegacyInstanceRuntimeAdapter(
        list_instances=lambda: ("ap",),
        manager_factory=lambda _name: Manager(),
    )
    status = InstanceQueryService(adapter).get_status("ap")

    assert events == ["alive", "state"]
    assert status.running is True
    assert status.state is RuntimeState.RUNNING
