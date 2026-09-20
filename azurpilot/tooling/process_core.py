"""MCP-safe core structured process runner and ownership primitives."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Literal

import psutil

from .mcp_contracts import ProcessEvidence
from .mcp_errors import ProcessExecutionError, ToolingError
from .mcp_filesystem import bounded_read_text, is_unsafe_path
from .result import ResultCode

DEFAULT_OUTPUT_LIMIT = 64 * 1024
DEFAULT_PROCESS_TIMEOUT = 30.0
_VENV_CONFIG_LIMIT = 64 * 1024
_FORCE_TERMINATION_SIGNAL = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
DOCKER_ENVIRONMENT_KEYS = frozenset(
    {
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
)
MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS = {
    "azurpilot-dev": "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
    "azurpilot-game": "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
}
MCP_LOCAL_SOURCE_DIGEST_ENVIRONMENT_KEYS = frozenset(
    {
        "AZURPILOT_DEV_MCP_SOURCE_SET_DIGEST",
        "AZURPILOT_GAME_MCP_SOURCE_SET_DIGEST",
    }
)
MCP_LOCAL_TEST_ENVIRONMENT_PREFIX = "TEST_LOCAL_MCP_"
INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS = frozenset(
    {
        "CONTEXT7_API_KEY",
        "DOCKERHUB_PAT",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    }
)
INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS = frozenset(
    {"GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE"}
)
GRAFANA_URL_ENVIRONMENT_KEY = "GRAFANA_URL"


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _same_path(left: Path, right: Path) -> bool:
    if os.name == "nt":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


def _bounded_text(data: bytes, limit: int) -> tuple[str, bool]:
    bounded = data[:limit]
    return bounded.decode("utf-8", errors="replace"), len(data) > limit


def _redact_argument(value: str) -> str:
    lowered = value.lower()
    if any(
        token in lowered
        for token in ("password", "token", "secret", "cookie", "authorization")
    ):
        if "=" in value:
            return f"{value.split('=', 1)[0]}=<redacted>"
        return "<redacted>"
    if "=" in value:
        key, candidate = value.split("=", 1)
        if _is_absolute_path(candidate):
            return f"{key}=<path>"
    if _is_absolute_path(value):
        return "<path>"
    return value[:512]


def _is_absolute_path(value: str) -> bool:
    """Распознать POSIX-, Windows- и UNC-пути без раскрытия значения."""

    return os.path.isabs(value) or bool(
        re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", value)
    )


def _windows_venv_runtime(executable: Path) -> Path | None:
    """Найти фактическую среду Python за перенаправителем Windows venv."""

    if os.name != "nt":
        return None
    if executable.name.casefold() != "python.exe":
        return None
    if executable.parent.name.casefold() != "scripts":
        return None
    venv_root = executable.parent.parent
    config_path = venv_root / "pyvenv.cfg"
    try:
        if is_unsafe_path(config_path):
            return None
        config = bounded_read_text(config_path, max_bytes=_VENV_CONFIG_LIMIT)
    except (OSError, ToolingError, UnicodeError):
        return None

    home_value: str | None = None
    for line in config.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "home":
            home_value = value.strip()
            break
    if not home_value or "\x00" in home_value:
        return None
    home = Path(home_value).expanduser()
    if not home.is_absolute():
        return None
    try:
        runtime = home / "python.exe"
        if not runtime.is_file():
            return None
        canonical_runtime = _canonical(runtime)
        # uv может хранить home под junction; executable после canonicalization
        # всё равно должен быть обычным файлом, а не link-like object.
        if is_unsafe_path(canonical_runtime):
            return None
        return canonical_runtime
    except OSError:
        return None


def public_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Сделать argv пригодным для доказательств, не публикуя очевидные секреты."""

    return tuple(_redact_argument(str(item)) for item in argv[:32])


