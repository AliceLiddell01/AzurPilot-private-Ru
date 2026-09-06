"""Fail-open application metrics через официальный OpenTelemetry SDK."""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from module.observability._shared import (
    _OUTCOMES,
    _metric_label,
    _outcome_from_exception,
    _outcome_from_result,
    _profile_label,
)

_METRIC_SCOPE_NAME = "azurpilot.observability"
_TASK_RUN_NAME = "azurpilot.task.run"
_TASK_DURATION_NAME = "azurpilot.task.duration"
_TASK_RUN_UNIT = "{run}"
_TASK_DURATION_UNIT = "s"
_MIN_DURATION_SECONDS = 1e-9
_runtime_lock = threading.RLock()
_active_runtime: MetricsRuntime | None = None
_task_depth: ContextVar[int] = ContextVar("azurpilot_metrics_task_depth", default=0)
_task_outcome: ContextVar[str | None] = ContextVar(
    "azurpilot_metrics_task_outcome",
    default=None,
)


@dataclass(frozen=True)
class MetricsConfig:
    """Проверенный bounded contract metrics exporter-а."""

    endpoint: str | None
    timeout_millis: int
    export_interval_millis: int
    export_timeout_millis: int


@dataclass(frozen=True, slots=True)
class _CanonicalTaskIdentity:
    """Проверенная identity задачи из внешнего scheduler registry."""

    name: str


def _canonical_task_identity(
    task: object,
    registry: Iterable[object] | None,
) -> _CanonicalTaskIdentity | None:
    """Получить task label только из уже существующего scheduler registry."""
    try:
        command = getattr(task, "command", None)
    except Exception:
        return None
    if not isinstance(command, str) or isinstance(registry, (str, bytes)):
        return None
    try:
        for candidate in registry or ():
            if not isinstance(candidate, str):
                continue
            if candidate.casefold() != command.casefold():
                continue
            canonical = _metric_label(candidate)
            if canonical != "unknown":
                return _CanonicalTaskIdentity(canonical)
            return None
    except Exception:
        return None
    return None


def _metric_attributes(
    profile: object,
    task: _CanonicalTaskIdentity | None,
    outcome: object,
) -> Mapping[str, str]:
    try:
        normalized_outcome = (
            outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "unknown"
        )
    except Exception:
        normalized_outcome = "unknown"
    return {
        "azurpilot.profile": _profile_label(profile),
        "azurpilot.task": task.name if task is not None else "unknown",
        "azurpilot.task.outcome": normalized_outcome,
    }


def _duration_seconds(value: object) -> float:
    try:
        duration = float(value)
    except Exception:
        return _MIN_DURATION_SECONDS
    if not math.isfinite(duration) or duration <= 0:
        return _MIN_DURATION_SECONDS
    return duration


@dataclass
class MetricsRuntime:
    """Process-local provider и переиспользуемые task instruments."""

    provider: Any
    task_run_counter: Any
    task_duration_histogram: Any
    owner_pid: int
    reporter: Any
    active: bool = True

    def _record_task_run(
        self,
        *,
        profile: object,
        task: _CanonicalTaskIdentity | None,
        outcome: object,
        duration_seconds: object,
    ) -> None:
        if not self.active or self.owner_pid != os.getpid():
            return
        try:
            attributes = _metric_attributes(profile, task, outcome)
            duration = _duration_seconds(duration_seconds)
            self.task_run_counter.add(1, attributes=attributes)
            self.task_duration_histogram.record(duration, attributes=attributes)
        except Exception as exc:
            try:
                self.reporter.report(
                    "Ошибка записи application metrics; основная операция продолжит работу",
                    exc,
                )
            except Exception:
                pass

    def after_fork(self) -> None:
        """Отключить унаследованный provider до явного child bootstrap."""
        self.active = False

    def shutdown(self, timeout_millis: int) -> bool:
        self.active = False
        completed = True
        deadline = time.monotonic() + max(0, timeout_millis) / 1000

        def remaining_timeout_millis() -> int:
            return max(0, int((deadline - time.monotonic()) * 1000))

        try:
            completed = bool(
                self.provider.force_flush(
                    timeout_millis=remaining_timeout_millis()
                )
            ) and completed
        except Exception as exc:
            completed = False
            try:
                self.reporter.report("Не удалось сбросить буфер application metrics", exc)
            except Exception:
                pass
        try:
            self.provider.shutdown(timeout_millis=remaining_timeout_millis())
        except TypeError:
            try:
                self.provider.shutdown()
            except Exception as exc:
                completed = False
                try:
                    self.reporter.report(
                        "Не удалось завершить metrics provider",
                        exc,
                    )
                except Exception:
                    pass
        except Exception as exc:
            completed = False
            try:
                self.reporter.report("Не удалось завершить metrics provider", exc)
            except Exception:
                pass
        return completed


