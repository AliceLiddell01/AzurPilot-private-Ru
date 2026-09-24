"""Настройки и состояние WebUI: предпочтения интерфейса, маркер синхронизации зависимостей и кэш ресурсов."""

# Этот файл предназначен для управления настройками самого Web-интерфейса и классами сохраняемого состояния.
# Включает тему интерфейса, состояние раскрытия/сворачивания часто используемых элементов, пути к изображениям-заглушкам предпросмотра и ресурсам иконок, а также механизм управления кэшем.
import os
import multiprocessing
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from deploy.atomic import atomic_remove, atomic_write
from module.base.decorator import cached_class_property

if TYPE_CHECKING:
    from module.config.config_updater import ConfigUpdater
    from module.webui.config import DeployConfig

# После обновления кода родительский супервизор должен сначала завершить синхронизацию изолированного окружения и только затем создавать новый дочерний процесс WebUI.
DEPENDENCY_SYNC_PENDING_FILE = "./config/webui-dependency-sync-pending"


def _stop_notification_runtime(runtime: object | None) -> None:
    if runtime is None:
        return
    stop = getattr(runtime, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception as exc:  # noqa: BLE001 - очистка не должна оставлять manager без владельца.
            try:
                from module.logger import logger

                logger.warning(
                    "Не удалось остановить notification runtime при очистке: %s",
                    type(exc).__name__,
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
    _notification_runtime = None
    _desktop_agent_runtime = None
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
    def get_notification_runtime(cls):
        """Вернуть application notification runtime текущего WebUI owner."""

        return cls._notification_runtime

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
    def init(cls, *, notification_runtime=None, desktop_agent_runtime=None):
        cls._clearup = False
        cls._restart_requested = False
        previous_desktop_agent_runtime = cls._desktop_agent_runtime
        cls._desktop_agent_runtime = None
        _stop_notification_runtime(previous_desktop_agent_runtime)
        previous_notification_runtime = cls._notification_runtime
        cls._notification_runtime = None
        _stop_notification_runtime(previous_notification_runtime)
        try:
            cls._notification_runtime = notification_runtime
            cls._desktop_agent_runtime = desktop_agent_runtime
            if notification_runtime is not None:
                start = getattr(notification_runtime, "start", None)
                if callable(start):
                    start()
            if desktop_agent_runtime is not None:
                start = getattr(desktop_agent_runtime, "start", None)
                if callable(start):
                    start()
        except Exception:
            cls._notification_runtime = None
            cls._desktop_agent_runtime = None
            _stop_notification_runtime(notification_runtime)
            _stop_notification_runtime(desktop_agent_runtime)
            cls._init = False
            raise
        cls._init = True

    @classmethod
    def clearup(cls):
        if cls._clearup:
            return
        cls._clearup = True
        desktop_agent_runtime = cls._desktop_agent_runtime
        cls._desktop_agent_runtime = None
        _stop_notification_runtime(desktop_agent_runtime)
        notification_runtime = cls._notification_runtime
        cls._notification_runtime = None
        _stop_notification_runtime(notification_runtime)

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
