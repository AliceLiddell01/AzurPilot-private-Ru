"""Общая межпроцессная координация владельцев Dev Runtime.

Control operation, SmokeRun и DevSession хранят разные маркеры, но создание
каждого владельца проходит через одну repository-scoped блокировку. Это делает
проверку конфликтов и фиксацию durable reservation одной атомарной секцией.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

from module.dev_runtime.contracts import DevEnvironment

COORDINATION_LOCK_TIMEOUT = 10.0
COORDINATION_LOCK_RETRY_SECONDS = 0.05


class RuntimeCoordinationError(RuntimeError):
    """Общая блокировка владельцев runtime недоступна или небезопасна."""

    code = "DEV_RUNTIME_COORDINATION_UNAVAILABLE"


def _is_reparse_point(path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError as exc:
        raise RuntimeCoordinationError(
            "Нельзя проверить путь общей runtime-блокировки"
        ) from exc


def _check_path(environment: DevEnvironment, path) -> None:
    root = environment.repository_root
    state = root / "config" / "state"
    for candidate in (root, root / "config", state, path):
        if os.path.lexists(candidate) and _is_reparse_point(candidate):
            raise RuntimeCoordinationError(
                "Общая runtime-блокировка не должна проходить через ссылку или junction"
            )


@contextmanager
def runtime_coordination_lock(environment: DevEnvironment) -> Iterator[None]:
    """Захватить общую lock до фиксации одного из runtime owner markers."""

    path = environment.coordination_lock_file
    _check_path(environment, path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _check_path(environment, path)
        handle = path.open("a+b")
    except OSError as exc:
        raise RuntimeCoordinationError(
            "Нельзя открыть общую runtime-блокировку"
        ) from exc

    acquired = False
    try:
        try:
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RuntimeCoordinationError(
                "Нельзя инициализировать общую runtime-блокировку"
            ) from exc

        deadline = time.monotonic() + COORDINATION_LOCK_TIMEOUT
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeCoordinationError(
                            "Истекло время ожидания общей runtime-блокировки"
                        ) from exc
                    time.sleep(COORDINATION_LOCK_RETRY_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeCoordinationError(
                            "Истекло время ожидания общей runtime-блокировки"
                        ) from exc
                    time.sleep(COORDINATION_LOCK_RETRY_SECONDS)
                except OSError as exc:
                    raise RuntimeCoordinationError(
                        "Общая runtime-блокировка недоступна на этой файловой системе"
                    ) from exc
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


__all__ = [
    "COORDINATION_LOCK_RETRY_SECONDS",
    "COORDINATION_LOCK_TIMEOUT",
    "RuntimeCoordinationError",
    "runtime_coordination_lock",
]
