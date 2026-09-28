"""Закрытые модели состояния внешних интеграций.

Эти DTO намеренно не содержат значения токенов, ответов MCP или произвольных
команд. Адаптеры публикуют только bounded evidence, достаточное для оператора
и машинной проверки.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from azurpilot.tooling.contracts import (
    AnalysisScope,
    CapabilityStatus,
    ClosedModel,
)



class IntegrationName(StrEnum):
    """Ровно пять поддерживаемых продуктовых семейств."""

    SEMGREP = "semgrep"
    GRAFANA = "grafana"
    CONTEXT7 = "context7"
    DOCKER_DOCS = "docker-docs"
    DOCKER_HUB = "docker-hub"


class IntegrationState(StrEnum):
    """Общие состояния всех внешних адаптеров."""

    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    RATE_LIMITED = "RATE_LIMITED"
    INCOMPATIBLE = "INCOMPATIBLE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class CredentialSource(StrEnum):
    """Разрешённые источники credential без хранения его значения."""

    NONE = "none"
    ENVIRONMENT = "environment"
    FILE = "file"
    USER_CONFIG = "user_config"
    MACHINE_CONFIG = "machine_config"


class CredentialRef(ClosedModel):
    """Безопасная ссылка на credential, не содержащая secret value."""

    configured: bool = False
    source: CredentialSource = CredentialSource.NONE
    name: str | None = Field(default=None, min_length=1, max_length=128)
    auth_verified: bool | None = None


class IntegrationEvidence(ClosedModel):
    """Bounded evidence одного прямого адаптера."""

    route: str = Field(min_length=1, max_length=80)
    transport: str | None = Field(default=None, max_length=40)
    endpoint: str | None = Field(default=None, max_length=512)
    executable: str | None = Field(default=None, max_length=256)
    image: str | None = Field(default=None, max_length=512)
    configured: bool = False
    reachable: bool = False
    authenticated: bool | None = None
    read_only: bool = True
    tool_count: int | None = Field(default=None, ge=0, le=256)
    selected_tool: str | None = Field(default=None, max_length=128)
    blocked_write_tools: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    credential: CredentialRef = Field(default_factory=CredentialRef)
    scope: AnalysisScope | None = None
    diagnostics: tuple[str, ...] = Field(default_factory=tuple, max_length=16)


class IntegrationRecord(ClosedModel):
    """Стабильный результат status/doctor/probe для одного семейства."""

    name: IntegrationName
    state: IntegrationState
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    message: str = Field(min_length=1, max_length=300)
    evidence: IntegrationEvidence


class IntegrationFinding(ClosedModel):
    """Санитизированный finding Semgrep или внешнего review."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    identifier: str = Field(min_length=1, max_length=240)
    path: str = Field(min_length=1, max_length=512)
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    line_end: int | None = Field(default=None, ge=1, le=10_000_000)
    title: str | None = Field(default=None, max_length=160)
    severity: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=1200)
    fingerprint: str | None = Field(default=None, max_length=128)
    reviewed_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    fix_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    disposition: str | None = Field(default=None, max_length=40)
    resolution: str | None = Field(default=None, max_length=4000)
    codegen_instructions: str | None = Field(default=None, max_length=4000)
    suggestions: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    decision_reason: str | None = Field(default=None, max_length=2000)
    change_summary: str | None = Field(default=None, max_length=2000)
    conflict_kind: str | None = Field(default=None, max_length=80)
    deferral_reason: str | None = Field(default=None, max_length=80)
    authoritative_source: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def validate_line_range(self) -> IntegrationFinding:
        if self.line is not None and self.line_end is not None and self.line_end < self.line:
            raise ValueError("line_end не может быть меньше line")
        return self



class IntegrationDetails(ClosedModel):
    """Операционный payload CLI."""

    action: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    integrations: tuple[IntegrationRecord, ...] = Field(min_length=1, max_length=5)
    target: IntegrationName | None = None
    scope: AnalysisScope | None = None
    findings: tuple[IntegrationFinding, ...] = Field(default_factory=tuple, max_length=128)


class SharedMcpDetails(ClosedModel):
    """Состояние общих долговременных MCP HTTP services одной машины."""

    action: Literal["start", "stop", "status"]
    state: CapabilityStatus
    services: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    diagnostics: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    # Health/auth status сам по себе не доказывает provenance image. Поле
    # становится истинным только для `start` после успешного Compose build.
    build_confirmed: bool = False


class IntegrationEvidenceBundle(ClosedModel):
    """Общая bounded provenance сводки без пути и secret payload."""

    generated_at: str = Field(min_length=1, max_length=40)
    collector: str = Field(default="integration_registry", min_length=1, max_length=80)
    configuration: str = Field(default="merged_explicit_sources", min_length=1, max_length=80)


__all__ = [
    "CredentialRef",
    "CredentialSource",
    "IntegrationDetails",
    "IntegrationEvidence",
    "IntegrationEvidenceBundle",
    "IntegrationFinding",
    "IntegrationName",
    "IntegrationRecord",
    "IntegrationState",
    "SharedMcpDetails",
]
