"""Нативная Windows-граница для ``.lnk`` без PowerShell-оркестрации."""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import CapabilityStatus, ResultCode
from .errors import ToolingError
from .filesystem import StateLayout, is_unsafe_path, path_has_link
from .path import console_script_path


@dataclass(frozen=True)
class ShortcutSpecification:
    target: Path
    arguments: str
    working_directory: Path
    icon: Path
    description: str = "AzurPilot"


@dataclass(frozen=True)
class ShortcutResult:
    status: CapabilityStatus
    changed: bool
    path: Path | None
    backup_created: bool
    message: str


def default_shortcut_path() -> Path:
    if os.name != "nt":
        raise ToolingError(
            ResultCode.TOOLING_CAPABILITY_UNSUPPORTED,
            "Ярлык Windows не поддерживается на POSIX.",
        )
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ToolingError(
            ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
            "Не удалось определить пользовательское меню «Пуск».",
        )
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "AzurPilot.lnk"


def shortcut_specification(
    root: Path,
    python_executable: Path,
    *,
    icon: Path | None = None,
) -> ShortcutSpecification:
    script = console_script_path(python_executable)
    if script.is_file():
        target = script
        arguments = "start"
    else:
        target = python_executable
        arguments = "-m azurpilot start"
    icon_path = icon or (root / "assets" / "AzurPilot.ico")
    for candidate in (target, icon_path, root):
        if path_has_link(candidate) or not candidate.exists():
            raise ToolingError(
                ResultCode.TOOLING_SHORTCUT_FAILED,
                "Цель, значок или рабочий каталог ярлыка не прошли проверку.",
            )
    if not target.is_file() or not icon_path.is_file() or not root.is_dir():
        raise ToolingError(
            ResultCode.TOOLING_SHORTCUT_FAILED,
            "Цель, значок или рабочий каталог ярлыка имеют неверный тип.",
        )
    return ShortcutSpecification(
        target=target.resolve(strict=True),
        arguments=arguments,
        working_directory=root.resolve(strict=True),
        icon=icon_path.resolve(strict=True),
    )


def _guid(value: str):
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    parsed = uuid.UUID(value)
    return GUID(
        parsed.time_low,
        parsed.time_mid,
        parsed.time_hi_version,
        (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
    )


def _com_call(pointer: ctypes.c_void_p, index: int, restype, argtypes, *args):
    function_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.cast(table[index], function_type(restype, ctypes.c_void_p, *argtypes))
    return function(pointer, *args)


def _check_hresult(value: int, message: str) -> None:
    if value < 0:
        raise ToolingError(ResultCode.TOOLING_SHORTCUT_FAILED, message)


def _write_shell_link(spec: ShortcutSpecification, destination: Path) -> None:
    if os.name != "nt":
        raise ToolingError(
            ResultCode.TOOLING_CAPABILITY_UNSUPPORTED,
            "Ярлык Windows не поддерживается на POSIX.",
        )
    ole32 = ctypes.windll.ole32
    shell_link = ctypes.c_void_p()
    shell_link_iid = _guid("000214F9-0000-0000-C000-000000000046")
    persist_iid = _guid("0000010B-0000-0000-C000-000000000046")
    clsid = _guid("00021401-0000-0000-C000-000000000046")
    _check_hresult(ole32.CoInitializeEx(None, 2), "Не удалось инициализировать COM ярлыка Windows.")
    persist = ctypes.c_void_p()
    try:
        result = ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            1,
            ctypes.byref(shell_link_iid),
            ctypes.byref(shell_link),
        )
        _check_hresult(result, "Не удалось создать Windows ShellLink.")
        _check_hresult(
            _com_call(shell_link, 20, ctypes.c_long, [ctypes.c_wchar_p], str(spec.target)),
            "Не удалось задать цель ярлыка.",
        )
        _check_hresult(
            _com_call(shell_link, 11, ctypes.c_long, [ctypes.c_wchar_p], spec.arguments),
            "Не удалось задать аргументы ярлыка.",
        )
        _check_hresult(
            _com_call(
                shell_link,
                9,
                ctypes.c_long,
                [ctypes.c_wchar_p],
                str(spec.working_directory),
            ),
            "Не удалось задать рабочий каталог ярлыка.",
        )
        _check_hresult(
            _com_call(shell_link, 7, ctypes.c_long, [ctypes.c_wchar_p], spec.description),
            "Не удалось задать описание ярлыка.",
        )
        _check_hresult(
            _com_call(
                shell_link,
                17,
                ctypes.c_long,
                [ctypes.c_wchar_p, ctypes.c_int],
                str(spec.icon),
                0,
            ),
            "Не удалось задать значок ярлыка.",
        )
        _check_hresult(
            _com_call(
                shell_link,
                0,
                ctypes.c_long,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(persist_iid),
                ctypes.byref(persist),
            ),
            "Не удалось получить persist interface ярлыка.",
        )
        _check_hresult(
            _com_call(
                persist,
                6,
                ctypes.c_long,
                [ctypes.c_wchar_p, ctypes.c_int],
                str(destination),
                1,
            ),
            "Не удалось сохранить ярлык.",
        )
    finally:
        if persist:
            _com_call(persist, 2, ctypes.c_ulong, [])
        if shell_link:
            _com_call(shell_link, 2, ctypes.c_ulong, [])
        ole32.CoUninitialize()


