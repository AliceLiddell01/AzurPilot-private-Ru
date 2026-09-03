"""Legacy/runtime адаптеры для нейтральных game application services.

Импорт модуля намеренно не выполняет побочных действий. Legacy dependencies
загружаются только при запросе конкретной операции.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import NamedTuple

from module.application.errors import (
    OperationFailedError,
    OwnershipAmbiguousError,
    PostconditionFailedError,
    PreconditionFailedError,
)
from module.application.game_models import (
    ConfigUpdateRequest,
    DashboardResources,
    GameApplicationState,
    MediaFrame,
    SchedulerEntry,
    thaw_payload,
)
from module.application.game_ports import GameConfigMetadata
from module.application.game_validation import (
    INVALID_NAME_CHARS,
    MAX_NAME_LENGTH,
    UNKNOWN_TASK,
)
from module.application.host_lock import (
    application_host_lock,
    ensure_host_runtime_root,
    host_scoped_lock_path,
)

_MAX_LOG_LINES = 10_000
_MAX_LOG_BYTES = 2 * 1024 * 1024
_PASSIVE_SCREENSHOT_TIMEOUT_SECONDS = 10
_PASSIVE_SCREENSHOT_MAX_BYTES = 4 * 1024 * 1024
_PASSIVE_EMULATOR_ALIASES_CACHE_TTL_SECONDS = 1.0
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_ADB_PATH_CANDIDATES = (
    Path(".venv/Scripts/adb.exe"),
    Path(".venv/bin/adb"),
    Path("bin/adb/adb.exe"),
    Path("bin/adb/adb"),
)
_SCHEDULER_FALLBACK_NEXT_RUN = datetime.fromisoformat("2050-01-01")
_ADB_DEVICE_STATES = frozenset({"device", "offline", "unauthorized"})
_GAME_PACKAGE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


def _adb_server_identity() -> str:
    """Определить identity фактического ADB server, а не checkout."""

    socket = os.environ.get("ADB_SERVER_SOCKET", "").strip()
    if socket:
        if socket.casefold().startswith("tcp:"):
            return socket
        return f"socket:{socket}"
    address = os.environ.get("ANDROID_ADB_SERVER_ADDRESS", "127.0.0.1").strip()
    port = os.environ.get("ANDROID_ADB_SERVER_PORT", "5037").strip()
    return f"tcp:{address or '127.0.0.1'}:{port or '5037'}"


def _adb_host_lock_path(server_identity: str | None = None) -> Path:
    """Вернуть stable user-runtime lock path для одного ADB endpoint."""

    return host_scoped_lock_path("adb", server_identity or _adb_server_identity())


_ADB_HOST_LOCK_PATH = _adb_host_lock_path()
_ADB_HOST_LOCK_TIMEOUT_SECONDS = 75.0
_ADB_RESTART_READY_TIMEOUT_SECONDS = 5.0
_ADB_RESTART_READY_RETRY_INTERVAL_SECONDS = 0.1
_ADB_RESTART_READY_MAX_ATTEMPTS = 60
_EMULATOR_STATE_TIMEOUT_SECONDS = 5.0
_EMULATOR_STATE_RETRY_INTERVAL_SECONDS = 0.1
_EMULATOR_STATE_MAX_ATTEMPTS = 60
_TASK_LOG_PATTERNS = (
    re.compile(r"调度器: 开始任务\s*[`'\" ](.*?)[`'\" ]"),
    re.compile(r"<<<\s*Run task\s*(.*?)\s*>>>")
)


def _adb_host_lock(server_identity: str | None = None):
    """Сериализовать Game ADB операции для одного server endpoint."""

    ensure_host_runtime_root()
    return application_host_lock(
        _adb_host_lock_path(server_identity),
        timeout=_ADB_HOST_LOCK_TIMEOUT_SECONDS,
    )


class _AdbDevice(NamedTuple):
    """Одна запись inventory ADB с serial и подтверждённым состоянием."""

    serial: str
    state: str


def _scheduler_sort_key(entry: SchedulerEntry) -> tuple[int, float, str]:
    """Упорядочить datetime по абсолютному времени, fallback — по тексту."""

    value = entry.next_run
    if isinstance(value, datetime):
        moment = (
            value.astimezone(UTC)
            if value.tzinfo is not None
            else value.replace(tzinfo=UTC)
        )
        return (0, moment.timestamp(), "")
    return (1, 0.0, str(value))


def legacy_current_time() -> datetime:
    """Получить проектное время только внутри legacy adapter boundary."""
    from module.config.time_source import now

    return now()


def _safe_instance_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("instance должен быть строкой")
    value = value.strip()
    if (
        not value
        or value in {".", ".."}
        or len(value) > MAX_NAME_LENGTH
        or any(char in INVALID_NAME_CHARS for char in value)
    ):
        raise ValueError("instance содержит недопустимое значение")
    return value


def _safe_segment(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("segment должен быть строкой")
    value = value.strip()
    if (
        not value
        or value in {".", ".."}
        or any(char in INVALID_NAME_CHARS for char in value)
        or len(value) > MAX_NAME_LENGTH
    ):
        raise ValueError("segment содержит недопустимое значение")
    return value


def _safe_serial(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("serial должен быть строкой")
    value = value.strip()
    if (
        not value
        or len(value) > 256
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("serial содержит недопустимое значение")
    return value


def _safe_adb_state(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("state должен быть строкой")
    value = value.strip()
    if (
        not value
        or len(value) > 64
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise ValueError("state содержит недопустимое значение")
    return value


def _read_target_serial(instance: str) -> str | None:
    """Прочитать profile-scoped serial без изменения конфигурации."""

    try:
        from module.config.config_updater import ConfigUpdater

        data = ConfigUpdater().read_file(instance)
    except (AttributeError, ImportError, OSError, TypeError, ValueError, KeyError):
        raise ValueError("Не удалось прочитать конфигурацию ADB.") from None
    if not isinstance(data, Mapping):
        raise TypeError("Конфигурация ADB имеет неверный формат.")
    alas = data.get("Alas", {})
    emulator = alas.get("Emulator", {}) if isinstance(alas, Mapping) else {}
    serial = emulator.get("Serial") if isinstance(emulator, Mapping) else None
    if not isinstance(serial, str):
        raise TypeError("В конфигурации ADB не задан serial.")
    serial = serial.strip()
    if not serial:
        raise ValueError("В конфигурации ADB не задан serial.")
    if serial.casefold() == "auto":
        return None
    return _safe_serial(serial)


def _parse_adb_inventory(output: str) -> tuple[_AdbDevice, ...] | None:
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices attached", "*")):
            continue
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            devices.append(
                _AdbDevice(
                    serial=_safe_serial(fields[0]),
                    state=_safe_adb_state(fields[1]),
                )
            )
        except (TypeError, ValueError):
            return None
    return tuple(devices)


def _bound_passive_screenshot(data: bytes) -> tuple[bytes, str]:
    """Сжать крупный PNG в памяти до bounded Game media contract."""

    if len(data) <= _PASSIVE_SCREENSHOT_MAX_BYTES:
        return data, "image/png"

    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            image.load()
            rgb = image.convert("RGB")
            for quality in (90, 80, 70, 60, 50):
                output = BytesIO()
                rgb.save(output, format="JPEG", quality=quality, optimize=True)
                candidate = output.getvalue()
                if len(candidate) <= _PASSIVE_SCREENSHOT_MAX_BYTES:
                    return candidate, "image/jpeg"
    except Exception:  # noqa: BLE001 - passive media boundary fails closed.
        raise OSError("Крупный framebuffer не прошёл bounded media contract.") from None

    raise OSError("Крупный framebuffer не удалось уложить в bounded media contract.")


def _first_existing_adb_path(*roots: Path) -> str | None:
    search_roots = (*roots, _REPOSITORY_ROOT)
    for base in search_roots:
        for candidate in _ADB_PATH_CANDIDATES:
            resolved = base / candidate
            if resolved.is_file():
                return str(resolved.resolve())
    discovered = shutil.which("adb")
    if discovered:
        return str(Path(discovered).resolve())
    return None


def _find_passive_adb_path() -> str:
    """Найти ADB без загрузки WebUI state и без его изменения."""

    discovered = _first_existing_adb_path()
    if discovered is not None:
        return discovered
    raise ValueError("Исполняемый файл ADB не найден.")


def _read_only_emulator_serial_aliases(target_serial: str) -> tuple[str, ...]:
    """Найти aliases настроенного инстанса без Device и lifecycle recovery."""

    try:
        from module.device.platform.emulator_windows import EmulatorManager
    except (ImportError, OSError):
        return ()

    try:
        instances = tuple(EmulatorManager().all_emulator_instances)
    except (AttributeError, OSError, TypeError, ValueError):
        return ()

    matches: list[tuple[str, ...]] = []
    for instance in instances:
        try:
            aliases = getattr(instance, "adb_serials", ())
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            continue
        normalized = tuple(
            alias for alias in aliases if isinstance(alias, str) and alias
        )
        if target_serial in normalized:
            matches.append(normalized)

    if len(matches) != 1:
        return ()
    return matches[0]


class LegacyConfigAdapter:
    """Адаптер generated config и существующего AzurLaneConfig owner."""

    def __init__(
        self,
        metadata: GameConfigMetadata,
        *,
        config_factory: Callable[[str], object] | None = None,
        updater_factory: Callable[[], object] | None = None,
    ) -> None:
        self._metadata = metadata
        self._config_factory = config_factory
        self._updater_factory = updater_factory

    def read_config(
        self,
        instance: str,
        task: str | None = None,
    ) -> Mapping[str, object]:
        instance = _safe_instance_name(instance)
        updater = self._make_updater()
        data = updater.read_file(instance)  # type: ignore[attr-defined]
        if not isinstance(data, Mapping):
            raise TypeError("config owner вернул не mapping")
        data = self._metadata.redact_config(data)
        if task is None:
            return data
        selected = data.get(_safe_segment(task), {})
        if not isinstance(selected, Mapping):
            return {}
        return selected

    def read_resources(self, instance: str) -> DashboardResources:
        data = self.read_config(instance)
        return self._metadata.read_dashboard_resources(data)

    def read_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: Sequence[str],
    ) -> tuple[SchedulerEntry, ...]:
        data = self.read_config(instance)
        entries = []
        for task in schedulable_tasks:
            task = _safe_segment(task)
            raw_task = data.get(task)
            if not isinstance(raw_task, Mapping):
                continue
            scheduler = raw_task.get("Scheduler")
            if not isinstance(scheduler, Mapping) or scheduler.get("Enable") is not True:
                continue
            next_run = scheduler.get("NextRun", _SCHEDULER_FALLBACK_NEXT_RUN)
            entries.append(SchedulerEntry(task=task, next_run=next_run))
        return tuple(sorted(entries, key=_scheduler_sort_key))

    def update_config(self, request: ConfigUpdateRequest) -> None:
        task = _safe_segment(request.task)
        group = _safe_segment(request.group)
        argument = _safe_segment(request.argument)
        metadata = self._metadata.read_argument_metadata(task, group, argument)
        if metadata is None:
            raise ValueError("metadata конфигурации не найдено")
        from module.config.utils import parse_value

        raw_value = thaw_payload(request.value)
        value = parse_value(raw_value, data=metadata)
        raw_type = metadata.get("type", "input")
        input_type = raw_type.casefold() if isinstance(raw_type, str) else "input"
        if (
            input_type != "datetime"
            and isinstance(raw_value, str)
            and (
                metadata.get("valuetype") == "str"
                or isinstance(metadata.get("value"), str)
            )
        ):
            value = raw_value
        config = self._make_config(request.instance)
        self._commit_changes(
            config,
            ((f"{task}.{group}.{argument}", value),),
        )

    def schedule_task(
        self,
        instance: str,
        task: str,
        scheduled_at: datetime,
    ) -> None:
        instance = _safe_instance_name(instance)
        task = _safe_segment(task)
        if not isinstance(scheduled_at, datetime):
            raise TypeError("scheduled_at должен быть datetime")
        config = self._make_config(instance)
        self._commit_changes(
            config,
            (
                (f"{task}.Scheduler.Enable", True),
                (f"{task}.Scheduler.NextRun", scheduled_at),
            ),
        )

    def clear_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: Sequence[str],
    ) -> tuple[str, ...]:
        instance = _safe_instance_name(instance)
        config = self._make_config(instance)
        data = getattr(config, "data", None)
        if not isinstance(data, Mapping):
            raise TypeError("config owner не предоставил data mapping")
        changes = []
        cleared = []
        for task in schedulable_tasks:
            task = _safe_segment(task)
            raw_task = data.get(task)
            scheduler = raw_task.get("Scheduler") if isinstance(raw_task, Mapping) else None
            if isinstance(scheduler, Mapping) and scheduler.get("Enable") is True:
                changes.append((f"{task}.Scheduler.Enable", False))
                cleared.append(task)
        if changes:
            self._commit_changes(config, tuple(changes))
        return tuple(cleared)

    def _make_updater(self) -> object:
        if self._updater_factory is not None:
            return self._updater_factory()
        from module.config.config_updater import ConfigUpdater

        return ConfigUpdater()

    def _make_config(self, instance: str) -> object:
        instance = _safe_instance_name(instance)
        if self._config_factory is not None:
            return self._config_factory(instance)
        from module.config.config import AzurLaneConfig

        return AzurLaneConfig(instance)

    @staticmethod
    def _commit_changes(
        config: object,
        changes: Sequence[tuple[str, object]],
    ) -> None:
        auto_update = getattr(config, "auto_update", True)
        try:
            config.auto_update = False
            for path, value in changes:
                config.cross_set(path, value)  # type: ignore[attr-defined]
            config.update()  # type: ignore[attr-defined]
        finally:
            config.auto_update = auto_update


class LegacyRuntimeLogAdapter:
    """Безопасный bounded reader файлов runtime-журнала."""

    def __init__(
        self,
        log_root: Path | str = Path("log"),
        *,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._log_root = Path(log_root)
        self._date_provider = date_provider or date.today

    def read_tail(self, instance: str, limit: int) -> tuple[str, ...]:
        if type(limit) is not int or not 0 <= limit <= _MAX_LOG_LINES:
            raise ValueError("limit вне допустимого диапазона")
        if limit == 0:
            return ()
        path = self._find_log_file(instance)
        return self._read_bounded_tail(path, limit)

    def read_current_task(self, instance: str) -> str:
        try:
            for window in (500, _MAX_LOG_LINES):
                lines = self.read_tail(instance, window)
                for line in reversed(lines):
                    for pattern in _TASK_LOG_PATTERNS:
                        match = pattern.search(line)
                        if match:
                            candidate = match.group(1).strip(" `'\"")
                            if candidate:
                                return candidate
                            break
                if len(lines) < window:
                    break
        except FileNotFoundError:
            return UNKNOWN_TASK
        return UNKNOWN_TASK

    def _find_log_file(self, instance: str) -> Path:
        instance = _safe_instance_name(instance)
        current_date = self._date_provider()
        if not isinstance(current_date, date):
            raise TypeError("date_provider вернул не date")
        date_prefix = current_date.strftime("%Y-%m-%d")
        previous_prefix = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
        candidates = (
            self._safe_candidate(f"{date_prefix}_{instance}.txt"),
            self._safe_candidate(f"{previous_prefix}_{instance}.txt"),
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise FileNotFoundError

    @staticmethod
    def _read_bounded_tail(path: Path, limit: int) -> tuple[str, ...]:
        """Прочитать tail с ограничением и по числу строк, и по объёму."""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            offset = max(0, file_size - _MAX_LOG_BYTES)
            handle.seek(offset)
            payload = handle.read(file_size - offset)
            previous = b"\n"
            if offset:
                handle.seek(offset - 1)
                previous = handle.read(1)
        if not isinstance(payload, bytes):
            raise TypeError("файл журнала вернул не bytes")
        text = payload.decode("utf-8", errors="ignore")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines(keepends=True)
        if offset and previous not in {b"\r", b"\n"} and lines:
            # Не выдавать обрезанный фрагмент строки как полноценную запись.
            lines = lines[1:]
        return tuple(lines[-limit:])

    def _safe_candidate(self, filename: str) -> Path:
        root = self._log_root
        if root.is_symlink() or (
            hasattr(root, "is_junction") and root.is_junction()
        ):
            raise ValueError("log root не должен быть ссылкой")
        resolved_root = root.resolve(strict=False)
        candidate = root / filename
        if candidate.is_symlink() or (
            hasattr(candidate, "is_junction") and candidate.is_junction()
        ):
            raise ValueError("файл журнала не должен быть ссылкой")
        resolved = candidate.resolve(strict=False)
        if resolved.parent != resolved_root:
            raise ValueError("файл журнала находится вне разрешённого root")
        return candidate


class LegacyScreenshotAdapter:
    """Пассивно прочитать framebuffer через прямой ADB read primitive."""

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str]], object] | None = None,
        adb_path_provider: Callable[[], str] | None = None,
        target_serial_provider: Callable[[str], str | None] | None = None,
        target_serial_aliases_provider: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._runner = runner or self._run
        self._adb_path_provider = adb_path_provider or _find_passive_adb_path
        self._target_serial_provider = target_serial_provider or _read_target_serial
        self._target_serial_aliases_provider = (
            target_serial_aliases_provider or _read_only_emulator_serial_aliases
        )
        self._target_serial_aliases_cache: dict[
            str, tuple[float, tuple[object, ...]]
        ] = {}
        self._target_serial_aliases_cache_lock = Lock()

    def read_frame(self, instance: str) -> MediaFrame:
        instance = _safe_instance_name(instance)
        with _adb_host_lock():
            return self._read_frame(instance)

    def _read_frame(self, instance: str) -> MediaFrame:
        adb = self._adb_path_provider()
        if not isinstance(adb, str) or not adb:
            raise ValueError("ADB path не определён.")
        serial = self._target_serial_provider(instance)
        if isinstance(serial, str) and serial.casefold() == "auto":
            serial = None
        if serial is not None:
            serial = _safe_serial(serial)
        devices = self._read_inventory(adb)
        serial = self._resolve_target_serial(serial, devices)

        result = self._runner(
            (adb, "-s", serial, "exec-out", "screencap", "-p")
        )
        returncode = getattr(result, "returncode", None)
        if type(returncode) is not int:
            raise TypeError("ADB runner вернул объект без returncode.")
        if returncode != 0:
            raise OSError("ADB не вернул framebuffer.")
        data = getattr(result, "stdout", None)
        if not isinstance(data, bytes) or not data:
            raise OSError("Безопасный framebuffer недоступен.")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise OSError("ADB вернул данные без корректной PNG-сигнатуры.")
        data, media_type = _bound_passive_screenshot(data)
        return MediaFrame(data=data, media_type=media_type)

    def _read_inventory(self, adb: str) -> tuple[_AdbDevice, ...]:
        inventory = self._runner((adb, "devices"))
        returncode = getattr(inventory, "returncode", None)
        if type(returncode) is not int:
            raise TypeError("ADB runner вернул объект без returncode.")
        if returncode != 0:
            raise OSError("Инвентарь ADB недоступен.")
        output = getattr(inventory, "stdout", None)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="strict")
        if not isinstance(output, str):
            raise TypeError("ADB inventory имеет неверный формат.")
        devices = _parse_adb_inventory(output)
        if devices is None:
            raise OSError("Инвентарь ADB имеет неверный формат.")
        return devices

    @staticmethod
    def _resolve_single_device(devices: Sequence[_AdbDevice]) -> str:
        if len(devices) != 1 or devices[0].state != "device":
            raise OSError("Единственный готовый ADB target не подтверждён.")
        return devices[0].serial

    def _resolve_target_serial(
        self,
        target_serial: str | None,
        devices: Sequence[_AdbDevice],
    ) -> str:
        if target_serial is None:
            return self._resolve_single_device(devices)

        ready_serials = tuple(
            device.serial for device in devices if device.state == "device"
        )
        if target_serial in ready_serials:
            return target_serial

        aliases = self._read_target_serial_aliases(target_serial)
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            raise TypeError("Resolver ADB aliases вернул неверный формат.")
        safe_aliases: set[str] = set()
        for alias in aliases:
            try:
                safe_aliases.add(_safe_serial(alias))
            except (TypeError, ValueError):
                continue
        matches = tuple(serial for serial in ready_serials if serial in safe_aliases)
        if len(matches) != 1:
            raise OSError("Настроенный ADB target не подтверждён.")
        return matches[0]

    def _read_target_serial_aliases(self, target_serial: str) -> object:
        now = monotonic()
        with self._target_serial_aliases_cache_lock:
            cached = self._target_serial_aliases_cache.get(target_serial)
            if (
                cached is not None
                and now - cached[0] < _PASSIVE_EMULATOR_ALIASES_CACHE_TTL_SECONDS
            ):
                return cached[1]

        aliases = self._target_serial_aliases_provider(target_serial)
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            return aliases

        snapshot = tuple(aliases)
        with self._target_serial_aliases_cache_lock:
            self._target_serial_aliases_cache[target_serial] = (
                monotonic(),
                snapshot,
            )
        return snapshot

    @staticmethod
    def _run(argv: Sequence[str]) -> object:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            timeout=_PASSIVE_SCREENSHOT_TIMEOUT_SECONDS,
        )


class LegacyGameApplicationAdapter:
    """Запустить настроенную игру через существующий ADB application boundary."""

    def __init__(
        self,
        *,
        config_factory: Callable[[str], object] | None = None,
        adb_client_factory: Callable[[], object] | None = None,
        app_control_factory: Callable[[object, object, str, str, object], object]
        | None = None,
        target_serial_provider: Callable[[str], str | None] | None = None,
        target_serial_aliases_provider: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._adb_client_factory = adb_client_factory or self._default_adb_client
        self._app_control_factory = (
            app_control_factory or self._default_app_control_factory
        )
        self._target_serial_provider = target_serial_provider or _read_target_serial
        self._target_serial_aliases_provider = (
            target_serial_aliases_provider or _read_only_emulator_serial_aliases
        )

    def read_state(self, instance: str) -> GameApplicationState:
        instance = _safe_instance_name(instance)
        with _adb_host_lock():
            return self._read_state(instance)

    def start_game(self, instance: str) -> bool:
        instance = _safe_instance_name(instance)
        with _adb_host_lock():
            app = self._make_app_control(instance)
            method = getattr(app, "app_start_adb", None)
            if not callable(method):
                raise OperationFailedError(
                    "Application owner не предоставил запуск настроенной игры."
                )
            try:
                result = method()
            except (OwnershipAmbiguousError, PreconditionFailedError):
                raise
            except Exception:  # noqa: BLE001 - application boundary is sanitized.
                raise OperationFailedError(
                    "Не удалось запустить настроенную игру."
                ) from None
            if type(result) is not bool:
                raise OperationFailedError(
                    "Application owner вернул некорректный результат запуска игры."
                )
            return result

    def _read_state(self, instance: str) -> GameApplicationState:
        app, package = self._make_app_control(instance, include_package=True)
        foreground: bool | None
        try:
            current = app.app_current_adb()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - foreground read fails closed.
            foreground = None
        else:
            foreground = (
                current.strip() == package if isinstance(current, str) else None
            )

        running: bool | None
        shell = getattr(app, "adb_shell", None)
        if not callable(shell):
            running = None
        else:
            try:
                output = shell(["pidof", package], timeout=5)
            except Exception:  # noqa: BLE001 - process read is best effort.
                running = None
            else:
                running = bool(re.search(r"\b\d+\b", str(output)))
        if foreground is True:
            running = True
        return GameApplicationState(
            adb_ready=True,
            game_running=running,
            game_foreground=foreground,
        )

    def _make_app_control(
        self,
        instance: str,
        *,
        include_package: bool = False,
    ) -> object | tuple[object, str]:
        config = self._make_config(instance)
        package = self._read_package(config)
        client = self._make_adb_client()
        device, serial = self._resolve_target_device(instance, client)
        app = self._app_control_factory(config, device, serial, package, client)
        if include_package:
            return app, package
        return app

    def _make_config(self, instance: str) -> object:
        if self._config_factory is not None:
            return self._config_factory(instance)
        from module.config.config import AzurLaneConfig

        return AzurLaneConfig(instance, task=None)

    @staticmethod
    def _read_package(config: object) -> str:
        package = getattr(config, "Emulator_PackageName", None)
        if (
            not isinstance(package, str)
            or package != package.strip()
            or len(package) > 256
            or _GAME_PACKAGE_RE.fullmatch(package) is None
        ):
            raise PreconditionFailedError(
                "В конфигурации не задан валидный пакет настроенной игры."
            )
        return package

    def _make_adb_client(self) -> object:
        try:
            return self._adb_client_factory()
        except (OwnershipAmbiguousError, PreconditionFailedError):
            raise
        except Exception:  # noqa: BLE001 - ADB setup fails closed.
            raise PreconditionFailedError("ADB server недоступен.") from None

    def _resolve_target_device(
        self,
        instance: str,
        client: object,
    ) -> tuple[object, str]:
        list_devices = getattr(client, "device_list", None)
        if not callable(list_devices):
            raise PreconditionFailedError("ADB client не предоставляет inventory target.")
        try:
            devices = list(list_devices())
        except Exception:  # noqa: BLE001 - ADB inventory fails closed.
            raise PreconditionFailedError("ADB inventory target недоступен.") from None
        if not devices:
            raise OwnershipAmbiguousError("Готовый ADB target не найден.")

        records: list[tuple[object, str, str]] = []
        seen_serials: set[str] = set()
        for device in devices:
            try:
                serial = _safe_serial(device.serial)  # type: ignore[attr-defined]
            except (TypeError, ValueError):
                raise OwnershipAmbiguousError(
                    "ADB inventory содержит неподтверждённый serial."
                ) from None
            if serial in seen_serials:
                raise OwnershipAmbiguousError(
                    "ADB inventory содержит повторный serial."
                )
            seen_serials.add(serial)
            state = self._read_device_state(device)
            records.append((device, serial, state))

        try:
            target_serial = self._target_serial_provider(instance)
        except (OwnershipAmbiguousError, PreconditionFailedError):
            raise
        except Exception:  # noqa: BLE001 - target config fails closed.
            raise PreconditionFailedError(
                "Конфигурация ADB target недоступна."
            ) from None
        if isinstance(target_serial, str) and target_serial.casefold() == "auto":
            target_serial = None
        if target_serial is not None:
            try:
                target_serial = _safe_serial(target_serial)
            except (TypeError, ValueError):
                raise PreconditionFailedError(
                    "Конфигурация ADB target имеет некорректный serial."
                ) from None

        ready = [record for record in records if record[2] == "device"]
        if target_serial is None:
            if len(records) != 1 or len(ready) != 1:
                raise OwnershipAmbiguousError(
                    "Ownership ADB target неоднозначен без configured serial."
                )
            _device, serial, _state = ready[0]
            return _device, serial

        exact = [record for record in ready if record[1] == target_serial]
        if len(exact) == 1:
            device, serial, _state = exact[0]
            return device, serial

        try:
            aliases = self._target_serial_aliases_provider(target_serial)
        except Exception:  # noqa: BLE001 - aliases are not proof when unavailable.
            raise OwnershipAmbiguousError(
                "Ownership ADB target нельзя подтвердить по aliases."
            ) from None
        if isinstance(aliases, (str, bytes)) or not isinstance(aliases, Sequence):
            raise OwnershipAmbiguousError(
                "Resolver ADB aliases вернул неподтверждённый результат."
            )
        safe_aliases: set[str] = set()
        for alias in aliases:
            try:
                safe_aliases.add(_safe_serial(alias))
            except (TypeError, ValueError):
                continue
        matches = [record for record in ready if record[1] in safe_aliases]
        if len(matches) != 1:
            raise OwnershipAmbiguousError(
                "Ownership configured ADB target не подтверждён."
            )
        device, serial, _state = matches[0]
        return device, serial

    @staticmethod
    def _read_device_state(device: object) -> str:
        checker = getattr(device, "get_state", None)
        if not callable(checker):
            raise OwnershipAmbiguousError("ADB target не предоставляет state check.")
        try:
            state = checker()
        except Exception:  # noqa: BLE001 - target state fails closed.
            raise PreconditionFailedError("Состояние ADB target недоступно.") from None
        try:
            return _safe_adb_state(state)
        except (TypeError, ValueError):
            raise OwnershipAmbiguousError("ADB target вернул некорректное состояние.") from None

    @staticmethod
    def _default_adb_client() -> object:
        import adbutils

        port = 5037
        raw_port = os.environ.get("ANDROID_ADB_SERVER_PORT")
        if raw_port is not None:
            try:
                port = int(raw_port)
            except ValueError:
                raise PreconditionFailedError(
                    "Порт ADB имеет некорректный формат."
                ) from None
        if not 1 <= port <= 65_535:
            raise PreconditionFailedError("Порт ADB вне допустимого диапазона.")
        return adbutils.AdbClient("127.0.0.1", port)

    @staticmethod
    def _default_app_control_factory(
        config: object,
        device: object,
        serial: str,
        package: str,
        client: object,
    ) -> object:
        from module.device.app_control import AppControl

        class LegacyGameAppControl(AppControl):
            def __init__(self) -> None:
                self.config = config
                self.adb = device
                self.adb_client = client
                self.serial = serial
                self.package = package
                self.is_wsa = False
                self.is_local_network_device = False
                self.is_waydroid = False

            def adb_shell(self, cmd: object, **kwargs: object) -> str:
                timeout = kwargs.get("timeout", 10)
                return str(self.adb.shell(cmd, timeout=timeout))

        return LegacyGameAppControl()


class LegacyProcessManagerAdapter:
    """Узкий adapter к WebUI-owned ProcessManager."""

    def __init__(
        self,
        *,
        manager_factory: Callable[[str], object] | None = None,
        function_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._manager_factory = manager_factory
        self._function_factory = function_factory

    def is_running(self, instance: str) -> bool:
        manager = self._manager(instance)
        value = manager.alive
        if type(value) is not bool:
            raise TypeError("ProcessManager.alive должен быть bool")
        return value

    def start_instance(self, instance: str) -> bool:
        instance = _safe_instance_name(instance)
        manager = self._manager(instance)
        function = self._function_factory(instance) if self._function_factory else self._default_function(instance)
        manager.start(func=function)  # type: ignore[attr-defined]
        return self.is_running(instance)

    def stop_instance(self, instance: str) -> bool:
        instance = _safe_instance_name(instance)
        manager = self._manager(instance)
        stopped = manager.stop()  # type: ignore[attr-defined]
        if type(stopped) is not bool:
            raise TypeError("ProcessManager.stop должен вернуть bool")
        return stopped and not self.is_running(instance)

    def _manager(self, instance: str) -> object:
        instance = _safe_instance_name(instance)
        if self._manager_factory is not None:
            return self._manager_factory(instance)
        from module.webui.process_manager import ProcessManager

        return ProcessManager.get_manager(instance)

    @staticmethod
    def _default_function(instance: str) -> str:
        from module.submodule.utils import get_config_mod

        return get_config_mod(instance)


class LegacyEmulatorAdapter:
    """Выполняет instance-scoped restart через проверяемый Platform owner."""

    def __init__(
        self,
        *,
        platform_factory: Callable[[str], object] | None = None,
        typed_failures: bool = False,
    ) -> None:
        self._platform_factory = platform_factory
        if type(typed_failures) is not bool:
            raise TypeError("typed_failures должен быть bool")
        self._typed_failures = typed_failures

    def restart_emulator(self, instance: str) -> bool:
        instance = _safe_instance_name(instance)
        with _adb_host_lock():
            return self._restart_emulator(instance)

    def _restart_emulator(self, instance: str) -> bool:
        platform = self._make_platform(instance)
        is_running = getattr(platform, "is_emulator_instance_running", None)
        if not callable(is_running) or self._read_running(is_running) is None:
            return self._failure(
                OwnershipAmbiguousError(
                    "Ownership emulator instance не подтверждён."
                )
            )

        try:
            self._call_platform(
                platform,
                "emulator_stop",
                "остановку эмулятора",
            )
        except OperationFailedError:
            # Решение о внутренней эскалации принимает свежая проверка
            # instance state, а не exit code или исключение manager API.
            pass

        stop_state = self._state_after_wait(is_running, expected=False)
        if stop_state is None:
            return self._failure(
                OwnershipAmbiguousError(
                    "Ownership emulator instance нельзя подтвердить после stop."
                )
            )

        if stop_state:
            # Это одна lifecycle transition: штатная остановка уже завершилась
            # неуспешно, поэтому перед instance-scoped escalation требуется
            # отдельная актуальная проверка exact ownership.
            if self._read_running(is_running) is not True:
                stop_state = self._state_after_wait(is_running, expected=False)
                if stop_state is None:
                    return self._failure(
                        OwnershipAmbiguousError(
                            "Ownership emulator instance нельзя подтвердить перед escalation."
                        )
                    )
            if stop_state:
                force_error: OperationFailedError | None = None
                try:
                    self._call_platform(
                        platform,
                        "emulator_force_stop_instance",
                        "instance-scoped завершение эмулятора",
                    )
                except OperationFailedError as error:
                    # Даже при ошибке команды authoritative state может уже
                    # быть stopped; ниже решение принимается только по нему.
                    force_error = error

                force_state = self._state_after_wait(is_running, expected=False)
                if force_state is None:
                    return self._failure(
                        OwnershipAmbiguousError(
                            "Ownership emulator instance нельзя подтвердить после escalation."
                        )
                    )
                if force_state:
                    return self._failure(
                        force_error
                        or PostconditionFailedError(
                            "Эмулятор не подтвердил состояние stopped после escalation."
                        )
                    )

        started = self._call_platform(
            platform,
            "emulator_start",
            "запуск эмулятора",
        )
        if started is not True:
            return self._failure(
                OperationFailedError("Эмулятор не подтвердил запуск после stop.")
            )
        if not self._await_running(is_running, True):
            return self._failure(
                PostconditionFailedError(
                    "Эмулятор не подтвердил состояние running после start."
                )
            )
        return True

    @classmethod
    def _state_after_wait(
        cls,
        checker: Callable[[], object],
        *,
        expected: bool,
    ) -> bool | None:
        if cls._await_running(checker, expected):
            return expected
        return cls._read_running(checker)

    def _failure(self, error: OperationFailedError) -> bool:
        if self._typed_failures:
            raise error
        return False

    @staticmethod
    def _read_running(checker: Callable[[], object]) -> bool | None:
        try:
            value = checker()
        except Exception:  # noqa: BLE001 - lifecycle confirmation fails closed.
            return None
        return value if type(value) is bool else None

    @staticmethod
    def _await_running(checker: Callable[[], object], expected: bool) -> bool:
        deadline = monotonic() + _EMULATOR_STATE_TIMEOUT_SECONDS
        for attempt in range(_EMULATOR_STATE_MAX_ATTEMPTS):
            if LegacyEmulatorAdapter._read_running(checker) is expected:
                return True
            if (
                attempt + 1 >= _EMULATOR_STATE_MAX_ATTEMPTS
                or monotonic() >= deadline
            ):
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(_EMULATOR_STATE_RETRY_INTERVAL_SECONDS, remaining))
        return False

    def _call_platform(
        self,
        platform: object,
        method_name: str,
        operation: str,
    ) -> object:
        try:
            method = getattr(platform, method_name, None)
            if not callable(method):
                return self._failure(
                    OperationFailedError(
                        f"Platform не предоставила операцию: {operation}."
                    )
                )
            return method()
        except Exception:  # noqa: BLE001 - platform boundary is sanitized.
            return self._failure(
                OperationFailedError(f"Не удалось выполнить {operation}.")
            )

    def _make_platform(self, instance: str) -> object:
        if self._platform_factory is not None:
            return self._platform_factory(instance)
        from module.config.config import AzurLaneConfig
        from module.device.platform import get_recovery_platform

        return get_recovery_platform(AzurLaneConfig(instance, task=None))


class LegacyAdbAdapter:
    """Перезапуск ADB с проверкой команд и без угадывания profile target."""

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str]], object] | None = None,
        adb_path_provider: Callable[[], str] | None = None,
        target_serial_provider: Callable[[str], str | None] | None = None,
        typed_failures: bool = False,
    ) -> None:
        self._runner = runner or self._run
        self._adb_path_provider = adb_path_provider or self._default_adb_path
        self._target_serial_provider = target_serial_provider or self._default_target_serial
        if type(typed_failures) is not bool:
            raise TypeError("typed_failures должен быть bool")
        self._typed_failures = typed_failures

    def restart_adb(self, instance: str | None = None) -> bool:
        """Сериализовать host-global kill/start между всеми Game owners."""

        with _adb_host_lock():
            return self._restart_adb(instance)

    def _restart_adb(self, instance: str | None = None) -> bool:
        # `adb kill-server` является host-global операцией. Без доказанной
        # принадлежности конкретному экземпляру она может оборвать чужие
        # устройства и потому запрещена до любого обращения к ADB.
        if instance is None:
            return False
        instance = _safe_instance_name(instance)
        adb = self._adb_path_provider()
        if not isinstance(adb, str) or not adb:
            raise ValueError("ADB path не определён")
        inventory = self._runner((adb, "devices"))
        if self._returncode(inventory) != 0:
            return self._failure(
                OwnershipAmbiguousError("Свежий инвентарь ADB недоступен.")
            )
        devices = self._parse_devices(inventory)
        if devices is None:
            return self._failure(
                OwnershipAmbiguousError("Инвентарь ADB не подтверждает ownership target.")
            )
        target_serial = self._target_serial_provider(instance)
        if target_serial is not None:
            target_serial = _safe_serial(target_serial)
        auto_target = target_serial is None
        if not self._inventory_is_safe(
            devices,
            target_serial,
            allow_singleton=auto_target,
        ):
            return self._failure(
                OwnershipAmbiguousError("Ownership ADB target не подтверждён.")
            )
        if auto_target:
            target_serial = devices[0].serial
        kill = self._runner((adb, "kill-server"))
        start = self._runner((adb, "start-server"))
        if not all(self._returncode(result) == 0 for result in (kill, start)):
            return self._failure(
                OperationFailedError("ADB не подтвердил выполнение restart.")
            )
        deadline = monotonic() + _ADB_RESTART_READY_TIMEOUT_SECONDS
        for attempt in range(_ADB_RESTART_READY_MAX_ATTEMPTS):
            ready = self._runner((adb, "devices"))
            if self._returncode(ready) != 0:
                return self._failure(
                    OperationFailedError("ADB не подтвердил выполнение restart.")
                )
            ready_devices = self._parse_devices(ready)
            if (
                ready_devices is not None
                and self._inventory_is_safe(
                    ready_devices,
                    target_serial,
                )
                and self._target_is_ready(ready_devices, target_serial)
            ):
                return True
            if (
                attempt + 1 >= _ADB_RESTART_READY_MAX_ATTEMPTS
                or monotonic() >= deadline
            ):
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(_ADB_RESTART_READY_RETRY_INTERVAL_SECONDS, remaining))
        return self._failure(
            PostconditionFailedError("ADB target не подтверждён после restart.")
        )

    def _failure(self, error: OperationFailedError) -> bool:
        if self._typed_failures:
            raise error
        return False

    @staticmethod
    def _parse_devices(result: object) -> tuple[_AdbDevice, ...] | None:
        output = getattr(result, "stdout", None)
        if not isinstance(output, str):
            return None
        return _parse_adb_inventory(output)

    @staticmethod
    def _inventory_is_safe(
        devices: Sequence[_AdbDevice],
        target_serial: str | None,
        *,
        allow_singleton: bool = False,
    ) -> bool:
        try:
            normalized = tuple(
                _AdbDevice(
                    serial=_safe_serial(device.serial),
                    state=_safe_adb_state(device.state),
                )
                for device in devices
            )
        except (TypeError, ValueError):
            return False
        if normalized != tuple(devices) or len(devices) != len(set(devices)):
            return False
        if not devices or any(device.state not in _ADB_DEVICE_STATES for device in devices):
            return False
        serials = tuple(device.serial for device in devices)
        if len(serials) != len(set(serials)):
            return False
        if target_serial is None:
            return allow_singleton and len(devices) == 1
        return len(devices) == 1 and serials[0] == target_serial

    @staticmethod
    def _target_is_ready(
        devices: Sequence[_AdbDevice],
        target_serial: str | None,
    ) -> bool:
        return (
            target_serial is not None
            and len(devices) == 1
            and devices[0].serial == target_serial
            and devices[0].state == "device"
        )

    @staticmethod
    def _returncode(result: object) -> int:
        value = getattr(result, "returncode", None)
        if type(value) is not int:
            raise TypeError("ADB runner вернул объект без returncode")
        return value

    @staticmethod
    def _run(argv: Sequence[str]) -> object:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            timeout=30,
            text=True,
        )

    @staticmethod
    def _default_target_serial(instance: str) -> str | None:
        return _read_target_serial(instance)

    @staticmethod
    def _default_adb_path() -> str:
        configured_root: Path | None = None
        try:
            from module.webui.setting import State

            configured = State.deploy_config.AdbExecutable
            configured_root = Path(State.deploy_config.root_filepath).resolve()
            if not configured_root.is_dir():
                configured_root = None
            if configured:
                candidate = (configured_root or _REPOSITORY_ROOT) / configured
                if candidate.is_file():
                    return str(candidate.resolve())
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            pass
        roots = (configured_root,) if configured_root is not None else ()
        discovered = _first_existing_adb_path(*roots)
        if discovered is not None:
            return discovered
        raise ValueError("Исполняемый файл ADB не найден.")


__all__ = [
    "LegacyAdbAdapter",
    "LegacyConfigAdapter",
    "LegacyEmulatorAdapter",
    "LegacyGameApplicationAdapter",
    "LegacyProcessManagerAdapter",
    "LegacyRuntimeLogAdapter",
    "LegacyScreenshotAdapter",
    "legacy_current_time",
]
