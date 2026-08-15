import logging
from collections.abc import Callable, Hashable
from typing import Any

from rich.console import Console, ConsoleRenderable
from rich.highlighter import RegexHighlighter
from rich.logging import RichHandler
from rich.theme import Theme

from module.logging_core import DiagnosticContextHandler

class HTMLConsole(Console): ...
class Highlighter(RegexHighlighter): ...

WEB_THEME: Theme

logger_debug: bool
pyw_name: str

file_formatter: logging.Formatter
console_formatter: logging.Formatter
web_formatter: logging.Formatter

stdout_console: Console
console_hdlr: RichHandler
diagnostic_hdlr: DiagnosticContextHandler

def set_file_logger(
    name: str = pyw_name,
) -> None: ...
def set_func_logger(
    func: Callable[[ConsoleRenderable], None],
) -> None: ...
def log_suppressed(
    level: int,
    message: object,
    *,
    key: Hashable | None = None,
    payload: Any = ...,
    window: float | None = None,
) -> bool: ...
def finish_suppressed(key: Hashable) -> int: ...
def reset_suppression(key: Hashable | None = None) -> None: ...
def get_diagnostic_context(*, last_failure: bool = False) -> tuple[str, ...]: ...
def reset_diagnostic_context() -> None: ...

class __logger(logging.Logger):
    log_file: str
    diagnostic_log_file: str

    def rule(
        self,
        title: str = "",
        *,
        characters: str = "-",
        style: str = "rule.line",
        end: str = "\n",
        align: str = "center",
    ) -> None: ...
    def hr(
        self,
        title,
        level: int = 3,
    ) -> None: ...
    def attr(
        self,
        name,
        text,
    ) -> None: ...
    def attr_align(
        self,
        name,
        text,
        front="",
        align: int = 22,
    ) -> None: ...
    def set_file_logger(
        self,
        name: str = pyw_name,
    ) -> None: ...
    def set_func_logger(
        self,
        func: Callable[[ConsoleRenderable], None],
    ) -> None: ...
    def print(
        self,
        *objects: ConsoleRenderable,
        **kwargs,
    ) -> None: ...
    def log_suppressed(
        self,
        level: int,
        message: object,
        *,
        key: Hashable | None = None,
        payload: Any = ...,
        window: float | None = None,
    ) -> bool: ...
    def finish_suppressed(self, key: Hashable) -> int: ...
    def reset_suppression(self, key: Hashable | None = None) -> None: ...
    def get_diagnostic_context(self, *, last_failure: bool = False) -> tuple[str, ...]: ...
    def reset_diagnostic_context(self) -> None: ...

logger: __logger
