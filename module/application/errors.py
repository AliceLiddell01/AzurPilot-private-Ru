"""Безопасная граница ошибок прикладного слоя."""


class ApplicationError(Exception):
    """Ожидаемая ошибка, безопасная для преобразования транспортным адаптером."""

    code = "application_error"


class InvalidRequestError(ApplicationError):
    """Входные данные не удовлетворяют контракту операции."""

    code = "invalid_request"


class ResourceNotFoundError(ApplicationError):
    """Запрошенный прикладной ресурс не существует."""

    code = "not_found"


class ServiceUnavailableError(ApplicationError):
    """Источник данных временно не может выполнить операцию."""

    code = "service_unavailable"


class ConfigurationValidationError(ApplicationError):
    """Значение не прошло проверку generated config metadata."""

    code = "configuration_invalid"


class OperationFailedError(ApplicationError):
    """Инфраструктурный control operation не подтвердил успех."""

    code = "operation_failed"


class GameRuntimePhaseError(OperationFailedError):
    """Сохранить фазу composite runtime mutation без нового public error code."""

    _PHASES = frozenset({"emulator_restart", "game_start"})

    def __init__(self, phase: str, cause: ApplicationError) -> None:
        if phase not in self._PHASES:
            raise ValueError("Неизвестная фаза Game runtime operation.")
        if not isinstance(cause, ApplicationError):
            raise TypeError("cause должен быть ApplicationError")
        self.phase = phase
        self.cause = cause
        super().__init__(str(cause))


class ResourceBusyError(OperationFailedError):
    """Ресурс уже занят другой несовместимой control-операцией."""

    code = "resource_busy"


class PreconditionFailedError(OperationFailedError):
    """Безопасное выполнение операции невозможно из-за precondition."""

    code = "precondition_failed"


class OwnershipAmbiguousError(PreconditionFailedError):
    """Невозможно доказать ownership целевого host/device ресурса."""

    code = "ownership_ambiguous"


class PostconditionFailedError(OperationFailedError):
    """После mutation не подтверждено ожидаемое состояние."""

    code = "postcondition_failed"


class InstanceNotRunningError(ApplicationError):
    """Операция требует подтверждённый запущенный экземпляр."""

    code = "instance_not_running"


class StorageError(ApplicationError):
    """Базовая ошибка application-owned storage boundary."""

    code = "storage_error"


class StorageUnavailableError(StorageError):
    """Хранилище недоступно по транспортной причине."""

    code = "storage_unavailable"


class StorageAuthenticationError(StorageError):
    """Хранилище отклонило аутентификацию без раскрытия credentials."""

    code = "storage_authentication_failed"


class StorageConfigurationError(StorageError):
    """Структурная конфигурация подключения некорректна."""

    code = "storage_configuration_invalid"


class IncompatibleSchemaError(StorageError):
    """Versioned schema отсутствует или не совпадает с ожидаемым head."""

    code = "storage_schema_incompatible"


class StorageConflictError(StorageError):
    """Idempotency key или optimistic version конфликтует."""

    code = "storage_conflict"


class StorageInvalidDataError(StorageError):
    """Domain command не удовлетворяет storage-инвариантам."""

    code = "storage_invalid_data"
