"""Прямые адаптеры внешних инструментов AzurPilot."""

from .contracts import (
    TASK_ID_PATTERN,
    CodeRabbitCycleSummary,
    CredentialRef,
    CredentialSource,
    IntegrationDetails,
    IntegrationEvidence,
    IntegrationFinding,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from .service import IntegrationRegistry, IntegrationService

__all__ = [
    "TASK_ID_PATTERN",
    "CodeRabbitCycleSummary",
    "CredentialRef",
    "CredentialSource",
    "IntegrationDetails",
    "IntegrationEvidence",
    "IntegrationFinding",
    "IntegrationName",
    "IntegrationRecord",
    "IntegrationRegistry",
    "IntegrationService",
    "IntegrationState",
]
