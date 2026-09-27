"""Прямые адаптеры внешних инструментов AzurPilot."""

from .contracts import (
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
