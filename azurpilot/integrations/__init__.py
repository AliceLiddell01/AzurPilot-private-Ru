"""Прямые адаптеры внешних инструментов AzurPilot."""

from .contracts import (
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
