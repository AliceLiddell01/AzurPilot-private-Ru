"""Лёгкие перехватчики выполнения без импорта диагностики в обычном рабочем процессе."""

from __future__ import annotations

import os

_SESSION_ENV = "AZURPILOT_DEV_SESSION_ID"


def _enabled() -> bool:
    return bool(os.environ.get(_SESSION_ENV))


def record_task_started(config_name: object, task: object) -> None:
    if not _enabled():
        return
    try:
        from module.dev_runtime.evidence import record_task_started as record

        record(config_name, task)
    except Exception:
        return


def record_task_finished(config_name: object, task: object) -> None:
    if not _enabled():
        return
    try:
        from module.dev_runtime.evidence import record_task_finished as record

        record(config_name, task, "returned")
    except Exception:
        return


def record_runtime_error(
    config_name: object,
    exception: BaseException,
    *,
    phase: str,
    task: object = None,
) -> None:
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


__all__ = [
    "record_dependency_registered",
    "record_runtime_error",
    "record_task_finished",
    "record_task_started",
    "serve_pending_screenshot",
]