def _read_shell_link(path: Path) -> ShortcutSpecification:
    """Прочитать поля ShellLink для проверки постусловия."""

    if os.name != "nt":
        raise ToolingError(ResultCode.TOOLING_CAPABILITY_UNSUPPORTED, "Ярлык Windows не поддерживается на POSIX.")
    ole32 = ctypes.windll.ole32
    shell_link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    shell_link_iid = _guid("000214F9-0000-0000-C000-000000000046")
    persist_iid = _guid("0000010B-0000-0000-C000-000000000046")
    clsid = _guid("00021401-0000-0000-C000-000000000046")
    _check_hresult(ole32.CoInitializeEx(None, 2), "Не удалось инициализировать COM ярлыка Windows.")
    try:
        _check_hresult(
            ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(shell_link_iid), ctypes.byref(shell_link)
            ),
            "Не удалось открыть Windows ShellLink.",
        )
        _check_hresult(
            _com_call(
                shell_link,
                0,
                ctypes.c_long,
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(persist_iid),
                ctypes.byref(persist),
            ),
            "Не удалось получить persist interface ярлыка.",
        )
        _check_hresult(
            _com_call(persist, 5, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_uint32], str(path), 0),
            "Не удалось загрузить ярлык.",
        )
        target = ctypes.create_unicode_buffer(32768)
        working = ctypes.create_unicode_buffer(32768)
        arguments = ctypes.create_unicode_buffer(32768)
        icon = ctypes.create_unicode_buffer(32768)
        icon_index = ctypes.c_int()
        _check_hresult(
            _com_call(
                shell_link,
                3,
                ctypes.c_long,
                [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
                target,
                len(target),
                None,
                0,
            ),
            "Не удалось прочитать цель ярлыка.",
        )
        _check_hresult(
            _com_call(shell_link, 8, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int], working, len(working)),
            "Не удалось прочитать рабочий каталог ярлыка.",
        )
        _check_hresult(
            _com_call(shell_link, 10, ctypes.c_long, [ctypes.c_wchar_p, ctypes.c_int], arguments, len(arguments)),
            "Не удалось прочитать аргументы ярлыка.",
        )
        _check_hresult(
            _com_call(
                shell_link,
                16,
                ctypes.c_long,
                [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)],
                icon,
                len(icon),
                ctypes.byref(icon_index),
            ),
            "Не удалось прочитать значок ярлыка.",
        )
        return ShortcutSpecification(
            target=Path(target.value),
            arguments=arguments.value,
            working_directory=Path(working.value),
            icon=Path(icon.value.split(",", 1)[0]),
        )
    finally:
        if persist:
            _com_call(persist, 2, ctypes.c_ulong, [])
        if shell_link:
            _com_call(shell_link, 2, ctypes.c_ulong, [])
        ole32.CoUninitialize()


def _same_spec(actual: ShortcutSpecification, expected: ShortcutSpecification) -> bool:
    return (
        actual.target.resolve(strict=False) == expected.target.resolve(strict=False)
        and actual.arguments == expected.arguments
        and actual.working_directory.resolve(strict=False)
        == expected.working_directory.resolve(strict=False)
        and actual.icon.resolve(strict=False) == expected.icon.resolve(strict=False)
    )


