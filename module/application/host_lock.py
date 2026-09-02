"""Ограниченная межпоточная и межпроцессная блокировка application resources."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from typing import BinaryIO
from weakref import WeakValueDictionary

HOST_LOCK_TIMEOUT_SECONDS = 10.0
HOST_LOCK_RETRY_INTERVAL_SECONDS = 0.05
_PROCESS_LOCKS_GUARD = Lock()
_PROCESS_LOCKS: WeakValueDictionary[str, RLock] = WeakValueDictionary()
_THREAD_LOCK_DEPTHS = local()


def host_runtime_root() -> Path:
    """Вернуть стабильный пользовательский root для host-level locks."""

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
        if base:
            return Path(base) / "AzurPilot"
    else:
        base = os.environ.get("XDG_RUNTIME_DIR")
        if base:
            return Path(base) / "azurpilot"
        uid = getattr(os, "getuid", lambda: 0)()
        return Path(tempfile.gettempdir()) / f"azurpilot-{uid}"
    return Path(tempfile.gettempdir()) / "AzurPilot"


def host_scoped_lock_path(resource: str, identity: str) -> Path:
    """Построить host-scoped lock path без включения identity в имя файла."""

    if (
        not isinstance(resource, str)
        or not resource
        or resource in {".", ".."}
        or any(char in resource for char in "/\\")
    ):
        raise ValueError("Имя host lock resource имеет недопустимый формат")
    if not isinstance(identity, str) or not identity:
        raise ValueError("Идентификатор host lock resource должен быть непустым")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return host_runtime_root() / "locks" / resource / f"{digest}.lock"


def _process_lock(path: Path) -> RLock:
    key = str(path.resolve(strict=False))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = RLock()
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
    handle: BinaryIO,
    path: Path,
    deadline: float,
    retry_interval: float,
) -> None:
    fileno = handle.fileno
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                handle.seek(0)
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


def _release_os_lock(handle: BinaryIO) -> None:
    fileno = handle.fileno
    handle.seek(0)
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
    if path.is_symlink() or path.is_junction():
        raise OSError("Application host lock не должен быть ссылкой")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_junction():
        raise OSError("Application host lock не должен быть ссылкой")

    process_lock = _process_lock(path)
    deadline = time.monotonic() + timeout
    if not process_lock.acquire(timeout=_remaining(deadline)):
        raise TimeoutError(f"Истёк тайм-аут application host lock: {path}")

    key = str(path.resolve(strict=False))
    depths = getattr(_THREAD_LOCK_DEPTHS, "values", None)
    if depths is None:
        depths = {}
        _THREAD_LOCK_DEPTHS.values = depths
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
            process_lock.release()
        return

    handle: BinaryIO | None = None
    acquired = False
    try:
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        _acquire_os_lock(handle, path, deadline, retry_interval)
        acquired = True
        depths[key] = 1
        yield
    finally:
        try:
            try:
                if acquired and handle is not None:
                    _release_os_lock(handle)
            finally:
                if handle is not None:
                    handle.close()
        finally:
            depths.pop(key, None)
            process_lock.release()


__all__ = (
    "HOST_LOCK_RETRY_INTERVAL_SECONDS",
    "HOST_LOCK_TIMEOUT_SECONDS",
    "application_host_lock",
    "host_runtime_root",
    "host_scoped_lock_path",
)
