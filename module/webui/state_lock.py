"""Общая межпоточная и межпроцессная блокировка файлов состояния WebUI."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from typing import Any

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


@contextmanager
def state_write_lock(lock_path: Path | str):
    """Сериализовать изменение файла между потоками/процессами с повторным входом потока."""

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    process_lock = _process_lock(path)

    with process_lock:
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
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
