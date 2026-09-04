"""Единый bounded lease для игрового runtime-ресурса."""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from deploy.atomic import atomic_remove, atomic_write
from module.application.host_lock import (
    application_host_lock,
    host_scoped_lock_path,
)

GAME_RUNTIME_LEASE_TIMEOUT_SECONDS = 30.0
GAME_RUNTIME_LEASE_RETRY_INTERVAL_SECONDS = 0.05
_LEASE_RESOURCE = "game-runtime"
_LEASE_STATE = threading.local()


class ResourceLeaseError(RuntimeError):
    """Игровой ресурс невозможно безопасно передать текущей операции."""

    code = "RUNTIME_RESOURCE_LEASE_UNAVAILABLE"


def _adb_server_identity() -> str:
    socket = os.environ.get("ADB_SERVER_SOCKET", "").strip()
    if socket:
        return socket if socket.casefold().startswith("tcp:") else f"socket:{socket}"
    address = os.environ.get("ANDROID_ADB_SERVER_ADDRESS", "127.0.0.1").strip()
    port = os.environ.get("ANDROID_ADB_SERVER_PORT", "5037").strip()
    return f"tcp:{address or '127.0.0.1'}:{port or '5037'}"


def game_runtime_lease_path() -> Path:
    """Вернуть host-scoped lock path общего игрового runtime."""

    return host_scoped_lock_path(_LEASE_RESOURCE, _adb_server_identity())


def _marker_path(lock_path: Path) -> Path:
    return lock_path.with_name(f"{lock_path.stem}.owner.json")


def _local_depths() -> dict[str, int]:
    depths = getattr(_LEASE_STATE, "depths", None)
    if depths is None:
        depths = {}
        _LEASE_STATE.depths = depths
    return depths


def _process_created_at(pid: int) -> float | None:
    try:
        import psutil

        value = float(psutil.Process(pid).create_time())
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _read_marker(path: Path) -> tuple[int, float] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ResourceLeaseError("Не удалось прочитать identity владельца игрового lease") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ResourceLeaseError("Identity владельца игрового lease повреждена") from exc
    if not isinstance(payload, dict) or set(payload) != {"pid", "created_at", "acquired_at"}:
        raise ResourceLeaseError("Identity владельца игрового lease имеет неверную структуру")
    pid = payload.get("pid")
    created_at = payload.get("created_at")
    acquired_at = payload.get("acquired_at")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not math.isfinite(float(created_at))
        or float(created_at) <= 0
        or not isinstance(acquired_at, str)
        or not acquired_at
    ):
        raise ResourceLeaseError("Identity владельца игрового lease имеет неверные поля")
    return pid, float(created_at)


def _marker_is_active(marker: tuple[int, float]) -> bool | None:
    pid, created_at = marker
    current = _process_created_at(pid)
    if current is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None
    return abs(current - created_at) < 0.01


@contextmanager
def game_runtime_lease(
    repository_root: Path | str | None = None,
    *,
    timeout: float = GAME_RUNTIME_LEASE_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Удерживать единый cross-process lease до конца runtime mutation.

    ``repository_root`` принимается для симметрии с profile lock. Identity lease
    намеренно не включает checkout: один ADB endpoint должен сериализоваться
    между разными рабочими копиями.
    """

    del repository_root
    if type(timeout) not in (int, float) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120:
        raise ValueError("Тайм-аут игрового runtime lease должен быть в диапазоне (0, 120]")
    lock_path = game_runtime_lease_path()
    marker_path = _marker_path(lock_path)
    key = str(lock_path.resolve(strict=False))
    depths = _local_depths()
    with application_host_lock(
        lock_path,
        timeout=float(timeout),
        retry_interval=GAME_RUNTIME_LEASE_RETRY_INTERVAL_SECONDS,
    ):
        depth = int(depths.get(key, 0) or 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                remaining = depth
                if remaining:
                    depths[key] = remaining
                else:
                    depths.pop(key, None)
            return

        marker = _read_marker(marker_path)
        if marker is not None:
            active = _marker_is_active(marker)
            if active is None:
                raise ResourceLeaseError("Невозможно подтвердить владельца занятого игрового lease")
            if active:
                raise ResourceLeaseError("Игровой runtime уже занят другим процессом")
            try:
                atomic_remove(marker_path)
            except OSError as exc:
                raise ResourceLeaseError("Не удалось удалить устаревшую identity игрового lease") from exc

        created_at = _process_created_at(os.getpid())
        if created_at is None:
            raise ResourceLeaseError("Невозможно подтвердить identity текущего процесса для игрового lease")
        try:
            atomic_write(
                marker_path,
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_at": created_at,
                        "acquired_at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        except OSError as exc:
            raise ResourceLeaseError("Не удалось записать identity игрового lease") from exc
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
            try:
                atomic_remove(marker_path)
            except OSError as exc:
                raise ResourceLeaseError("Не удалось очистить identity игрового lease") from exc


__all__ = [
    "GAME_RUNTIME_LEASE_RETRY_INTERVAL_SECONDS",
    "GAME_RUNTIME_LEASE_TIMEOUT_SECONDS",
    "ResourceLeaseError",
    "game_runtime_lease",
    "game_runtime_lease_path",
]
