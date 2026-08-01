"""WebUI настройки и управление состоянием, включая предпочтения интерфейса."""

import multiprocessing
import os
import threading
from multiprocessing.managers import SyncManager
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from deploy.atomic import atomic_remove, atomic_write

if TYPE_CHECKING:
    from module.config.config_updater import ConfigUpdater
    from module.webui.config import DeployConfig

T = TypeVar("T")


DEPENDENCY_SYNC_PENDING_FILE = "./config/webui-dependency-sync-pending"


def mark_dependency_sync_pending() -> None:
    """Сохранить признак обязательной синхронизации зависимостей перед запуском."""
    atomic_write(DEPENDENCY_SYNC_PENDING_FILE, "pending\n")


def is_dependency_sync_pending() -> bool:
    """Вернуть, требуется ли синхронизация зависимостей перед запуском."""
    return os.path.isfile(DEPENDENCY_SYNC_PENDING_FILE)


def clear_dependency_sync_pending() -> None:
    """Удалить признак только после успешной синхронизации родительским процессом."""
    atomic_remove(DEPENDENCY_SYNC_PENDING_FILE)


class cached_class_property(Generic[T]):
    """Кешируемое свойство уровня класса с поддержкой типов."""

    class AliasConflict(ValueError):
        pass

    def __init__(self, func: Callable[..., T]):
        self.__func__ = func
        self.__cache_name__ = "_{}_".format(func.__name__.strip("_"))
        if self.__cache_name__ == func.__name__:
            raise self.AliasConflict(self.__cache_name__)

    def __get__(self, instance, cls=None) -> T:
        if cls is None:
            cls = type(instance)

        try:
            return vars(cls)[self.__cache_name__]
        except KeyError:
            result = self.__func__(cls)
            setattr(cls, self.__cache_name__, result)
            return result


class State:
    """Общее состояние WebUI."""

    _init = False
    _clearup = False
    cleanup_lock = threading.Lock()
    restart_lock = threading.RLock()
    _restart_requested = False

    restart_event: threading.Event = None
    dependency_sync_event: threading.Event = None
    manager: SyncManager = None
    process_registry = None
    electron: bool = False
    webui_host: str = None
    theme: str = "default"
    placeholder_images: list = [
        "screen1.jpg",
        "screen2.jpg",
        "screen3.jpg",
        "screen4.png",
        "screen5.png",
        "screen6.png",
        "screen7.png",
        "screen8.jpg",
        "screen9.png",
    ]
    placeholder_index: int = 0

    @classmethod
    def get_placeholder_url(cls) -> str:
        try:
            idx = getattr(cls.deploy_config, "PlaceholderIndex", None)
            if idx is not None:
                try:
                    idx = int(idx)
                    cls.placeholder_index = idx % len(cls.placeholder_images)
                except Exception:
                    pass
        except Exception:
            pass

        name = cls.placeholder_images[cls.placeholder_index % len(cls.placeholder_images)]
        return f"static/assets/spa/{name}"

    @classmethod
    def toggle_placeholder(cls) -> str:
        return cls.advance_placeholder()

    @classmethod
    def advance_placeholder(cls) -> str:
        cls.placeholder_index = (cls.placeholder_index + 1) % len(cls.placeholder_images)
        try:
            cls.deploy_config.PlaceholderIndex = cls.placeholder_index
        except Exception:
            pass
        name = cls.placeholder_images[cls.placeholder_index]
        return f"static/assets/spa/{name}"

    @classmethod
    def init(cls):
        cls._clearup = False
        cls._restart_requested = False
        manager = multiprocessing.Manager()
        cls.manager = manager
        cls.process_registry = manager.dict()
        from module.webui.worker_registry import claim_owner

        try:
            claim_owner(os.getpid())
        except Exception:
            cls.process_registry = None
            cls.manager = None
            cls._init = False
            try:
                manager.shutdown()
            except Exception:
                pass
            raise
        cls._init = True

    @classmethod
    def clearup(cls):
        if cls._clearup:
            return
        from module.webui.worker_registry import clear_owner, get_workers

        workers = get_workers(os.getpid())
        if workers:
            raise RuntimeError(f"Остались незавершённые worker-процессы: {list(workers)}")
        cls._clearup = True
        manager = cls.manager
        try:
            if manager is not None:
                manager.shutdown()
        finally:
            cls.manager = None
            cls.process_registry = None
            clear_owner(os.getpid())

    @cached_class_property
    def deploy_config(cls) -> "DeployConfig":
        """Мигрировать UI locale до первого чтения и кеширования deploy-конфигурации."""
        from deploy.language_migration import migrate_deploy_language

        migration = migrate_deploy_language()
        if migration.changed:
            from module.logger import logger

            logger.info("[WebUI] Старое значение Language безопасно изменено на ru-RU")

        from module.webui.config import DeployConfig

        return DeployConfig()

    @cached_class_property
    def config_updater(cls) -> "ConfigUpdater":
        """Вернуть кешированный обновлятор конфигурации."""
        from module.config.config_updater import ConfigUpdater

        return ConfigUpdater()
