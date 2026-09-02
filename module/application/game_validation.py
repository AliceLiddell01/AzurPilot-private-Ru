"""Общая валидация и безопасные error boundaries game services."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from module.application.errors import (
    ConfigurationValidationError,
    InvalidRequestError,
    OperationFailedError,
    PostconditionFailedError,
    PreconditionFailedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from module.application.game_models import ConfigArgumentDefinition, freeze_payload
from module.application.game_ports import SchedulerTaskReader
from module.application.ports import InstanceRuntimeReader

MAX_RECENT_LOG_LINES = 10_000
MAX_SCHEDULABLE_TASKS = 512
MAX_NAME_LENGTH = 128
MAX_CONFIG_VALUE_DEPTH = 8
MAX_CONFIG_VALUE_ITEMS = 256
MAX_CONFIG_VALUE_STRING_LENGTH = 4096
MAX_CONFIG_VALUE_MAGNITUDE = 10**12
UNKNOWN_TASK = "Unknown"
INVALID_NAME_CHARS = frozenset("./\\\x00:*?\"<>|")


def validated_name(value: object, *, resource: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequestError(f"Имя {resource} должно быть строкой.")
    normalized = value.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or any(char in INVALID_NAME_CHARS for char in normalized)
        or len(normalized) > MAX_NAME_LENGTH
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
            or any(char in INVALID_NAME_CHARS for char in name)
            for name in names
        ):
            raise TypeError("reader вернул некорректный список экземпляров")
        if len(names) != len(set(names)):
            raise TypeError("reader вернул повторяющиеся экземпляры")
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
            or any(char in INVALID_NAME_CHARS for char in task)
            for task in tasks
        ):
            raise TypeError("registry вернул некорректные задачи")
        if len(tasks) != len(set(tasks)):
            raise TypeError("registry вернул повторяющиеся задачи")
        return tasks
    except Exception:  # noqa: BLE001
        raise ServiceUnavailableError(
            "Не удалось получить реестр задач scheduler."
        ) from None


def safe_read(operation: str, callback: Callable[[], object]) -> object:
    try:
        return callback()
    except Exception:  # noqa: BLE001 - public result must not expose adapter details.
        raise ServiceUnavailableError(f"Не удалось выполнить чтение: {operation}.") from None


def safe_control(operation: str, callback: Callable[[], object]) -> object:
    try:
        return callback()
    except (PreconditionFailedError, PostconditionFailedError):
        raise
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


def _validate_bounded_payload(
    value: object,
    *,
    depth: int = 0,
    allow_extended_scalars: bool,
) -> None:
    """Проверить размер и тип динамического значения до его заморозки."""

    if depth > MAX_CONFIG_VALUE_DEPTH:
        raise ValueError("Значение конфигурации слишком глубоко вложено")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_CONFIG_VALUE_MAGNITUDE:
            raise ValueError("Число конфигурации выходит за безопасный диапазон")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Число конфигурации не является конечным")
        if abs(value) > MAX_CONFIG_VALUE_MAGNITUDE:
            raise ValueError("Число конфигурации выходит за безопасный диапазон")
        return
    if isinstance(value, str):
        if len(value) > MAX_CONFIG_VALUE_STRING_LENGTH:
            raise ValueError("Строка конфигурации слишком длинная")
        return
    if allow_extended_scalars and isinstance(value, (date, datetime, Decimal)):
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("Число конфигурации не является конечным")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_CONFIG_VALUE_ITEMS:
            raise ValueError("Объект конфигурации содержит слишком много полей")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > MAX_NAME_LENGTH:
                raise ValueError("Ключ конфигурации имеет недопустимый размер")
            _validate_bounded_payload(
                item,
                depth=depth + 1,
                allow_extended_scalars=allow_extended_scalars,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_CONFIG_VALUE_ITEMS:
            raise ValueError("Массив конфигурации содержит слишком много элементов")
        for item in value:
            _validate_bounded_payload(
                item,
                depth=depth + 1,
                allow_extended_scalars=allow_extended_scalars,
            )
        return
    raise ValueError("Значение конфигурации содержит неподдерживаемый тип")


def validate_json_value(value: object) -> None:
    """Проверить bounded JSON value для внешнего mutation contract."""

    _validate_bounded_payload(value, allow_extended_scalars=False)


def validate_config_payload(value: object) -> None:
    """Проверить bounded payload application config, включая datetime values."""

    _validate_bounded_payload(value, allow_extended_scalars=True)


def _is_valid_datetime_value(value: object) -> bool:
    """Проверить legacy datetime contract без изменения исходного значения."""

    if isinstance(value, datetime):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_config_value(
    definition: ConfigArgumentDefinition,
    value: object,
) -> None:
    if definition.sensitive:
        raise ConfigurationValidationError(
            "Изменение чувствительного параметра запрещено."
        )
    try:
        validate_config_payload(value)
    except ValueError:
        raise ConfigurationValidationError(
            "Значение конфигурации не прошло bounded-проверку."
        ) from None
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
    if input_type == "datetime" and not _is_valid_datetime_value(frozen):
        raise ConfigurationValidationError(
            "Для этого параметра требуется корректное значение даты и времени."
        )
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
    ):
        if type(frozen) not in {int, float}:
            raise ConfigurationValidationError(
                "Для этого параметра требуется числовое значение."
            )
        if not validation[0] <= frozen <= validation[1]:
            raise ConfigurationValidationError(
                "Значение конфигурации вне допустимого диапазона."
            )


__all__ = [
    "INVALID_NAME_CHARS",
    "MAX_CONFIG_VALUE_DEPTH",
    "MAX_CONFIG_VALUE_ITEMS",
    "MAX_CONFIG_VALUE_MAGNITUDE",
    "MAX_CONFIG_VALUE_STRING_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_RECENT_LOG_LINES",
    "MAX_SCHEDULABLE_TASKS",
    "UNKNOWN_TASK",
    "known_instance",
    "require_bool",
    "safe_control",
    "safe_read",
    "same_value",
    "scheduler_tasks",
    "validate_config_payload",
    "validate_config_value",
    "validate_json_value",
    "validated_name",
    "validated_segment",
]
