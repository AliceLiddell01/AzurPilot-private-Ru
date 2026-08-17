"""Общая межпоточная и межпроцессная блокировка файлов состояния WebUI."""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from typing import Any

STATE_LOCK_TIMEOUT_SECONDS = 10.0
STATE_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: dict[str, Any] = {}
_THREAD_STATE = local()


def _process_lock(path: Path):
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _remaining_time(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def _wait_before_retry(deadline: float, retry_interval: float) -> None:
    remaining = _remaining_time(deadline)
    if remaining <= 0:
        return
    time.sleep(min(retry_interval, remaining))


def _acquire_os_lock(handle, path: Path, deadline: float, retry_interval: float) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EDEADLOCK}:
                    raise
                if _remaining_time(deadline) <= 0:
                    raise TimeoutError(
                        f"Истёк тайм-аут блокировки файла состояния: {path}"
                    ) from exc
                _wait_before_retry(deadline, retry_interval)

    import fcntl

    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            if _remaining_time(deadline) <= 0:
                raise TimeoutError(
                    f"Истёк тайм-аут блокировки файла состояния: {path}"
                ) from exc
            _wait_before_retry(deadline, retry_interval)


def _release_os_lock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def state_write_lock(
    lock_path: Path | str,
    *,
    timeout: float = STATE_LOCK_TIMEOUT_SECONDS,
    retry_interval: float = STATE_LOCK_RETRY_INTERVAL_SECONDS,
):
    """Сериализовать изменение файла с повторным входом и общим ограниченным ожиданием."""

    timeout = float(timeout)
    retry_interval = float(retry_interval)
    if timeout < 0:
        raise ValueError("Тайм-аут блокировки состояния не может быть отрицательным")
    if retry_interval <= 0:
        raise ValueError("Интервал повторной блокировки состояния должен быть положительным")

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    process_lock = _process_lock(path)
    deadline = time.monotonic() + timeout

    if not process_lock.acquire(timeout=_remaining_time(deadline)):
        raise TimeoutError(f"Истёк тайм-аут блокировки файла состояния: {path}")

    try:
        depths = getattr(_THREAD_STATE, "depths", None)
        if depths is None:
            depths = {}
            _THREAD_STATE.depths = depths

        depth = int(depths.get(key, 0) or 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                remaining = int(depths.get(key, 1) or 1) - 1
                if remaining:
                    depths[key] = remaining
                else:
                    depths.pop(key, None)
            return

        with path.open("a+b") as handle:
            _acquire_os_lock(handle, path, deadline, retry_interval)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                _release_os_lock(handle)
    finally:
        process_lock.release()
