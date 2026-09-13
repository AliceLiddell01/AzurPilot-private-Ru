"""Устойчивый контракт операций устройства для project adapters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class DeviceService(Protocol):
    """Операции жизненного цикла удалённой службы устройства."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def running(self) -> bool: ...


class DeviceTouch(Protocol):
    """Пошаговый ввод touch-событий."""

    def down(self, x: int | float, y: int | float) -> DeviceTouch: ...

    def move(self, x: int | float, y: int | float) -> DeviceTouch: ...

    def up(self, x: int | float, y: int | float) -> DeviceTouch: ...


class Uiautomator2Device(Protocol):
    """Минимальный общий API локального и phone-cloud транспорта."""

    @property
    def serial(self) -> str: ...

    @property
    def info(self) -> dict[str, Any]: ...

    @property
    def clipboard(self) -> str | None: ...

    @property
    def touch(self) -> DeviceTouch: ...

    @property
    def xpath(self) -> Any: ...

    @property
    def wait_timeout(self) -> float: ...

    @wait_timeout.setter
    def wait_timeout(self, value: float) -> None: ...

    def click(self, x: int | float, y: int | float) -> Any: ...

    def long_click(self, x: int | float, y: int | float, duration: float = 0.5) -> Any: ...

    def swipe(
        self,
        fx: int | float,
        fy: int | float,
        tx: int | float,
        ty: int | float,
        duration: float | None = None,
        steps: int | None = None,
    ) -> Any: ...

    def shell(
        self,
        cmdargs: str | Sequence[str],
        stream: bool = False,
        timeout: float | None = 60,
    ) -> Any: ...

    def screenshot(self, filename: str | None = None, format: str = "pillow") -> Any: ...

    def dump_hierarchy(
        self,
        compressed: bool = False,
        pretty: bool = False,
        max_depth: int | None = None,
        root_in_active: bool | None = None,
    ) -> str: ...

    def window_size(self) -> tuple[int, int]: ...

    def app_current(self) -> dict[str, Any]: ...

    def app_info(self, package_name: str) -> dict[str, Any]: ...

    def app_stop(self, package_name: str) -> Any: ...

    def reset_uiautomator(self) -> Any: ...

    def start_uiautomator(self) -> Any: ...

    def stop_uiautomator(self, wait: bool = True) -> Any: ...

    def service(self, name: str) -> DeviceService: ...

    def set_fastinput_ime(self, enable: bool = True) -> Any: ...

    def send_keys(self, text: str, clear: bool = False) -> Any: ...

    def send_action(self, code: Any = None) -> Any: ...

    def clear_text(self) -> Any: ...

    def current_ime(self) -> Any: ...

    def set_clipboard(self, text: str, label: str | None = None) -> Any: ...
