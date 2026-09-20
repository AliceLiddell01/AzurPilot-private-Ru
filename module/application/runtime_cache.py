"""Транспортно-независимый контракт ephemeral runtime cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
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


__all__ = (
    "RuntimeCache",
    "RuntimeCacheError",
    "RuntimeCacheHealth",
    "RuntimeCacheStatus",
)
