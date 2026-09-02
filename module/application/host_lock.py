"""Ограниченная межпоточная и межпроцессная блокировка application resources."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

HOST_LOCK_TIMEOUT_SECONDS = 10.0
HOST_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: dict[str, Lock] = {}


def _process_lock(path: Path) -> Lock:
    key = str(path.resolve(strict=False))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def _sleep_until_retry(deadline: float, retry_interval: float) -> None:
    remaining = _remaining(deadline)
    if remaining > 0:
        time.sleep(min(retry_interval, remaining))


def _is_lock_conflict(error: OSError) -> bool:
    return error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLOCK,
    } or getattr(error, "winerror", None) in {32, 33}


def _acquire_os_lock(
    handle: object,
    path: Path,
    deadline: float,
    retry_interval: float,
) -> None:
    fileno = getattr(handle, "fileno")
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                getattr(handle, "seek")(0)
                msvcrt.locking(fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if not _is_lock_conflict(error):
                    raise
                if _remaining(deadline) <= 0:
                    raise TimeoutError(
                        f"Истёк тайм-аут application host lock: {path}"
                    ) from error
                _sleep_until_retry(deadline, retry_interval)

    try:
        import fcntl
    except ImportError as error:
        raise OSError("Платформа не поддерживает application host lock") from error

    while True:
        try:
            fcntl.flock(fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if not _is_lock_conflict(error):
                raise
            if _remaining(deadline) <= 0:
                raise TimeoutError(
                    f"Истёк тайм-аут application host lock: {path}"
                ) from error
            _sleep_until_retry(deadline, retry_interval)


def _release_os_lock(handle: object) -> None:
    fileno = getattr(handle, "fileno")
    getattr(handle, "seek")(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fileno(), fcntl.LOCK_UN)


@contextmanager
def application_host_lock(
    lock_path: Path | str,
    *,
    timeout: float = HOST_LOCK_TIMEOUT_SECONDS,
    retry_interval: float = HOST_LOCK_RETRY_INTERVAL_SECONDS,
) -> Iterator[None]:
    """Захватить безопасный lock для одной host-global application операции."""

    timeout = float(timeout)
    retry_interval = float(retry_interval)
    if timeout < 0:
        raise ValueError("Тайм-аут application host lock не может быть отрицательным")
    if retry_interval <= 0:
        raise ValueError("Интервал application host lock должен быть положительным")

    path = Path(lock_path)
    if path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        raise OSError("Application host lock не должен быть ссылкой")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        raise OSError("Application host lock не должен быть ссылкой")

    process_lock = _process_lock(path)
    deadline = time.monotonic() + timeout
    if not process_lock.acquire(timeout=_remaining(deadline)):
        raise TimeoutError(f"Истёк тайм-аут application host lock: {path}")

    handle = None
    acquired = False
    try:
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        _acquire_os_lock(handle, path, deadline, retry_interval)
        acquired = True
        yield
    finally:
        if acquired and handle is not None:
            _release_os_lock(handle)
        if handle is not None:
            handle.close()
        process_lock.release()


__all__ = (
    "HOST_LOCK_RETRY_INTERVAL_SECONDS",
    "HOST_LOCK_TIMEOUT_SECONDS",
    "application_host_lock",
)
