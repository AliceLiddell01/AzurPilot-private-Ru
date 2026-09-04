"""Лёгкие перехватчики выполнения без импорта диагностики в обычном рабочем процессе."""

from __future__ import annotations

import os
from pathlib import Path

_SESSION_ENV = "AZURPILOT_DEV_SESSION_ID"
_OPERATION_ENV = "AZURPILOT_RUNTIME_OPERATION_ID"
_REPOSITORY_ENV = "AZURPILOT_REPOSITORY_ROOT"


def _enabled() -> bool:
    return bool(os.environ.get(_SESSION_ENV))


def _repository_root() -> Path:
    return Path(os.environ.get(_REPOSITORY_ENV) or Path.cwd())


def record_task_started(config_name: object, task: object) -> bool:
    state_ok = _record_runtime_state(config_name, task=task, started=True)
    if not state_ok:
        return False
    if not _enabled():
        return True
    try:
        from module.dev_runtime.evidence import record_task_started as record

        record(config_name, task)
    except Exception:
        return True
    return True


def record_task_finished(config_name: object, task: object) -> bool:
    state_ok = _record_runtime_state(config_name, task=task, started=False)
    if not state_ok:
        return False
    if not _enabled():
        return True
    try:
        from module.dev_runtime.evidence import record_task_finished as record

        record(config_name, task, "returned")
    except Exception:
        return True
    return True


def record_runtime_error(
    config_name: object,
    exception: BaseException,
    *,
    phase: str,
    task: object = None,
) -> None:
    try:
        from module.application.runtime_state import RuntimeStateStore

        profile = str(config_name)
        RuntimeStateStore(_repository_root()).mark_failed(
            profile,
            operation_id=os.environ.get(_OPERATION_ENV),
            session_id=os.environ.get(_SESSION_ENV),
            terminal_state="runtime_error",
        )
    except Exception:
        pass
    if not _enabled():
        return
    try:
        from module.dev_runtime.evidence import record_runtime_error as record

        record(config_name, exception, phase=phase, task=task)
    except Exception:
        return


def record_dependency_registered(
    config_name: object,
    *,
    caller: object,
    target: object,
    timestamp: object,
) -> None:
    if not _enabled():
        return
    try:
        from module.dev_runtime.evidence import record_dependency_registered as record

        record(
            config_name,
            caller=caller,
            target=target,
            timestamp=timestamp,
        )
    except Exception:
        return


def serve_pending_screenshot(image: object) -> None:
    if not _enabled():
        return
    try:
        from module.dev_runtime.evidence import serve_pending_screenshot as serve

        serve(image)
    except Exception:
        return


def handover_requested(config_name: object) -> bool | None:
    """Проверить transient handover перед выбором следующей задачи."""

    try:
        from module.application.runtime_state import RuntimeStateStore

        profile = str(config_name)
        snapshot = RuntimeStateStore(_repository_root()).read(profile)
        return None if snapshot is None else snapshot.handover_requested
    except Exception:
        return None


def _record_runtime_state(config_name: object, *, task: object, started: bool) -> bool:
    try:
        from module.application.runtime_state import RuntimeStateStore

        profile = str(config_name)
        if not isinstance(task, str) or not task.strip():
            return False
        store = RuntimeStateStore(_repository_root())
        # Старый/тестовый worker может работать без process-shared snapshot.
        # Это не доказывает handover и потому сохраняет прежнюю семантику
        # scheduler; если snapshot существует, граница обязана быть атомарной.
        if store.read(profile) is None:
            return True
        operation_id = os.environ.get(_OPERATION_ENV)
        session_id = os.environ.get(_SESSION_ENV)
        if started:
            return store.try_mark_task_started(
                profile,
                task,
                operation_id=operation_id,
                session_id=session_id,
            )
        store.mark_task_finished(
            profile,
            operation_id=operation_id,
            session_id=session_id,
        )
        return True
    except Exception:
        # Runtime state — это диагностические и координационные метаданные;
        # они не должны превращать штатный результат задачи в исключение.
        return False


__all__ = [
    "record_dependency_registered",
    "handover_requested",
    "record_runtime_error",
    "record_task_finished",
    "record_task_started",
    "serve_pending_screenshot",
]
