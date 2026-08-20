"""Транспортно-независимый прикладной слой AzurPilot.

Импорт пакета намеренно не создаёт runtime-объекты и не читает конфигурацию.
Legacy-адаптеры подключаются вызывающей стороной явно.
"""

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
    TaskArgumentMetadata,
    TaskGroupMetadata,
    TaskMetadata,
    TaskOption,
    TaskSummary,
)
from module.application.services import InstanceQueryService, TaskCatalogService

__all__ = (
    "ApplicationError",
    "InstanceQueryService",
    "InstanceReference",
    "InstanceStatus",
    "InvalidRequestError",
    "ResourceNotFoundError",
    "RuntimeState",
    "ServiceUnavailableError",
    "TaskArgumentMetadata",
    "TaskCatalogService",
    "TaskGroupMetadata",
    "TaskMetadata",
    "TaskOption",
    "TaskSummary",
)
