"""Контекст задачи для общего logging-потока AzurPilot."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import inflection

_TASK_NAME_LIMIT = 128
_task_name: ContextVar[str | None] = ContextVar("alas_logging_task", default=None)


def _normalize_task_name(task: object) -> str | None:
    if task is None:
        return None
    value = str(task).strip()
    if not value:
        return None
    return value[:_TASK_NAME_LIMIT]


def get_task_context() -> str | None:
    """Вернуть имя текущей задачи или ``None`` вне task boundary."""
    return _task_name.get()


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
        task_name = inflection.camelize(str(command))
        with task_context(task_name):
            return func(self, command, *args, **kwargs)

    return wrapped
