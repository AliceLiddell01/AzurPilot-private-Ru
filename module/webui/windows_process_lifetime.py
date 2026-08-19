"""Защита дерева процессов WebUI от осиротевших процессов в Windows."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_CONSOLE_PARENT_NAMES = frozenset(
    {
        "cmd.exe",
        "conhost.exe",
        "powershell.exe",
        "pwsh.exe",
        "windowsterminal.exe",
    }
)

_guard_lock = threading.Lock()
_process_tree_job_handle: int | None = None
_parent_watch_started = False


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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _raise_last_win32_error(action: str) -> None:
    error_code = ctypes.get_last_error()
    raise OSError(error_code, f"{action}: {ctypes.FormatError(error_code)}")


def _create_process_tree_job() -> int:
    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        _raise_last_win32_error("Не удалось создать Windows Job Object")

    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not configured:
        kernel32.CloseHandle(handle)
        _raise_last_win32_error(
            "Не удалось включить завершение дерева процессов при закрытии Job Object"
        )

    assigned = kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess())
    if not assigned:
        kernel32.CloseHandle(handle)
        _raise_last_win32_error(
            "Не удалось привязать WebUI к Windows Job Object"
        )
    return int(handle)


def _console_parent_process():
    try:
        import psutil

        parent = psutil.Process(os.getppid())
        if parent.name().casefold() not in _CONSOLE_PARENT_NAMES:
            return None
        return parent
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _wait_for_console_parent_exit(parent) -> None:
    try:
        parent.wait()
    except Exception:
        return

    # Аварийный выход намеренный: закрытие собственного Job Object процессом
    # завершит все унаследовавшие его дочерние процессы без зависимости от finally.
    os._exit(0)


def install_windows_process_lifetime_guards() -> int | None:
    """Защитить WebUI и дочерние процессы от смерти управляющей консоли.

    В Windows корневой ``gui.py`` помещается в Job Object с
    ``KILL_ON_JOB_CLOSE``. Дескриптор намеренно живёт до завершения процесса:
    если сам ``gui.py`` будет убит, Windows закроет дескриптор и завершит всё
    его дерево. Если непосредственным родителем является консольная оболочка,
    отдельный поток ждёт её завершения и немедленно завершает ``gui.py``.

    Возвращает PID отслеживаемой консольной оболочки либо ``None``.
    На других платформах функция ничего не делает.
    """
    global _parent_watch_started
    global _process_tree_job_handle

    if os.name != "nt":
        return None

    with _guard_lock:
        if _process_tree_job_handle is None:
            _process_tree_job_handle = _create_process_tree_job()

        if _parent_watch_started:
            return None

        parent = _console_parent_process()
        if parent is None:
            return None

        parent_pid = parent.pid
        threading.Thread(
            target=_wait_for_console_parent_exit,
            args=(parent,),
            daemon=True,
            name="webui-console-parent-watcher",
        ).start()
        _parent_watch_started = True
        return parent_pid
