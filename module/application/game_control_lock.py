"""Общая межпроцессная сериализация mutation-операций профиля."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path

from module.application.errors import ResourceBusyError
from module.application.host_lock import application_host_lock

GAME_CONTROL_LOCK_TIMEOUT_SECONDS = 30.0
GAME_CONTROL_LOCK_RETRY_INTERVAL_SECONDS = 0.05


def profile_mutation_lock_path(
    profile: str,
    *,
    repository_root: Path | str | None = None,
) -> Path:
    """Вернуть lock path для профиля в его repository-scoped runtime state."""

    if not isinstance(profile, str) or not profile:
        raise ValueError("Имя профиля для mutation lock должно быть непустым")
    root = Path(repository_root) if repository_root is not None else Path.cwd()
    root = root.resolve(strict=False)
    # На Windows имена config case-insensitive; normcase сохраняет одну identity
    # для одного физического профиля при разном регистре входа.
    identity = os.path.normcase(profile)
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return root / "config" / "state" / "game-control" / f"{digest}.lock"


@contextmanager
def profile_mutation_lock(
    profile: str,
    *,
    repository_root: Path | str | None = None,
    timeout: float = GAME_CONTROL_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Захватить bounded cross-process lock для одной profile mutation."""

    manager = application_host_lock(
        profile_mutation_lock_path(profile, repository_root=repository_root),
        timeout=timeout,
        retry_interval=GAME_CONTROL_LOCK_RETRY_INTERVAL_SECONDS,
    )
    with ExitStack() as stack:
        try:
            stack.enter_context(manager)
        except TimeoutError:
            raise ResourceBusyError("Профиль занят другой control-операцией.") from None
        yield


__all__ = (
    "GAME_CONTROL_LOCK_RETRY_INTERVAL_SECONDS",
    "GAME_CONTROL_LOCK_TIMEOUT_SECONDS",
    "profile_mutation_lock",
    "profile_mutation_lock_path",
)
