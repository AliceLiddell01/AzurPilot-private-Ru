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
    """Источник данных временно не может выполнить read-only операцию."""

    code = "service_unavailable"
