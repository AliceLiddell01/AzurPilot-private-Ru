"""Read-only application services для экземпляров и каталога задач."""

from __future__ import annotations

from module.application.errors import (
    ApplicationError,
    InvalidRequestError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from module.application.models import (
    InstanceReference,
    InstanceStatus,
    RuntimeState,
    TaskMetadata,
    TaskSummary,
)
from module.application.ports import InstanceRuntimeReader, TaskCatalogReader


def _validated_name(value: object, *, resource: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"Имя {resource} должно быть непустой строкой.")
    return value.strip()


class InstanceQueryService:
    """Запросы экземпляров без передачи runtime-объектов наружу."""

    def __init__(self, reader: InstanceRuntimeReader):
        self._reader = reader

    def list_instances(self) -> tuple[InstanceReference, ...]:
        names = self._read_names()
        return tuple(InstanceReference(name=name) for name in names)

    def list_statuses(self) -> tuple[InstanceStatus, ...]:
        return tuple(self._read_status(name) for name in self._read_names())

    def get_status(self, name: str) -> InstanceStatus:
        validated = _validated_name(name, resource="экземпляра")
        if validated not in self._read_names():
            raise ResourceNotFoundError(f"Экземпляр {validated!r} не найден.")
        return self._read_status(validated)

    def _read_names(self) -> tuple[str, ...]:
        try:
            names = self._reader.list_instance_names()
            if not isinstance(names, tuple):
                raise TypeError("reader должен вернуть tuple")
            if any(not isinstance(name, str) or not name.strip() for name in names):
                raise TypeError("reader вернул некорректное имя")
            if any(
                _validated_name(name, resource="экземпляра") != name for name in names
            ):
                raise TypeError("reader вернул неканоническое имя")
            if len(set(names)) != len(names):
                raise TypeError("reader вернул повторяющиеся имена")
            return names
        except ApplicationError:
            raise
        except Exception:  # noqa: BLE001 - application boundary sanitizes reader failures.
            raise ServiceUnavailableError(
                "Не удалось получить список экземпляров."
            ) from None

    def _read_status(self, name: str) -> InstanceStatus:
        try:
            snapshot = self._reader.read_instance_status(name)
            if not isinstance(snapshot.running, bool):
                raise TypeError("running должен быть bool")
            if isinstance(snapshot.state_code, bool) or not isinstance(
                snapshot.state_code, int
            ):
                raise TypeError("state_code должен быть int")
            state = RuntimeState(snapshot.state_code)
            return InstanceStatus(name=name, running=snapshot.running, state=state)
        except ApplicationError:
            raise
        except Exception:  # noqa: BLE001 - application boundary sanitizes reader failures.
            raise ServiceUnavailableError(
                f"Не удалось получить статус экземпляра {name!r}."
            ) from None


class TaskCatalogService:
    """Типизированная проекция generated task metadata."""

    def __init__(self, reader: TaskCatalogReader):
        self._reader = reader

    def list_tasks(self) -> tuple[TaskSummary, ...]:
        try:
            tasks = self._reader.list_task_summaries()
            if not isinstance(tasks, tuple) or any(
                not isinstance(task, TaskSummary) for task in tasks
            ):
                raise TypeError("reader вернул некорректный каталог задач")
            if len({task.name for task in tasks}) != len(tasks):
                raise TypeError("reader вернул повторяющиеся задачи")
            return tasks
        except ApplicationError:
            raise
        except Exception:  # noqa: BLE001 - application boundary sanitizes reader failures.
            raise ServiceUnavailableError("Не удалось получить список задач.") from None

    def get_task_metadata(self, name: str) -> TaskMetadata:
        validated = _validated_name(name, resource="задачи")
        try:
            task = self._reader.read_task_metadata(validated)
            if task is None:
                raise ResourceNotFoundError(f"Задача {validated!r} не найдена.")
            if not isinstance(task, TaskMetadata):
                raise TypeError("reader вернул некорректные metadata задачи")
            return task
        except ApplicationError:
            raise
        except Exception:  # noqa: BLE001 - application boundary sanitizes reader failures.
            raise ServiceUnavailableError(
                f"Не удалось получить metadata задачи {validated!r}."
            ) from None
