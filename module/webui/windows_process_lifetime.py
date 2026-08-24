"""Защита дерева процессов WebUI от осиротевших процессов в Windows."""

from __future__ import annotations

import _thread
import ctypes
import os
import threading
from ctypes import wintypes
from typing import NoReturn


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_LIFECYCLE_STOP_EVENT_ENV = "AZURPILOT_LIFECYCLE_STOP_EVENT"
_CONSOLE_PARENT_NAMES = frozenset(
    {
        "cmd.exe",
        "conhost.exe",
        "openconsole.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
    }
)
_LAUNCHER_BRIDGE_NAMES = frozenset(
    {
        "py.exe",
        "python.exe",
        "pythonw.exe",
        "uv.exe",
    }
)

_guard_lock = threading.Lock()
_process_tree_job_handle: int | None = None
_parent_watch_started = False
_lifecycle_stop_watch_started = False


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _raise_last_win32_error(error_code: int, action: str) -> NoReturn:
    raise OSError(error_code, f"{action}: {ctypes.FormatError(error_code)}")


def _create_process_tree_job() -> int:
    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        _raise_last_win32_error(
            ctypes.get_last_error(),
            "Не удалось создать Windows Job Object",
        )

    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not configured:
        error_code = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        _raise_last_win32_error(
            error_code,
            "Не удалось включить завершение дерева процессов при закрытии Job Object",
        )

    assigned = kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess())
    if not assigned:
        error_code = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        _raise_last_win32_error(
            error_code,
            "Не удалось привязать WebUI к Windows Job Object",
        )
    return int(handle)


def _find_console_parent_process():
    """Найти ближайшую консоль запуска через допустимые launcher-процессы."""
    import psutil

    try:
        parent = psutil.Process(os.getpid()).parent()
    except psutil.Error:
        return None

    while parent is not None:
        try:
            process_name = parent.name().casefold()
            if process_name in _CONSOLE_PARENT_NAMES:
                return parent
            if process_name not in _LAUNCHER_BRIDGE_NAMES:
                return None
            parent = parent.parent()
        except psutil.Error:
            return None
    return None


def _wait_for_console_parent_exit(parent) -> None:
    import psutil

    try:
        parent.wait()
    except psutil.NoSuchProcess:
        # Консоль могла завершиться между обнаружением процесса и запуском watcher.
        pass
    except Exception as exc:
        from module.logger import logger

        logger.warning(
            f"[WebUI] Наблюдение за консолью PID {parent.pid} прервано: "
            f"{type(exc).__name__}: {exc}. Автозавершение по смерти консоли отключено"
        )
        return

    # Аварийный выход намеренный: закрытие собственного Job Object процессом
    # завершит все унаследовавшие его дочерние процессы без зависимости от finally.
    os._exit(0)


def _wait_for_lifecycle_stop_event(kernel32, handle: int, event_name: str) -> None:
    """Преобразовать repository-scoped Windows event в штатный KeyboardInterrupt."""
    try:
        wait_result = kernel32.WaitForSingleObject(handle, _INFINITE)
    finally:
        kernel32.CloseHandle(handle)

    if wait_result != _WAIT_OBJECT_0:
        from module.logger import logger

        logger.warning(
            "[WebUI] Ожидание координированного запроса остановки завершилось "
            f"неожиданным кодом {wait_result}; event={event_name}"
        )
        return

    _thread.interrupt_main()


def _start_lifecycle_stop_watcher() -> bool:
    """Открыть созданный Start event и включить однократный stop watcher."""
    event_name = os.environ.get(_LIFECYCLE_STOP_EVENT_ENV, "").strip()
    if not event_name:
        return False

    kernel32 = _kernel32()
    handle = kernel32.OpenEventW(_SYNCHRONIZE, False, event_name)
    if not handle:
        error_code = ctypes.get_last_error()
        from module.logger import logger

        logger.warning(
            "[WebUI] Не удалось открыть объект координации остановки AzurPilot "
            f"({event_name}): {ctypes.FormatError(error_code)}. "
            "Координированная остановка отключена"
        )
        return False

    threading.Thread(
        target=_wait_for_lifecycle_stop_event,
        args=(kernel32, int(handle), event_name),
        daemon=True,
        name="azurpilot-lifecycle-stop-watcher",
    ).start()
    return True


def install_windows_process_lifetime_guards() -> int | None:
    """Защитить WebUI и дочерние процессы от смерти управляющей консоли.

    В Windows корневой ``gui.py`` помещается в Job Object с
    ``KILL_ON_JOB_CLOSE``. Дескриптор намеренно живёт до завершения процесса:
    если сам ``gui.py`` будет убит, Windows закроет дескриптор и завершит всё
    его дерево. Ближайшая консоль запуска ищется только через известные
    launcher-процессы Python/uv; посторонняя родительская цепочка не обходится.
    Смерть найденной консоли немедленно завершает ``gui.py``.

    Возвращает PID отслеживаемой консольной оболочки либо ``None``.
    На других платформах функция ничего не делает.
    """
    global _parent_watch_started
    global _process_tree_job_handle
    global _lifecycle_stop_watch_started

    if os.name != "nt":
        return None

    with _guard_lock:
        if _process_tree_job_handle is None:
            _process_tree_job_handle = _create_process_tree_job()

        if not _lifecycle_stop_watch_started:
            _lifecycle_stop_watch_started = _start_lifecycle_stop_watcher()

        if _parent_watch_started:
            return None

        parent = _find_console_parent_process()
        if parent is None:
            return None

        threading.Thread(
            target=_wait_for_console_parent_exit,
            args=(parent,),
            daemon=True,
            name=f"webui-console-parent-watcher-{parent.pid}",
        ).start()
        _parent_watch_started = True
        return parent.pid
