"""Граница наблюдаемости журналов приложения AzurPilot."""

from module.observability.bootstrap import (
    configure_application_observability,
    shutdown_application_observability,
)

__all__ = (
    "configure_application_observability",
    "shutdown_application_observability",
)