def build_metrics_runtime(
    config: MetricsConfig,
    *,
    resource: Any,
    reporter: Any,
    exporter_factory: Callable[[int], Any] | None = None,
    reader_factory: Callable[[Any, int, int], Any] | None = None,
) -> MetricsRuntime:
    """Создать один metrics provider и два переиспользуемых instruments."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    class _ProcessLocalPeriodicExportingMetricReader(PeriodicExportingMetricReader):
        """Не создавать унаследованный reader thread после fork."""

        def _at_fork_reinit(self) -> None:
            return None

    exporter = None
    reader = None
    provider = None
    try:
        exporter = (
            exporter_factory(config.timeout_millis)
            if exporter_factory is not None
            else OTLPMetricExporter(
                endpoint=config.endpoint,
                timeout=config.timeout_millis / 1000,
            )
        )
        reader = (
            reader_factory(
                exporter,
                config.export_interval_millis,
                config.export_timeout_millis,
            )
            if reader_factory is not None
            else _ProcessLocalPeriodicExportingMetricReader(
                exporter,
                export_interval_millis=config.export_interval_millis,
                export_timeout_millis=config.export_timeout_millis,
            )
        )
        # MeterProvider сам применяет стандартный OTEL_METRICS_EXEMPLAR_FILTER.
        provider = MeterProvider(
            metric_readers=(reader,),
            resource=resource,
            shutdown_on_exit=False,
        )
        meter = provider.get_meter(_METRIC_SCOPE_NAME)
        return MetricsRuntime(
            provider=provider,
            task_run_counter=meter.create_counter(
                _TASK_RUN_NAME,
                unit=_TASK_RUN_UNIT,
                description="Количество завершённых запусков canonical task",
            ),
            task_duration_histogram=meter.create_histogram(
                _TASK_DURATION_NAME,
                unit=_TASK_DURATION_UNIT,
                description="Длительность завершённых запусков canonical task",
            ),
            owner_pid=os.getpid(),
            reporter=reporter,
        )
    except Exception:
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass
        elif reader is not None:
            try:
                reader.shutdown()
            except Exception:
                pass
        elif exporter is not None:
            try:
                exporter.shutdown()
            except Exception:
                pass
        raise


def activate_metrics_runtime(runtime: MetricsRuntime) -> None:
    global _active_runtime
    with _runtime_lock:
        _active_runtime = runtime


def deactivate_metrics_runtime(runtime: MetricsRuntime | None) -> None:
    global _active_runtime
    with _runtime_lock:
        if runtime is None or _active_runtime is runtime:
            _active_runtime = None


def reset_metrics_runtime_after_fork() -> None:
    """Сбросить process-local registry в child без захвата унаследованного lock."""
    global _runtime_lock, _active_runtime
    _runtime_lock = threading.RLock()
    runtime = _active_runtime
    _active_runtime = None
    _task_depth.set(0)
    _task_outcome.set(None)
    if runtime is not None:
        runtime.after_fork()


def get_active_metrics_runtime() -> MetricsRuntime | None:
    with _runtime_lock:
        runtime = _active_runtime
    if runtime is None or not runtime.active or runtime.owner_pid != os.getpid():
        return None
    return runtime


class TaskRun:
    """Контекст одной внешней task boundary без дублирования nested calls."""

    def __init__(
        self,
        *,
        profile: object,
        task: _CanonicalTaskIdentity | None,
    ) -> None:
        self._profile = profile
        self._task = task if isinstance(task, _CanonicalTaskIdentity) else None
        self._runtime: MetricsRuntime | None = None
        self._started_at = 0.0
        self._depth_token: Any = None
        self._outcome_token: Any = None
        self._nested = False
        self._finished = False
        self._result_outcome: str | None = None

    def __enter__(self) -> Self:
        if _task_depth.get() > 0:
            self._nested = True
            return self
        self._runtime = get_active_metrics_runtime()
        if self._runtime is None:
            return self
        self._depth_token = _task_depth.set(1)
        self._outcome_token = _task_outcome.set(None)
        self._started_at = time.monotonic()
        return self

    @property
    def task_name(self) -> str:
        """Вернуть уже проверенное имя task для общей scheduler boundary."""
        return self._task.name if self._task is not None else "unknown"

    def finish(self, result: object) -> None:
        if self._nested or self._runtime is None:
            return
        self._result_outcome = _outcome_from_result(result)
        self._finished = True

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback_object: TracebackType | None,
    ) -> bool:
        del traceback_object
        if self._nested or self._runtime is None:
            return False
        try:
            if exception is not None:
                outcome = _outcome_from_exception(exception)
            else:
                outcome = _task_outcome.get() or (
                    self._result_outcome if self._finished else "unknown"
                )
            self._runtime._record_task_run(
                profile=self._profile,
                task=self._task,
                outcome=outcome,
                duration_seconds=max(
                    time.monotonic() - self._started_at,
                    _MIN_DURATION_SECONDS,
                ),
            )
        finally:
            if self._outcome_token is not None:
                _task_outcome.reset(self._outcome_token)
            if self._depth_token is not None:
                _task_depth.reset(self._depth_token)
        return False


def _task_run(*, profile: object, task: _CanonicalTaskIdentity | None) -> TaskRun:
    """Вернуть внутренний контекст уже проверенной task boundary."""
    return TaskRun(profile=profile, task=task)


def scheduler_task_run(
    *,
    profile: object,
    task: object,
    registry: Iterable[object] | None = None,
) -> TaskRun:
    """Создать metrics boundary для выбранной scheduler task."""
    return _task_run(
        profile=profile,
        task=_canonical_task_identity(task, registry),
    )


def mark_task_stopped() -> None:
    """Отметить normal ``TaskEnd`` как stopped, не меняя его control flow."""
    if _task_depth.get() > 0:
        _task_outcome.set("stopped")


__all__ = (
    "MetricsConfig",
    "MetricsRuntime",
    "activate_metrics_runtime",
    "build_metrics_runtime",
    "deactivate_metrics_runtime",
    "get_active_metrics_runtime",
    "mark_task_stopped",
    "reset_metrics_runtime_after_fork",
    "scheduler_task_run",
)
