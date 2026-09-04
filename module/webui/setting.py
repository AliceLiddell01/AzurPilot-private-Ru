"""WebUI 设置与状态管理模块，维护界面偏好和持久化状态。
包括主题配置、展开折叠状态、依赖同步标记，
以及预览资源路径定义和缓存管理机制。"""

# 此文件专门用于管理 Web 界面自身的偏好设置及持久化状态类文件。
# 包括界面主题、常用项展开折叠状态以及各类预览占位图、图标资源的路径定义与缓存管理机制。
import multiprocessing
import os
import threading
from pathlib import Path
from multiprocessing.managers import SyncManager
from typing import TYPE_CHECKING, Callable, Generic, TypeVar

from deploy.atomic import atomic_remove, atomic_write

if TYPE_CHECKING:
    from module.config.config_updater import ConfigUpdater
    from module.webui.config import DeployConfig

T = TypeVar("T")


# 代码更新后，父监督器必须先完成独立环境同步，才能创建新的 WebUI 子进程。
DEPENDENCY_SYNC_PENDING_FILE = "./config/webui-dependency-sync-pending"


def _close_runtime_control_server(server: object | None) -> None:
    """Закрыть control server, если объект предоставляет штатный lifecycle hook."""

    if server is None:
        return
    close = getattr(server, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue after close failure.
            try:
                from module.logger import logger

                logger.warning(
                    f"Не удалось закрыть runtime control server во время очистки: {type(exc).__name__}"
                )
            except Exception:
                pass


def _ensure_gui_process_lifetime_guard() -> None:
    """Включить Windows-защиту только для корневого процесса ``gui.py``."""
    if os.name != "nt":
        return
    if multiprocessing.current_process().name != "MainProcess":
        return

    import sys

    if os.path.basename(sys.argv[0]).casefold() != "gui.py":
        return

    from module.logger import logger
    from module.webui.windows_process_lifetime import (
        install_windows_process_lifetime_guards,
    )

    try:
        parent_pid = install_windows_process_lifetime_guards()
    except OSError as exc:
        logger.exception_context(
            title="Не удалось включить защиту дерева процессов WebUI",
            exc=exc,
            impact=(
                "При аварийном закрытии управляющей консоли дочерние процессы "
                "могут остаться без владельца; запуск WebUI остановлен."
            ),
            action=(
                "Проверьте права управления процессами Windows и повторите запуск "
                "AzurPilot из обычной PowerShell-консоли."
            ),
            level=50,
        )
        raise RuntimeError("Защита жизненного цикла WebUI не инициализирована") from exc

    if parent_pid is None:
        logger.info("[WebUI] Защита дерева процессов Windows активирована")
    else:
        logger.info(
            f"[WebUI] Защита дерева процессов Windows активирована "
            f"(родительская консоль PID: {parent_pid})"
        )


def mark_dependency_sync_pending() -> None:
    """持久化依赖同步待处理状态，供新父进程在启动前恢复。"""
    atomic_write(DEPENDENCY_SYNC_PENDING_FILE, "pending\n")


def is_dependency_sync_pending() -> bool:
    """返回当前启动前是否必须执行依赖同步。"""
    return os.path.isfile(DEPENDENCY_SYNC_PENDING_FILE)


def clear_dependency_sync_pending() -> None:
    """仅在父监督器确认依赖同步成功后清除待处理状态。"""
    atomic_remove(DEPENDENCY_SYNC_PENDING_FILE)


class cached_class_property(Generic[T]):
    """
    Code from https://github.com/dssg/dickens
    Add typing support

    Descriptor decorator implementing a class-level, read-only
    property, which caches its results on the class(es) on which it
    operates.
    Inheritance is supported, insofar as the descriptor is never hidden
    by its cache; rather, it stores values under its access name with
    added underscores. For example, when wrapping getters named
    "choices", "choices_" or "_choices", each class's result is stored
    on the class at "_choices_"; decoration of a getter named
    "_choices_" would raise an exception.
    """

    class AliasConflict(ValueError):
        pass

    def __init__(self, func: Callable[..., T]):
        self.__func__ = func
        self.__cache_name__ = '_{}_'.format(func.__name__.strip('_'))
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
    """
    Shared settings
    """

    _init = False
    _clearup = False
    cleanup_lock = threading.Lock()
    restart_lock = threading.RLock()
    _restart_requested = False

    restart_event: threading.Event = None
    dependency_sync_event: threading.Event = None
    manager: SyncManager = None
    process_registry = None
    _runtime_control_server = None
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
        previous_server = cls._runtime_control_server
        cls._runtime_control_server = None
        _close_runtime_control_server(previous_server)
        manager = multiprocessing.Manager()
        cls.manager = manager
        # Browser sessions may run in separate processes, so workers need a
        # process-wide registry instead of session-local Python objects.
        cls.process_registry = manager.dict()
        from module.webui.worker_registry import claim_owner

        try:
            claim_owner(os.getpid())
        except Exception:
            # 所有者认领失败时不能留下无主的 Manager 子进程。
            cls.process_registry = None
            cls.manager = None
            cls._init = False
            try:
                manager.shutdown()
            except Exception:
                pass
            raise
        server = None
        try:
            from module.webui.runtime_control_owner import WebUIRuntimeControlOwner

            owner = WebUIRuntimeControlOwner(Path(__file__).resolve().parents[2])
            server = owner.start_server()
            cls._runtime_control_server = server
        except Exception:
            # Нельзя оставлять worker registry owner без control server: это
            # создало бы невидимый и неуправляемый runtime.
            _close_runtime_control_server(server)
            try:
                from module.webui.worker_registry import clear_owner

                clear_owner(os.getpid())
            except Exception as exc:  # noqa: BLE001 - ошибку rollback необходимо отразить в журнале.
                from module.logger import logger

                logger.exception_context(
                    title="Не удалось откатить регистрацию WebUI owner",
                    exc=exc,
                    impact="В registry могла остаться запись owner без control server.",
                    action="Проверьте состояние registry и выполните безопасное восстановление WebUI.",
                    level=40,
                )
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
            raise RuntimeError(f"Остались незавершённые записи рабочих процессов: {list(workers)}")
        cls._clearup = True
        server = cls._runtime_control_server
        cls._runtime_control_server = None
        _close_runtime_control_server(server)
        manager = cls.manager
        try:
            if manager is not None:
                manager.shutdown()
        finally:
            cls.manager = None
            cls.process_registry = None
            clear_owner(os.getpid())

    @cached_class_property
    def deploy_config(self) -> "DeployConfig":
        """Мигрировать UI locale до первого чтения и кеширования deploy-конфигурации."""
        _ensure_gui_process_lifetime_guard()

        from deploy.language_migration import migrate_deploy_language

        migration = migrate_deploy_language()
        if migration.changed:
            from module.logger import logger

            logger.info("[WebUI] Старое значение Language безопасно изменено на ru-RU")

        from module.webui.config import DeployConfig

        return DeployConfig()

    @cached_class_property
    def config_updater(self) -> "ConfigUpdater":
        """
        Returns:
            ConfigUpdater：
        """
        from module.config.config_updater import ConfigUpdater

        return ConfigUpdater()
