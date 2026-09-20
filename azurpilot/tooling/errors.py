"""Исключения общего сервисного слоя.

ToolingError и ProcessExecutionError принадлежат MCP-safe runtime core и
переэкспортируются здесь для сохранения существующего public import path.
"""

from __future__ import annotations

from .mcp_errors import ProcessExecutionError, ToolingError


class RepositoryResolutionError(ToolingError):
    """Корень проекта не доказан или неоднозначен."""


__all__ = [
    "ProcessExecutionError",
    "RepositoryResolutionError",
    "ToolingError",
]
