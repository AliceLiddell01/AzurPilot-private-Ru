"""Транспортно-независимый контракт ephemeral runtime cache."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol

from module.application.errors import StorageError


class RuntimeCacheStatus(StrEnum):
    """Ограниченный набор состояний Redis runtime cache."""

    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    INVALID_DATA = "INVALID_DATA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RuntimeCacheHealth:
    """Безопасный результат bounded health probe."""

    status: RuntimeCacheStatus

    @property
    def available(self) -> bool:
        return self.status is RuntimeCacheStatus.READY


class RuntimeCacheError(StorageError):
    """Нормализованная ошибка cache boundary без provider payload."""

    code = "runtime_cache_error"

    def __init__(self, status: RuntimeCacheStatus):
        if status is RuntimeCacheStatus.READY:
            raise ValueError("READY не является ошибочным состоянием runtime cache.")
        self.status = status
        super().__init__(f"Runtime cache operation завершилась состоянием {status.value}.")


class RuntimeCache(Protocol):
    """Минимальный application-owned cache contract."""

    def health(self) -> RuntimeCacheHealth:
        """Проверить доступность cache без изменения состояния."""

    def get(self, key: str) -> bytes | None:
        """Получить bytes или отличимый cache miss."""

    def set(
        self,
        key: str,
        value: bytes,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """Записать bytes с абсолютным моментом истечения."""

    def delete(self, key: str) -> bool:
        """Удалить ключ и вернуть, был ли он найден."""

    def close(self) -> None:
        """Освободить текущие transport resources."""


_provider_lock = Lock()
_provider: Callable[[], RuntimeCache] | None = None


def install_runtime_cache_provider(provider: Callable[[], RuntimeCache]) -> None:
    """Установить transport provider на композиционной границе приложения."""

    if not callable(provider):
        raise TypeError("Provider runtime cache должен быть callable.")
    global _provider
    with _provider_lock:
        _provider = provider


def clear_runtime_cache_provider() -> None:
    """Сбросить process-local provider при завершении runtime."""

    global _provider
    with _provider_lock:
        _provider = None


def get_runtime_cache() -> RuntimeCache:
    """Получить cache через установленную composition root точку."""

    with _provider_lock:
        provider = _provider
    if provider is None:
        raise RuntimeCacheError(RuntimeCacheStatus.NOT_CONFIGURED)
    try:
        cache = provider()
    except RuntimeCacheError:
        raise
    except Exception as exc:  # граница provider завершается fail-closed
        raise RuntimeCacheError(RuntimeCacheStatus.UNKNOWN) from exc
    if cache is None:
        raise RuntimeCacheError(RuntimeCacheStatus.UNKNOWN)
    return cache


__all__ = (
    "RuntimeCache",
    "RuntimeCacheError",
    "RuntimeCacheHealth",
    "RuntimeCacheStatus",
    "clear_runtime_cache_provider",
    "get_runtime_cache",
    "install_runtime_cache_provider",
)
