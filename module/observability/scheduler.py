"""Общая scheduler boundary для application metrics и tracing."""

from __future__ import annotations

from types import TracebackType
from typing import Self

from module.observability.metrics import scheduler_task_run as metrics_task_run
from module.observability.tracing import scheduler_task_span


class SchedulerTaskRun:
    """Оркестрировать один task lifecycle и закрыть metrics до root span."""

    def __init__(
        self,
        *,
        profile: object,
        task: object,
        registry: object = None,
    ) -> None:
        self._metrics = metrics_task_run(
            profile=profile,
            task=task,
            registry=registry,
        )
        self._tracing = scheduler_task_span(
            profile=profile,
            task=self._metrics.task_name,
        )

    def __enter__(self) -> Self:
        self._metrics.__enter__()
        try:
            self._tracing.__enter__()
        except BaseException as exc:
            self._metrics.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def finish(self, result: object) -> None:
        self._metrics.finish(result)
        self._tracing.finish(result)

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback_object: TracebackType | None,
    ) -> bool:
        metrics_result = False
        try:
            # Метрики записываются, пока корневой span ещё остаётся текущим.
            metrics_result = self._metrics.__exit__(
                exception_type,
                exception,
                traceback_object,
            )
        finally:
            self._tracing.__exit__(
                exception_type,
                exception,
                traceback_object,
            )
        return metrics_result


def scheduler_task_run(
    *,
    profile: object,
    task: object,
    registry: object = None,
) -> SchedulerTaskRun:
    """Создать общую canonical boundary выбранной scheduler task."""
    return SchedulerTaskRun(profile=profile, task=task, registry=registry)


__all__ = ("SchedulerTaskRun", "scheduler_task_run")
