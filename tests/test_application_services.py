from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from module.application import (
    InstanceQueryService,
    InvalidRequestError,
    ResourceNotFoundError,
    RuntimeState,
    ServiceUnavailableError,
    TaskCatalogService,
    TaskMetadata,
    TaskSummary,
)
from module.application.ports import RuntimeSnapshot


class _InstanceReader:
    def __init__(self):
        self.status_reads: list[str] = []

    def list_instance_names(self) -> tuple[str, ...]:
        return ("ap", "secondary")

    def read_instance_status(self, name: str) -> RuntimeSnapshot:
        self.status_reads.append(name)
        return RuntimeSnapshot(
            running=name == "ap", state_code=1 if name == "ap" else 2
        )


class _TaskReader:
    task = TaskMetadata(name="Alas", display_name="Alas", help="help", groups=())

    def list_task_summaries(self) -> tuple[TaskSummary, ...]:
        return (TaskSummary(name="Alas", display_name="Alas", help="help"),)

    def read_task_metadata(self, name: str) -> TaskMetadata | None:
        return self.task if name == self.task.name else None


def test_instance_service_returns_immutable_typed_read_models():
    reader = _InstanceReader()
    service = InstanceQueryService(reader)

    assert tuple(item.name for item in service.list_instances()) == ("ap", "secondary")
    assert service.get_status("ap").state is RuntimeState.RUNNING
    assert service.list_statuses()[1].state is RuntimeState.STOPPED
    assert reader.status_reads == ["ap", "ap", "secondary"]

    with pytest.raises(FrozenInstanceError):
        service.get_status("ap").name = "changed"  # type: ignore[misc]


def test_instance_service_has_predictable_input_and_not_found_errors():
    service = InstanceQueryService(_InstanceReader())

    with pytest.raises(InvalidRequestError) as invalid:
        service.get_status(" ")
    with pytest.raises(ResourceNotFoundError) as missing:
        service.get_status("missing")

    assert invalid.value.code == "invalid_request"
    assert missing.value.code == "not_found"


def test_instance_service_hides_internal_reader_failure():
    class BrokenReader(_InstanceReader):
        def list_instance_names(self) -> tuple[str, ...]:
            raise RuntimeError("C:/private/config.json token=secret")

    with pytest.raises(ServiceUnavailableError) as failure:
        InstanceQueryService(BrokenReader()).list_instances()

    assert failure.value.code == "service_unavailable"
    assert "private" not in str(failure.value)
    assert "secret" not in str(failure.value)


def test_instance_service_rejects_noncanonical_reader_name():
    class NoncanonicalReader(_InstanceReader):
        def list_instance_names(self) -> tuple[str, ...]:
            return (" ap ",)

    with pytest.raises(ServiceUnavailableError):
        InstanceQueryService(NoncanonicalReader()).list_instances()


def test_instance_service_rejects_unknown_runtime_state_fail_closed():
    class UnknownStateReader(_InstanceReader):
        def read_instance_status(self, name: str) -> RuntimeSnapshot:
            return RuntimeSnapshot(running=False, state_code=99)

    with pytest.raises(ServiceUnavailableError):
        InstanceQueryService(UnknownStateReader()).get_status("ap")


def test_task_service_returns_typed_metadata_and_not_found_error():
    service = TaskCatalogService(_TaskReader())

    assert service.list_tasks()[0].name == "Alas"
    assert service.get_task_metadata("Alas") == _TaskReader.task
    with pytest.raises(ResourceNotFoundError):
        service.get_task_metadata("missing")
    with pytest.raises(InvalidRequestError):
        service.get_task_metadata("")
