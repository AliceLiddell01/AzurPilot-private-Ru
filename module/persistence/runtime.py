"""Безопасная для процессов точка сборки production-хранилища."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from module.application.runtime_storage import (
    RuntimeStorageService,
    clear_runtime_storage_provider,
    install_runtime_storage_provider,
)
from module.persistence.config import DEFAULT_BACKEND_MARKER_PATH, DatabaseSettings
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import load_local_postgres_environment
from module.persistence.unit_of_work import PostgresUnitOfWork

_lock = Lock()
_service: RuntimeStorageService | None = None
_engine: LazyEngine | None = None


def bootstrap_runtime_storage(
    marker_path: str | Path = DEFAULT_BACKEND_MARKER_PATH,
    *,
    require_ready: bool = True,
) -> RuntimeStorageService:
    """Установить один ленивый сервис и при необходимости проверить готовность."""

    global _engine, _service
    with _lock:
        if _service is None:
            local_environment = load_local_postgres_environment()
            settings = DatabaseSettings.from_backend_marker(marker_path)
            if local_environment is not None:
                local_environment.require_runtime_match(settings)
            _engine = LazyEngine(settings)
            engine = _engine
            _service = RuntimeStorageService(
                lambda: PostgresUnitOfWork(engine),
                runtime_timezone=ZoneInfo(settings.runtime_timezone),
            )
            service = _service
            install_runtime_storage_provider(lambda: service)
        engine = _engine
        service = _service
    if engine is None or service is None:
        raise RuntimeError("Точка сборки runtime-хранилища не инициализирована.")
    if require_ready:
        StorageHealthChecker(engine).require_ready()
    return service


def runtime_health() -> None:
    bootstrap_runtime_storage(require_ready=True)


def dispose_runtime_storage() -> None:
    global _engine, _service
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _service = None
        clear_runtime_storage_provider()


__all__ = ["bootstrap_runtime_storage", "dispose_runtime_storage", "runtime_health"]
