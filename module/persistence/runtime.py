"""Безопасная для процессов точка сборки production-хранилища."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from module.application.fleet_state import (
    FleetScanService,
    FleetStateService,
    FormationFleetScanController,
)
from module.application.fleet_manual_scan import (
    FleetManualScanCommandService,
    FleetManualScanCoordinator,
)
from module.application.fleet_page import FleetPageQueryService
from module.application.runtime_storage import (
    RuntimeStorageService,
    clear_runtime_storage_provider,
    install_runtime_storage_provider,
)
from module.persistence.config import (
    DEFAULT_BACKEND_MARKER_PATH,
    LEGACY_BACKEND_MARKER_PATH,
    DatabaseSettings,
    migrate_legacy_backend_marker,
)
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import (
    DEFAULT_LOCAL_ENV_PATH,
    read_local_postgres_environment,
)
from module.persistence.unit_of_work import PostgresUnitOfWork

_lock = Lock()
_service: RuntimeStorageService | None = None
_engine: LazyEngine | None = None
_runtime_timezone: ZoneInfo | None = None
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RuntimeFleetStateContext:
    """Production Fleet State service поверх общего engine и runtime timezone."""

    state_service: FleetStateService
    runtime_timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class RuntimeFleetPageContext:
    """Сервисы чтения и отправки команд для процесса WebUI."""

    query_service: FleetPageQueryService
    command_service: FleetManualScanCommandService
    runtime_timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class RuntimeFleetManualScanContext:
    """Координатор рабочего процесса с ленивой привязкой к устройству планировщика."""

    coordinator: FleetManualScanCoordinator


def bootstrap_runtime_storage(
    marker_path: str | Path = DEFAULT_BACKEND_MARKER_PATH,
    *,
    require_ready: bool = True,
) -> RuntimeStorageService:
    """Установить один ленивый сервис и при необходимости проверить готовность."""

    global _engine, _runtime_timezone, _service
    with _lock:
        if _service is None:
            requested_marker = Path(marker_path)
            if requested_marker == DEFAULT_BACKEND_MARKER_PATH:
                resolved_marker = _REPOSITORY_ROOT / DEFAULT_BACKEND_MARKER_PATH
                migrate_legacy_backend_marker(
                    target=resolved_marker,
                    legacy=_REPOSITORY_ROOT / LEGACY_BACKEND_MARKER_PATH,
                )
            else:
                resolved_marker = requested_marker
            local_environment = read_local_postgres_environment(
                _REPOSITORY_ROOT / DEFAULT_LOCAL_ENV_PATH
            )
            settings = DatabaseSettings.from_backend_marker(resolved_marker)
            if local_environment is not None:
                local_environment.require_app_runtime_match(settings)
                local_environment.install(role="app")
            _engine = LazyEngine(settings)
            engine = _engine
            _runtime_timezone = ZoneInfo(settings.runtime_timezone)
            _service = RuntimeStorageService(
                lambda: PostgresUnitOfWork(engine),
                runtime_timezone=_runtime_timezone,
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


def build_runtime_fleet_state_context(
    controller_factory: Callable[[], FormationFleetScanController],
    *,
    clock: Callable[[], datetime] | None = None,
    require_ready: bool = True,
) -> RuntimeFleetStateContext:
    """Собрать Fleet State API без второго Engine и без раннего создания Device."""

    if not callable(controller_factory):
        raise TypeError("controller_factory должен быть callable")
    bootstrap_runtime_storage(require_ready=require_ready)
    with _lock:
        engine = _engine
        runtime_timezone = _runtime_timezone
    if engine is None or runtime_timezone is None:
        raise RuntimeError("Точка сборки Fleet State не инициализирована.")

    uow_factory = lambda: PostgresUnitOfWork(engine)

    def scan_service_factory() -> FleetScanService:
        return FleetScanService(
            uow_factory,
            controller_factory(),
            clock=clock,
        )

    return RuntimeFleetStateContext(
        state_service=FleetStateService(
            uow_factory,
            scan_service_factory,
            clock=clock,
        ),
        runtime_timezone=runtime_timezone,
    )


def build_runtime_fleet_page_context(
    *,
    clock: Callable[[], datetime] | None = None,
    require_ready: bool = True,
) -> RuntimeFleetPageContext:
    """Собрать сервисы WebUI-страницы флотов без устройства и контроллера сканирования."""

    bootstrap_runtime_storage(require_ready=require_ready)
    with _lock:
        engine = _engine
        runtime_timezone = _runtime_timezone
    if engine is None or runtime_timezone is None:
        raise RuntimeError("Точка сборки Fleet page не инициализирована.")
    uow_factory = lambda: PostgresUnitOfWork(engine)
    return RuntimeFleetPageContext(
        query_service=FleetPageQueryService(uow_factory),
        command_service=FleetManualScanCommandService(
            uow_factory,
            clock=clock,
        ),
        runtime_timezone=runtime_timezone,
    )


def build_runtime_fleet_manual_scan_context(
    controller_factory: Callable[[], FormationFleetScanController],
    *,
    clock: Callable[[], datetime] | None = None,
    require_ready: bool = True,
) -> RuntimeFleetManualScanContext:
    """Собрать устойчивый координатор команд для существующего рабочего процесса."""

    state_context = build_runtime_fleet_state_context(
        controller_factory,
        clock=clock,
        require_ready=require_ready,
    )
    with _lock:
        engine = _engine
    if engine is None:
        raise RuntimeError("Точка сборки manual Fleet scan не инициализирована.")
    command_service = FleetManualScanCommandService(
        lambda: PostgresUnitOfWork(engine),
        clock=clock,
    )
    return RuntimeFleetManualScanContext(
        coordinator=FleetManualScanCoordinator(
            command_service,
            state_context.state_service,
        )
    )


def runtime_health() -> None:
    bootstrap_runtime_storage(require_ready=True)


def dispose_runtime_storage() -> None:
    global _engine, _runtime_timezone, _service
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _runtime_timezone = None
        _service = None
        clear_runtime_storage_provider()


__all__ = [
    "RuntimeFleetManualScanContext",
    "RuntimeFleetPageContext",
    "RuntimeFleetStateContext",
    "bootstrap_runtime_storage",
    "build_runtime_fleet_manual_scan_context",
    "build_runtime_fleet_page_context",
    "build_runtime_fleet_state_context",
    "dispose_runtime_storage",
    "runtime_health",
]
