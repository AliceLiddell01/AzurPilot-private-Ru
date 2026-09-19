"""Минимальные DTO, которые реально публикует MCP process runtime."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _McpRuntimeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ProcessEvidence(_McpRuntimeModel):
    pid: int | None = Field(default=None, ge=1)
    started_at: float | None = None
    executable_name: str = Field(min_length=1, max_length=120)
    argv: tuple[str, ...] = Field(max_length=32)
    working_directory_name: str = Field(min_length=1, max_length=120)
    identity_confirmed: bool


__all__ = ["ProcessEvidence"]



