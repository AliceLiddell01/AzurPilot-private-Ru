"""Граница наблюдаемости журналов приложения AzurPilot."""

from module.observability.bootstrap import (
    configure_application_observability,
    shutdown_application_observability,
)
from module.observability.metrics import mark_task_stopped as _mark_metrics_task_stopped
from module.observability.scheduler import (
    get_current_task_name,
    scheduler_task_run,
)
from module.observability.tracing import mark_task_stopped as _mark_tracing_task_stopped
from module.observability.tracing import get_current_trace_context


def mark_task_stopped() -> None:
    """Сохранить normal stopped outcome для всех активных application signals."""
    _mark_metrics_task_stopped()
    _mark_tracing_task_stopped()


__all__ = (
    "configure_application_observability",
    "get_current_task_name",
    "get_current_trace_context",
    "mark_task_stopped",
    "scheduler_task_run",
    "shutdown_application_observability",
)
