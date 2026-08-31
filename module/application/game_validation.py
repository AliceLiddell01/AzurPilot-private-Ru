"""Общая валидация и безопасные error boundaries game services."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from module.application.errors import (
    ApplicationError,
    ConfigurationValidationError,
    InvalidRequestError,
    OperationFailedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from module.application.game_models import ConfigArgumentDefinition, freeze_payload
from module.application.game_ports import SchedulerTaskReader
from module.application.ports import InstanceRuntimeReader

MAX_RECENT_LOG_LINES = 10_000
MAX_SCHEDULABLE_TASKS = 512
UNKNOWN_TASK = "Unknown"
_INVALID_NAME_CHARS = frozenset("./\\\x00:*?\"<>|")


def validated_name(value: object, *, resource: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"Имя {resource} должно быть строкой.")
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or any(char in _INVALID_NAME_CHARS for char in normalized)
        or len(normalized) > 128
    ):
        raise InvalidRequestError(f"Имя {resource} содержит недопустимое значение.")
    return normalized


def validated_segment(value: object, *, resource: str) -> str:
    return validated_name(value, resource=resource)


def known_instance(reader: InstanceRuntimeReader, value: object) -> str:
    instance = validated_name(value, resource="экземпляра")
    try:
        names = reader.list_instance_names()
        if not isinstance(names, tuple) or any(
            not isinstance(name, str)
            or name != name.strip()
            or not name
            or name in {".", ".."}
            or any(char in _INVALID_NAME_CHARS for char in name)
            for name in names
        ):
            raise TypeError("reader вернул некорректный список экземпляров")
        if len(names) != len(set(names)):
            raise TypeError("reader вернул повторяющиеся экземпляры")
    except ApplicationError:
        raise ServiceUnavailableError("Не удалось проверить экземпляр.") from None
    except Exception:  # noqa: BLE001 - application boundary sanitizes legacy failures.
        raise ServiceUnavailableError("Не удалось проверить экземпляр.") from None
    if instance not in names:
        raise ResourceNotFoundError("Экземпляр не найден.")
    return instance


def scheduler_tasks(reader: SchedulerTaskReader) -> tuple[str, ...]:
    try:
        tasks = reader.list_schedulable_task_names()
        if not isinstance(tasks, tuple) or len(tasks) > MAX_SCHEDULABLE_TASKS or any(
            not isinstance(task, str)
            or task != task.strip()
            or not task
            or task in {".", ".."}
            or any(char in _INVALID_NAME_CHARS for char in task)
            for task in tasks
        ):
            raise TypeError("registry вернул некорректные задачи")
        if len(tasks) != len(set(tasks)):
            raise TypeError("registry вернул повторяющиеся задачи")
        return tasks
    except ApplicationError:
        raise ServiceUnavailableError(
            "Не удалось получить реестр задач scheduler."
        ) from None
    except Exception:  # noqa: BLE001
        raise ServiceUnavailableError(
            "Не удалось получить реестр задач scheduler."
        ) from None


def safe_read(operation: str, callback: Callable[[], object]) -> object:
    try:
        return callback()
    except ApplicationError:
        raise ServiceUnavailableError(f"Не удалось выполнить чтение: {operation}.") from None
    except Exception:  # noqa: BLE001 - public result must not expose adapter details.
        raise ServiceUnavailableError(f"Не удалось выполнить чтение: {operation}.") from None


def safe_control(operation: str, callback: Callable[[], object]) -> object:
    try:
        return callback()
    except ApplicationError:
        raise OperationFailedError(f"Не удалось выполнить операцию: {operation}.") from None
    except Exception:  # noqa: BLE001 - public result must not expose adapter details.
        raise OperationFailedError(f"Не удалось выполнить операцию: {operation}.") from None


def require_bool(value: object, *, operation: str) -> bool:
    if type(value) is not bool:
        raise OperationFailedError(f"Операция {operation} не подтвердила результат.")
    return value


def same_value(left: object, right: object) -> bool:
    """Сравнить option values без bool/int coalescing Python."""

    if type(left) is not type(right):
        return False
    return left == right


def validate_config_value(
    definition: ConfigArgumentDefinition,
    value: object,
) -> None:
    if definition.sensitive:
        raise ConfigurationValidationError(
            "Изменение чувствительного параметра запрещено."
        )
    try:
        frozen = freeze_payload(value, field_name="value")
    except TypeError:
        raise ConfigurationValidationError(
            "Значение конфигурации содержит неподдерживаемый тип."
        ) from None

    if definition.options and not any(
        same_value(frozen, option) for option in definition.options
    ):
        raise ConfigurationValidationError(
            "Значение конфигурации отсутствует среди допустимых вариантов."
        )

    if frozen is None:
        if definition.default is None or any(
            option is None for option in definition.options
        ):
            return
        raise ConfigurationValidationError(
            "Для этого параметра значение null недопустимо."
        )

    input_type = definition.input_type.casefold()
    if input_type in {"checkbox", "state"} and type(frozen) is not bool:
        raise ConfigurationValidationError(
            "Для этого параметра требуется логическое значение."
        )
    if input_type in {"number", "int", "integer", "float"} and (
        type(frozen) not in {int, float}
    ):
        raise ConfigurationValidationError(
            "Для этого параметра требуется числовое значение."
        )

    default = definition.default
    if isinstance(default, Mapping) and not isinstance(frozen, Mapping):
        raise ConfigurationValidationError(
            "Для этого параметра требуется структурированное значение."
        )
    if isinstance(default, (list, tuple)) and not isinstance(frozen, tuple):
        raise ConfigurationValidationError(
            "Для этого параметра требуется список значений."
        )
    if type(default) is bool and type(frozen) is not bool:
        raise ConfigurationValidationError(
            "Для этого параметра требуется логическое значение."
        )
    if type(default) is str and type(frozen) is not str:
        raise ConfigurationValidationError(
            "Для этого параметра требуется строковое значение."
        )
    if type(default) in {int, float} and type(frozen) not in {int, float}:
        raise ConfigurationValidationError(
            "Для этого параметра требуется числовое значение."
        )

    validation = definition.validation
    if (
        isinstance(validation, tuple)
        and len(validation) == 2
        and all(type(item) in {int, float} for item in validation)
        and (
            type(frozen) not in {int, float}
            or not validation[0] <= frozen <= validation[1]
        )
    ):
        raise ConfigurationValidationError(
            "Значение конфигурации вне допустимого диапазона."
        )


__all__ = [
    "MAX_RECENT_LOG_LINES",
    "MAX_SCHEDULABLE_TASKS",
    "UNKNOWN_TASK",
    "known_instance",
    "require_bool",
    "safe_control",
    "safe_read",
    "same_value",
    "scheduler_tasks",
    "validate_config_value",
    "validated_name",
    "validated_segment",
]
