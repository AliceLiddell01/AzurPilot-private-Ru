"""Исключения сервисного слоя с типизированным результатом."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .contracts import OperationState, ResultCode, exit_code_for


class ToolingError(Exception):
    """Ожидаемая ошибка операции, которую CLI обязан отобразить типизированно."""

    def __init__(
        self,
        code: ResultCode,
        message: str,
        *,
        state: OperationState = OperationState.FAILED,
        details: BaseModel | None = None,
        evidence: BaseModel | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:300]
        self.state = state
        self.details = details
        self.evidence = evidence
        self.operation_id = operation_id

    @property
    def exit_code(self) -> int:
        return int(exit_code_for(self.code))


class ProcessExecutionError(ToolingError):
    """Невозможность безопасно создать или идентифицировать процесс."""


class RepositoryResolutionError(ToolingError):
    """Корень проекта не доказан или неоднозначен."""


def error_details(error: ToolingError) -> dict[str, Any]:
    """Подготовить диагностическое представление без traceback и секретов."""

    details: dict[str, Any] = {}
    for name in ("details", "evidence"):
        model = getattr(error, name)
        if isinstance(model, BaseModel):
            details[name] = model.model_dump(mode="json", exclude_none=True)
    return details


__all__ = [
    "ProcessExecutionError",
    "RepositoryResolutionError",
    "ToolingError",
    "error_details",
]