@dataclass(frozen=True)
class ProcessSpec:
    """Проверенная команда без разбора через shell."""

    executable: str | Path
    argv: tuple[str, ...] = ()
    cwd: Path = field(default_factory=Path.cwd)
    timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT
    max_output_bytes: int = DEFAULT_OUTPUT_LIMIT
    env: Mapping[str, str] = field(default_factory=dict)
    allow_test_environment: bool = False
    start_new_session: bool = True
    no_window: bool = True
    capture_output: bool = False

    def __post_init__(self) -> None:
        executable = str(self.executable)
        if not executable or "\x00" in executable:
            raise ValueError("executable должен быть непустой безопасной строкой")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 24 * 60 * 60:
            raise ValueError("timeout_seconds должен быть положительным и ограниченным")
        if self.max_output_bytes <= 0 or self.max_output_bytes > 16 * 1024 * 1024:
            raise ValueError("max_output_bytes должен быть ограниченным")
        if any(not isinstance(item, str) or "\x00" in item for item in self.argv):
            raise ValueError("argv должен содержать безопасные строки")
        if any(len(item) > 4096 for item in self.argv):
            raise ValueError("аргумент процесса слишком длинный")
        if any("\x00" in key or "\x00" in value for key, value in self.env.items()):
            raise ValueError("env не должен содержать NUL")
        cwd = _canonical(Path(self.cwd))
        if not cwd.is_dir():
            raise ValueError("cwd процесса не является каталогом")
        object.__setattr__(self, "cwd", cwd)

    @property
    def resolved_executable(self) -> Path:
        raw = Path(self.executable)
        candidate = raw if raw.is_absolute() else None
        if candidate is None and raw.parent != Path("."):
            candidate = self.cwd / raw
        if candidate is None:
            found = which(str(raw))
            if found is None:
                raise ProcessExecutionError(
                    code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                    message=f"Исполняемый файл {raw.name!r} не найден.",
                )
            candidate = Path(found)
        resolved = _canonical(candidate)
        if not resolved.is_file():
            raise ProcessExecutionError(
                code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                message="Исполняемый файл процесса не существует.",
            )
        return resolved

    @property
    def launch_executable(self) -> Path:
        """Вернуть фактический executable без Windows venv redirector."""

        resolved = self.resolved_executable
        return _windows_venv_runtime(resolved) or resolved

    @property
    def launch_environment(self) -> dict[str, str]:
        """Собрать окружение и связать базовую среду выполнения с ожидаемым venv."""

        resolved = self.resolved_executable
        runtime = _windows_venv_runtime(resolved)
        extra = dict(self.env)
        if runtime is not None:
            extra["__PYVENV_LAUNCHER__"] = str(resolved)
        return _safe_environment(
            extra, allow_test_environment=self.allow_test_environment
        )

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.resolved_executable), *self.argv)

    @property
    def launch_command(self) -> tuple[str, ...]:
        return (str(self.launch_executable), *self.argv)


