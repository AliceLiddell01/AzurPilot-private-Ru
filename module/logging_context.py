"""Контекст задачи для общего logging-потока AzurPilot."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

import inflection

_TASK_NAME_LIMIT = 128
_task_name: ContextVar[str | None] = ContextVar("alas_logging_task", default=None)


@dataclass(frozen=True)
class LoggingContext:
    """Дополнительные metadata текущего процесса или bounded operation."""

    profile: str | None = None
    component: str | None = None
    run_id: str | None = None


_UNSET = object()
_logging_context: ContextVar[LoggingContext | None] = ContextVar(
    "alas_logging_context",
    default=None,
)


def _normalize_task_name(task: object) -> str | None:
    if task is None:
        return None
    if isinstance(task, bytes):
        value = f"<байтовое значение, размер={len(task)}>"
    elif isinstance(task, (str, int, float, bool)):
        value = str(task).strip()
    else:
        value = f"<объект {type(task).__name__}>"
    if not value:
        return None
    return value[:_TASK_NAME_LIMIT]


def get_task_context() -> str | None:
    """Вернуть имя текущей задачи или ``None`` вне task boundary."""
    return _task_name.get()


def get_logging_context() -> LoggingContext:
    """Вернуть metadata текущей ContextVar без создания параллельного task context."""
    return _logging_context.get() or LoggingContext()


def _normalize_context_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        normalized = f"<байтовое значение, размер={len(value)}>"
    elif isinstance(value, (str, int, float, bool)):
        normalized = str(value).strip()
    else:
        normalized = f"<объект {type(value).__name__}>"
    if not normalized:
        return None
    return normalized[:_TASK_NAME_LIMIT]


@contextmanager
def logging_context(
    *,
    profile: object = _UNSET,
    component: object = _UNSET,
    run_id: object = _UNSET,
):
    """Временно добавить process/profile metadata с гарантированным восстановлением."""
    previous = get_logging_context()
    current = LoggingContext(
        profile=(
            previous.profile
            if profile is _UNSET
            else _normalize_context_value(profile)
        ),
        component=(
            previous.component
            if component is _UNSET
            else _normalize_context_value(component)
        ),
        run_id=(
            previous.run_id
            if run_id is _UNSET
            else _normalize_context_value(run_id)
        ),
    )
    token = _logging_context.set(current)
    try:
        yield current
    finally:
        _logging_context.reset(token)


@contextmanager
def task_context(task: object):
    """Временно установить task metadata и гарантированно восстановить прошлое значение."""
    value = _normalize_task_name(task)
    token = _task_name.set(value)
    try:
        yield value
    finally:
        _task_name.reset(token)


class TaskContextFilter(logging.Filter):
    """Добавить компактное имя задачи в ``LogRecord`` без изменения formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.alas_task = get_task_context()
        return True


def install_task_context_filter(target: logging.Logger) -> TaskContextFilter:
    """Идемпотентно подключить task metadata к общему logger."""
    for existing in target.filters:
        if isinstance(existing, TaskContextFilter):
            return existing
    context_filter = TaskContextFilter()
    target.addFilter(context_filter)
    return context_filter


def task_logging_context(func):
    """Оборачивать ``Alas.run(command, ...)`` task context без изменения return/exception semantics."""
    @wraps(func)
    def wrapped(self, command, *args, **kwargs):
        from module.logger import logger

        install_task_context_filter(logger)
        task_value = _normalize_task_name(command)
        task_name = inflection.camelize(task_value or "UnknownTask")
        profile = getattr(self, "config_name", _UNSET)
        with logging_context(profile=profile):
            with task_context(task_name):
                return func(self, command, *args, **kwargs)

    return wrapped
