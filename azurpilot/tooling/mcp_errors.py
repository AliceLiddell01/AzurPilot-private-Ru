"""Ошибки, необходимые standalone MCP process runtime."""

from __future__ import annotations

from pydantic import BaseModel

from .result import OperationState, ResultCode, exit_code_for


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
        return int(exit_code_for(self.code).value)


class ProcessExecutionError(ToolingError):
    """Невозможность безопасно создать или идентифицировать процесс."""


__all__ = ["ProcessExecutionError", "ToolingError"]