@dataclass(frozen=True)
class ProcessIdentity:
    """PID identity, достаточная для безопасной проверки права на stop."""

    pid: int
    start_time: float
    executable: Path
    argv: tuple[str, ...]
    cwd: Path
    process_group: int | None = None

    @classmethod
    def capture(cls, pid: int, fallback: ProcessSpec | None = None) -> ProcessIdentity:
        process = psutil.Process(pid)
        start_time = float(process.create_time())
        try:
            executable = _canonical(Path(process.exe()))
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            if fallback is None:
                raise
            executable = fallback.launch_executable
        try:
            cmdline = tuple(str(item) for item in process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            cmdline = fallback.launch_command if fallback else (str(executable),)
        try:
            cwd = _canonical(Path(process.cwd()))
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            if fallback is None:
                raise
            cwd = fallback.cwd
        process_group: int | None = None
        if os.name != "nt":
            try:
                process_group = os.getpgid(pid)
            except OSError:
                process_group = None
        return cls(pid, start_time, executable, cmdline, cwd, process_group)

    def matches(self, process: psutil.Process | None = None) -> bool:
        """Проверить PID reuse и exact executable/argv/cwd."""

        try:
            current = process or psutil.Process(self.pid)
            return (
                abs(float(current.create_time()) - self.start_time) <= 0.05
                and _same_path(_canonical(Path(current.exe())), self.executable)
                and tuple(str(item) for item in current.cmdline()) == self.argv
                and _same_path(_canonical(Path(current.cwd())), self.cwd)
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            return False

    def public_evidence(self) -> ProcessEvidence:
        return ProcessEvidence(
            pid=self.pid,
            started_at=self.start_time,
            executable_name=self.executable.name,
            argv=public_argv(self.argv),
            working_directory_name=self.cwd.name or self.cwd.anchor or ".",
            identity_confirmed=True,
        )


@dataclass(frozen=True)
class ProcessResult:
    """Результат запуска с ограниченными stdout/stderr."""

    returncode: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    pid: int
    identity: ProcessIdentity
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


@dataclass
class RunningProcess:
    """Процесс, оставленный работать после ограниченного ожидания готовности."""

    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    spec: ProcessSpec | None = None
    stdout_buffer: bytearray | None = None
    stderr_buffer: bytearray | None = None
    output_locks: tuple[threading.Lock, threading.Lock] | None = None
    output_threads: tuple[threading.Thread, ...] = ()
    output_truncated: list[bool] = field(default_factory=lambda: [False, False])
    collected: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()

    def collect(self, timeout_seconds: float | None = None) -> ProcessResult:
        """Дождаться процесса и вернуть bounded stdout/stderr с сохранённой identity."""

        if self.collected:
            raise RuntimeError("Результат процесса уже был собран")
        spec = self.spec
        timeout = timeout_seconds or (spec.timeout_seconds if spec else DEFAULT_PROCESS_TIMEOUT)
        timed_out = False
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(self.process, self.identity)
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for thread in self.output_threads:
                thread.join(timeout=3.0)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

        self.collected = True
        max_output_bytes = spec.max_output_bytes if spec else DEFAULT_OUTPUT_LIMIT
        locks = self.output_locks
        if locks is None:
            stdout_data = bytes(self.stdout_buffer or b"")
            stderr_data = bytes(self.stderr_buffer or b"")
        else:
            with locks[0]:
                stdout_data = bytes(self.stdout_buffer or b"")
            with locks[1]:
                stderr_data = bytes(self.stderr_buffer or b"")
        stdout, stdout_truncated = _bounded_text(stdout_data, max_output_bytes)
        stderr, stderr_truncated = _bounded_text(stderr_data, max_output_bytes)
        return ProcessResult(
            returncode=self.process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=(self.output_truncated[0] if self.output_truncated else False)
            or stdout_truncated,
            stderr_truncated=(self.output_truncated[1] if self.output_truncated else False)
            or stderr_truncated,
            timed_out=timed_out,
            pid=self.process.pid,
            identity=self.identity,
            stdout_bytes=stdout_data,
            stderr_bytes=stderr_data,
        )


@dataclass(frozen=True)
class _OutputCapture:
    stdout_buffer: bytearray
    stderr_buffer: bytearray
    output_locks: tuple[threading.Lock, threading.Lock]
    output_threads: tuple[threading.Thread, ...]
    output_truncated: list[bool]


def _start_output_capture(
    process: subprocess.Popen[bytes], spec: ProcessSpec
) -> _OutputCapture:
    """Запустить одинаковый bounded drain для long-lived и one-shot процессов."""

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_locks = (threading.Lock(), threading.Lock())
    truncated = [False, False]

    def drain(stream: object, buffer: bytearray, index: int) -> None:
        if stream is None:
            return
        read = stream.read
        try:
            while True:
                chunk = read(8192)
                if not chunk:
                    return
                with output_locks[index]:
                    remaining = spec.max_output_bytes - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated[index] = True
        except (OSError, ValueError):
            return

    threads = (
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_buffer, 0),
            name=f"azurpilot-process-stdout-{process.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_buffer, 1),
            name=f"azurpilot-process-stderr-{process.pid}",
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()
    return _OutputCapture(
        stdout_buffer=stdout_buffer,
        stderr_buffer=stderr_buffer,
        output_locks=output_locks,
        output_threads=threads,
        output_truncated=truncated,
    )


def _safe_environment(
    extra: Mapping[str, str], *, allow_test_environment: bool = False
) -> dict[str, str]:
    """Собрать минимальное окружение без автоматического наследования secrets."""

    allowed_exact = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "AZURPILOT_SOURCE_REVISION",
        "NO_COLOR",
        "VIRTUAL_ENV",
    }
    allowed_prefixes = ("LC_",)
    allowed_explicit = allowed_exact | {
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
        "GH_PAGER",
        "GH_PROMPT_DISABLED",
        "__PYVENV_LAUNCHER__",
    } | DOCKER_ENVIRONMENT_KEYS
    allowed_explicit |= (
        set(MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS.values())
        | MCP_LOCAL_SOURCE_DIGEST_ENVIRONMENT_KEYS
        | set(INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS)
        | set(INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS)
        | {GRAFANA_URL_ENVIRONMENT_KEY}
    )
    result = {
        key: value
        for key, value in os.environ.items()
        if key in allowed_exact or key.startswith(allowed_prefixes)
    }
    if len(extra) > 32:
        raise ValueError("число явных переменных окружения превышает лимит")
    for key, value in extra.items():
        if not isinstance(key, str) or not key or any(
            character in key for character in "=\x00"
        ):
            raise ValueError("недопустимое имя переменной окружения")
        if (
            key not in allowed_explicit
            and not key.startswith(allowed_prefixes)
            and not (
                allow_test_environment
                and key.startswith(MCP_LOCAL_TEST_ENVIRONMENT_PREFIX)
            )
        ):
            raise ValueError(f"переменная окружения {key!r} запрещена политикой")
        if len(key) > 128 or len(str(value)) > 4096:
            raise ValueError("переменная окружения превышает ограниченный размер")
        result[str(key)] = str(value)
    return result


def safe_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Вернуть ограниченную политику окружения для внешнего канонического адаптера."""

    return _safe_environment(extra or {})


def docker_environment() -> dict[str, str]:
    """Передать Docker CLI только выбранные оператором параметры окружения."""

    return {
        key: value
        for key, value in os.environ.items()
        if key in DOCKER_ENVIRONMENT_KEYS
    }


def _signal_process_group(process_group: int | None, signal_number: int) -> bool:
    if os.name == "nt" or process_group is None:
        return False
    try:
        if process_group == os.getpgid(0):
            return False
        os.killpg(process_group, signal_number)
    except OSError:
        return False
    return True


def _terminate_process(
    process: subprocess.Popen[bytes], identity: ProcessIdentity
) -> None:
    """Остановить только процесс, запущенный данным runner, и его группу."""

    if process.poll() is not None:
        return
    if not identity.matches():
        return
    try:
        descendants = tuple(psutil.Process(identity.pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = ()
    if not _signal_process_group(identity.process_group, signal.SIGTERM):
        try:
            process.terminate()
        except OSError:
            pass
        for child in descendants:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        if not _signal_process_group(
            identity.process_group, _FORCE_TERMINATION_SIGNAL
        ):
            try:
                process.kill()
            except OSError:
                pass
    for child in descendants:
        try:
            child.wait(timeout=2.0)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess, psutil.AccessDenied):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


def _cleanup_process_instance(process: subprocess.Popen[bytes]) -> None:
    """Убрать только что созданный Popen, если identity не удалось снять."""

    if process.poll() is not None:
        return
    try:
        descendants = tuple(psutil.Process(process.pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = ()
    process_group = None
    if os.name != "nt":
        try:
            process_group = os.getpgid(process.pid)
        except OSError:
            pass
    group_signaled = _signal_process_group(process_group, signal.SIGTERM)
    if not group_signaled:
        try:
            process.terminate()
        except OSError:
            pass
        for child in descendants:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        if not _signal_process_group(process_group, _FORCE_TERMINATION_SIGNAL):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
    for child in descendants:
        try:
            child.wait(timeout=2.0)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess, psutil.AccessDenied):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


def _is_process_running(process: psutil.Process) -> bool:
    try:
        return process.is_running()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


class StructuredProcessRunner:
    """Единая точка запуска Git, uv и project entrypoints."""

    def start(self, spec: ProcessSpec) -> RunningProcess:
        """Создать долгоживущий процесс без shell и без неограниченного захвата вывода."""

        executable = spec.launch_executable
        creationflags = 0
        if os.name == "nt" and spec.no_window:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [str(executable), *spec.argv],
                cwd=str(spec.cwd),
                env=spec.launch_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if spec.capture_output else subprocess.DEVNULL,
                stderr=subprocess.PIPE if spec.capture_output else subprocess.DEVNULL,
                shell=False,
                start_new_session=spec.start_new_session if os.name != "nt" else False,
                creationflags=creationflags,
            )
            identity = ProcessIdentity.capture(process.pid, spec)
        except (OSError, ValueError, psutil.Error) as exc:
            if process is not None:
                _cleanup_process_instance(process)
            raise ProcessExecutionError(
                code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                message="Не удалось создать или подтвердить долгоживущий процесс.",
            ) from exc
        running = RunningProcess(process=process, identity=identity, spec=spec)
        if not spec.capture_output:
            return running

        capture = _start_output_capture(process, spec)
        running.stdout_buffer = capture.stdout_buffer
        running.stderr_buffer = capture.stderr_buffer
        running.output_locks = capture.output_locks
        running.output_threads = capture.output_threads
        running.output_truncated = capture.output_truncated
        return running

    def run(self, spec: ProcessSpec) -> ProcessResult:
        executable = spec.launch_executable
        creationflags = 0
        if os.name == "nt" and spec.no_window:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [str(executable), *spec.argv],
                cwd=str(spec.cwd),
                env=spec.launch_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=spec.start_new_session if os.name != "nt" else False,
                creationflags=creationflags,
            )
        except (OSError, ValueError) as exc:
            raise ProcessExecutionError(
                code=ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                message="Не удалось создать структурированный процесс.",
            ) from exc

        try:
            identity = ProcessIdentity.capture(process.pid, spec)
        except (psutil.NoSuchProcess, OSError) as exc:
            if process.poll() is None:
                _cleanup_process_instance(process)
                raise ProcessExecutionError(
                    code=ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    message="Не удалось подтвердить идентичность созданного процесса.",
                ) from exc
            # Одноразовый CLI может завершиться между Popen и psutil capture.
            # Для уже завершённого процесса ownership recovery не выполняется;
            # fallback нужен только чтобы сохранить bounded stdout/returncode.
            identity = ProcessIdentity(
                pid=process.pid,
                start_time=time.time(),
                executable=spec.launch_executable,
                argv=spec.launch_command,
                cwd=spec.cwd,
            )
        except psutil.Error as exc:
            _cleanup_process_instance(process)
            raise ProcessExecutionError(
                code=ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                message="Не удалось подтвердить идентичность созданного процесса.",
            ) from exc

        capture = _start_output_capture(process, spec)

        deadline = time.monotonic() + spec.timeout_seconds
        timed_out = False
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process(process, identity)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for thread in capture.output_threads:
                thread.join(timeout=3.0)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

        with capture.output_locks[0]:
            stdout_data = bytes(capture.stdout_buffer)
            stdout_was_drained_truncated = capture.output_truncated[0]
        with capture.output_locks[1]:
            stderr_data = bytes(capture.stderr_buffer)
            stderr_was_drained_truncated = capture.output_truncated[1]
        stdout, stdout_was_truncated = _bounded_text(stdout_data, spec.max_output_bytes)
        stderr, stderr_was_truncated = _bounded_text(stderr_data, spec.max_output_bytes)
        return ProcessResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_was_drained_truncated or stdout_was_truncated,
            stderr_truncated=stderr_was_drained_truncated or stderr_was_truncated,
            timed_out=timed_out,
            pid=process.pid,
            identity=identity,
            stdout_bytes=stdout_data,
            stderr_bytes=stderr_data,
        )


class ProcessController:
    """Безопасная проверка и завершение ранее зарегистрированного процесса."""

    @staticmethod
    def inspect(identity: ProcessIdentity) -> bool:
        return identity.matches()

    @staticmethod
    def inspect_state(identity: ProcessIdentity) -> Literal["alive", "absent", "unknown"]:
        """Различить exact live process, доказанно отсутствующий PID и unknown.

        В отличие от boolean `inspect`, этот результат нельзя трактовать как
        разрешение на recovery при недоступных полях процесса.
        """

        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            return "absent"
        except (psutil.AccessDenied, OSError):
            return "unknown"
        try:
            if not process.is_running():
                return "absent"
            current_start = float(process.create_time())
            current_executable = _canonical(Path(process.exe()))
            current_argv = tuple(str(item) for item in process.cmdline())
            current_cwd = _canonical(Path(process.cwd()))
        except psutil.NoSuchProcess:
            return "absent"
        except (psutil.AccessDenied, OSError, ValueError):
            return "unknown"
        if (
            abs(current_start - identity.start_time) <= 0.05
            and _same_path(current_executable, identity.executable)
            and current_argv == identity.argv
            and _same_path(current_cwd, identity.cwd)
        ):
            return "alive"
        return "absent"

    @staticmethod
    def terminate(identity: ProcessIdentity, timeout_seconds: float = 15.0) -> bool:
        if not identity.matches():
            return False
        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied:
            return False
        try:
            descendants = tuple(process.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            descendants = ()
        if not _signal_process_group(identity.process_group, signal.SIGTERM):
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            for child in descendants:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            descendants_running = any(_is_process_running(child) for child in descendants)
            if not identity.matches() and not descendants_running:
                return True
            time.sleep(0.05)
        descendants_running = any(_is_process_running(child) for child in descendants)
        if not identity.matches() and not descendants_running:
            return True
        if not _signal_process_group(
            identity.process_group, _FORCE_TERMINATION_SIGNAL
        ):
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for child in descendants:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.1)
        return not identity.matches() and not any(
            _is_process_running(child) for child in descendants
        )


__all__ = [
    "DEFAULT_OUTPUT_LIMIT",
    "DEFAULT_PROCESS_TIMEOUT",
    "DOCKER_ENVIRONMENT_KEYS",
    "GRAFANA_URL_ENVIRONMENT_KEY",
    "INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS",
    "INTEGRATION_CREDENTIAL_FILE_ENVIRONMENT_KEYS",
    "MCP_LOCAL_SOURCE_DIGEST_ENVIRONMENT_KEYS",
    "MCP_LOCAL_TEST_ENVIRONMENT_PREFIX",
    "MCP_LOCAL_TOKEN_ENVIRONMENT_KEYS",
    "ProcessController",
    "ProcessIdentity",
    "ProcessResult",
    "ProcessSpec",
    "RunningProcess",
    "StructuredProcessRunner",
    "docker_environment",
    "public_argv",
    "safe_environment",
]
