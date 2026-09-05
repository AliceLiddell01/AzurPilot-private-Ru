"""Граница наблюдаемости журналов приложения AzurPilot."""

from module.observability.bootstrap import (
    configure_application_observability,
    shutdown_application_observability,
)
from module.observability.metrics import mark_task_stopped, scheduler_task_run

__all__ = (
    "configure_application_observability",
    "mark_task_stopped",
    "scheduler_task_run",
    "shutdown_application_observability",
)