def ensure_shortcut(
    root: Path,
    python_executable: Path,
    layout: StateLayout,
    *,
    shortcut_path: Path | None = None,
    icon_path: Path | None = None,
) -> ShortcutResult:
    """Создать или атомарно заменить пользовательский ярлык и проверить поля."""

    if os.name != "nt":
        return ShortcutResult(
            CapabilityStatus.UNSUPPORTED,
            False,
            None,
            False,
            "Ярлык Windows не является возможностью POSIX.",
        )
    layout.ensure()
    raw_path = (shortcut_path or default_shortcut_path()).expanduser()
    if not raw_path.is_absolute() or path_has_link(raw_path) or path_has_link(raw_path.parent):
        raise ToolingError(
            ResultCode.TOOLING_SHORTCUT_FAILED,
            "Путь ярлыка должен быть абсолютным и не содержать symlink или reparse point.",
        )
    path = raw_path.resolve(strict=False)
    if path.suffix.casefold() != ".lnk" or path_has_link(path) or path_has_link(path.parent):
        raise ToolingError(
            ResultCode.TOOLING_SHORTCUT_FAILED,
            "Путь ярлыка содержит symlink/reparse point или не имеет расширения .lnk.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_unsafe_path(path.parent):
        raise ToolingError(ResultCode.TOOLING_SHORTCUT_FAILED, "Каталог ярлыка небезопасен.")
    expected = shortcut_specification(root, python_executable, icon=icon_path)
    if path.is_file():
        try:
            if _same_spec(_read_shell_link(path), expected):
                return ShortcutResult(CapabilityStatus.READY, False, path, False, "Ярлык Windows уже соответствует контракту.")
        except (OSError, ToolingError, ValueError):
            pass
    backup_created = False
    backup_directory = layout.backups_directory / "shortcuts" / uuid.uuid4().hex
    backup_path = backup_directory / path.name
    if (
        path_has_link(backup_directory)
        or path_has_link(backup_directory.parent)
        or os.path.lexists(str(backup_path))
    ):
        raise ToolingError(
            ResultCode.TOOLING_SHORTCUT_FAILED,
            "Каталог резервной копии ярлыка содержит symlink или reparse point.",
        )
    if path.exists():
        if not path.is_file() or is_unsafe_path(path):
            raise ToolingError(ResultCode.TOOLING_SHORTCUT_FAILED, "Существующий ярлык имеет небезопасный тип.")
        backup_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
        backup_created = True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".lnk", dir=str(path.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_shell_link(expected, temporary)
        if not temporary.is_file():
            raise ToolingError(ResultCode.TOOLING_SHORTCUT_FAILED, "Временный ярлык не создан.")
        os.replace(temporary, path)
        if not _same_spec(_read_shell_link(path), expected):
            raise ToolingError(ResultCode.TOOLING_SHORTCUT_FAILED, "Постусловие ярлыка не подтверждено.")
        if backup_created:
            if path_has_link(backup_directory):
                raise ToolingError(
                    ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                    "Каталог резервной копии ярлыка стал небезопасным после записи.",
                )
            shutil.rmtree(backup_directory)
    except (OSError, ToolingError, ValueError) as error:
        try:
            if backup_created and backup_path.is_file():
                os.replace(backup_path, path)
            elif not backup_created:
                path.unlink(missing_ok=True)
        except OSError as rollback_error:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Ярлык не создан, а восстановление прежнего файла не подтверждено.",
            ) from rollback_error
        if isinstance(error, ToolingError):
            raise
        raise ToolingError(
            ResultCode.TOOLING_SHORTCUT_FAILED,
            "Не удалось создать или проверить ярлык Windows.",
        ) from error
    finally:
        temporary.unlink(missing_ok=True)
    return ShortcutResult(CapabilityStatus.READY, True, path, backup_created, "Ярлык Windows создан и проверен.")


__all__ = [
    "ShortcutResult",
    "ShortcutSpecification",
    "default_shortcut_path",
    "ensure_shortcut",
    "shortcut_specification",
]
