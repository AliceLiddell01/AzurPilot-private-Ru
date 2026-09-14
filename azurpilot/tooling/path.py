"""Проверка и user-level регистрация project console script."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from .contracts import CapabilityStatus, ResultCode
from .errors import ToolingError
from .filesystem import canonical_path, path_has_link

_MAX_USER_PATH_BYTES = 32 * 1024
_WINDOWS_ENVIRONMENT_MESSAGE = 0x001A
_WINDOWS_BROADCAST = 0xFFFF
_WINDOWS_SMTO_ABORT_IF_HUNG = 0x0002


@dataclass(frozen=True)
class ConsolePathStatus:
    """Bounded состояние installable script без публикации абсолютного пути."""

    installed: bool
    current_shell: bool
    user_scope: bool | None
    status: CapabilityStatus
    message: str


def console_script_path(python_executable: Path) -> Path:
    """Получить script рядом с project Python executable."""

    name = "azur.exe" if os.name == "nt" else "azur"
    return canonical_path(python_executable.parent / name)


def _entry_matches(entry: str, directory: Path) -> bool:
    value = entry.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].strip()
    if not value or len(value) > 4096:
        return False
    try:
        expanded = os.path.expandvars(value)
        candidate = Path(expanded)
        if not candidate.is_absolute():
            return False
        return os.path.normcase(str(canonical_path(candidate))) == os.path.normcase(
            str(directory)
        )
    except (OSError, ValueError):
        return False


def _contains_in_path(value: str | None, directory: Path) -> bool:
    if not value or len(value) > _MAX_USER_PATH_BYTES:
        return False
    return any(_entry_matches(entry, directory) for entry in value.split(os.pathsep))


def _current_shell_contains(directory: Path) -> bool:
    if not _contains_in_path(os.environ.get("PATH"), directory):
        return False
    command = which("azur")
    if not command:
        return False
    try:
        command_directory = Path(command).resolve(strict=False).parent
    except (OSError, ValueError):
        return False
    return os.path.normcase(str(command_directory)) == os.path.normcase(
        str(directory)
    )


def _read_user_path() -> tuple[str, int]:
    if os.name != "nt":
        return "", 0
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                value, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return "", winreg.REG_EXPAND_SZ
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ
    if value is None:
        return "", int(value_type)
    if not isinstance(value, str) or len(value) > _MAX_USER_PATH_BYTES:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Пользовательский PATH превышает безопасный bounded размер.",
        )
    return value, int(value_type)


def _broadcast_environment_change() -> None:
    if os.name != "nt":
        return
    try:
        result = ctypes.c_ulong()
        sent = ctypes.windll.user32.SendMessageTimeoutW(
            _WINDOWS_BROADCAST,
            _WINDOWS_ENVIRONMENT_MESSAGE,
            0,
            "Environment",
            _WINDOWS_SMTO_ABORT_IF_HUNG,
            5000,
            ctypes.byref(result),
        )
        if not sent:
            return
    except (AttributeError, OSError, TypeError):
        return


def inspect_console_path(python_executable: Path) -> ConsolePathStatus:
    """Проверить установку script и его доступность в текущем/user PATH."""

    script = console_script_path(python_executable)
    installed = script.is_file()
    directory = script.parent
    current_shell = installed and _current_shell_contains(directory)
    user_scope: bool | None = None
    if os.name == "nt":
        try:
            user_scope = installed and _contains_in_path(_read_user_path()[0], directory)
        except (ImportError, OSError, ToolingError):
            user_scope = False
    if not installed:
        return ConsolePathStatus(
            installed=False,
            current_shell=False,
            user_scope=user_scope,
            status=CapabilityStatus.NOT_CONFIGURED,
            message="Console script не найден; требуется установка package.",
        )
    if current_shell:
        return ConsolePathStatus(
            installed=True,
            current_shell=True,
            user_scope=user_scope,
            status=CapabilityStatus.READY,
            message="Console script project environment доступен через PATH текущего shell.",
        )
    if user_scope:
        return ConsolePathStatus(
            installed=True,
            current_shell=False,
            user_scope=True,
            status=CapabilityStatus.READY,
            message="Console script зарегистрирован в user PATH; откройте новый shell.",
        )
    return ConsolePathStatus(
        installed=True,
        current_shell=False,
        user_scope=user_scope,
        status=CapabilityStatus.NOT_CONFIGURED,
        message="Console script установлен, но не зарегистрирован в PATH.",
    )


def register_console_path(python_executable: Path) -> ConsolePathStatus:
    """Зарегистрировать project bin только в user PATH, если это поддержано."""

    before = inspect_console_path(python_executable)
    if not before.installed:
        return before
    if os.name != "nt":
        return ConsolePathStatus(
            installed=True,
            current_shell=before.current_shell,
            user_scope=None,
            status=(
                CapabilityStatus.READY
                if before.current_shell
                else CapabilityStatus.UNSUPPORTED
            ),
            message=(
                before.message
                if before.current_shell
                else "Автоматическая регистрация PATH не имеет единого shell-контракта на POSIX."
            ),
        )

    directory = console_script_path(python_executable).parent
    try:
        if path_has_link(directory) or not directory.is_dir():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Каталог console script имеет небезопасный тип.",
            )
        old_path, value_type = _read_user_path()
        entries = old_path.split(os.pathsep) if old_path else []
        if not any(_entry_matches(entry, directory) for entry in entries):
            new_path = os.pathsep.join((*entries, str(directory)))
            if len(new_path) > _MAX_USER_PATH_BYTES:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Пользовательский PATH превысит безопасный bounded размер.",
                )
            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                "Environment",
                0,
                winreg.KEY_READ | winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, "Path", 0, value_type, new_path)
            _broadcast_environment_change()
        after_path, _ = _read_user_path()
        if not _contains_in_path(after_path, directory):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Регистрация console script в user PATH не подтверждена.",
            )
    except (AttributeError, ImportError, OSError, ToolingError, TypeError, ValueError):
        return ConsolePathStatus(
            installed=True,
            current_shell=before.current_shell,
            user_scope=False,
            status=CapabilityStatus.FAILED,
            message="Не удалось зарегистрировать console script в user PATH.",
        )

    return inspect_console_path(python_executable)


__all__ = [
    "ConsolePathStatus",
    "console_script_path",
    "inspect_console_path",
    "register_console_path",
]
