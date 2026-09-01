"""Legacy/runtime адаптеры для нейтральных game application services.

Импорт модуля намеренно не выполняет побочных действий. Legacy dependencies
загружаются только при запросе конкретной операции.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from module.application.game_models import (
    ConfigUpdateRequest,
    DashboardResources,
    MediaFrame,
    SchedulerEntry,
    thaw_payload,
)
from module.application.game_ports import GameConfigMetadata
from module.application.game_validation import INVALID_NAME_CHARS, UNKNOWN_TASK

_MAX_LOG_LINES = 10_000
_MAX_LOG_BYTES = 2 * 1024 * 1024
_PASSIVE_SCREENSHOT_TIMEOUT_SECONDS = 10
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_ADB_PATH_CANDIDATES = (
    Path(".venv/Scripts/adb.exe"),
    Path(".venv/bin/adb"),
    Path("bin/adb/adb.exe"),
    Path("bin/adb/adb"),
)
_SCHEDULER_FALLBACK_NEXT_RUN = datetime.fromisoformat("2050-01-01")
_ADB_DEVICE_STATES = frozenset({"device", "offline", "unauthorized"})
_TASK_LOG_PATTERNS = (
    re.compile(r"调度器: 开始任务\s*[`'\" ](.*?)[`'\" ]"),
    re.compile(r"<<<\s*Run task\s*(.*?)\s*>>>")
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
        or len(value) > 128
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
        or len(value) > 128
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


def _first_existing_adb_path() -> str | None:
    for candidate in _ADB_PATH_CANDIDATES:
        for base in (Path.cwd(), _REPOSITORY_ROOT):
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
    ) -> None:
        self._runner = runner or self._run
        self._adb_path_provider = adb_path_provider or _find_passive_adb_path
        self._target_serial_provider = target_serial_provider or _read_target_serial

    def read_frame(self, instance: str) -> MediaFrame:
        instance = _safe_instance_name(instance)
        adb = self._adb_path_provider()
        if not isinstance(adb, str) or not adb:
            raise ValueError("ADB path не определён.")
        serial = self._target_serial_provider(instance)
        if isinstance(serial, str) and serial.casefold() == "auto":
            serial = None
        if serial is not None:
            serial = _safe_serial(serial)
        else:
            serial = self._resolve_single_device(adb)

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
        return MediaFrame(data=data, media_type="image/png")

    def _resolve_single_device(self, adb: str) -> str:
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
        if devices is None or len(devices) != 1 or devices[0].state != "device":
            raise OSError("Единственный готовый ADB target не подтверждён.")
        return devices[0].serial

    @staticmethod
    def _run(argv: Sequence[str]) -> object:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            timeout=_PASSIVE_SCREENSHOT_TIMEOUT_SECONDS,
        )


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
    """Выполняет instance-scoped restart через существующий Platform owner."""

    def __init__(
        self,
        *,
        platform_factory: Callable[[str], object] | None = None,
    ) -> None:
        self._platform_factory = platform_factory

    def restart_emulator(self, instance: str) -> bool:
        instance = _safe_instance_name(instance)
        platform = self._make_platform(instance)
        stopped = platform.emulator_stop()  # type: ignore[attr-defined]
        if stopped is not True:
            return False
        started = platform.emulator_start()  # type: ignore[attr-defined]
        return started is True

    def _make_platform(self, instance: str) -> object:
        if self._platform_factory is not None:
            return self._platform_factory(instance)
        from module.config.config import AzurLaneConfig
        from module.device.platform import Platform

        return Platform(AzurLaneConfig(instance, task=None), connect=False)


class LegacyAdbAdapter:
    """Перезапуск ADB с проверкой команд и без угадывания profile target."""

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str]], object] | None = None,
        adb_path_provider: Callable[[], str] | None = None,
        target_serial_provider: Callable[[str], str | None] | None = None,
    ) -> None:
        self._runner = runner or self._run
        self._adb_path_provider = adb_path_provider or self._default_adb_path
        self._target_serial_provider = target_serial_provider or self._default_target_serial

    def restart_adb(self, instance: str | None = None) -> bool:
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
            return False
        devices = self._parse_devices(inventory)
        if devices is None:
            return False
        target_serial = self._target_serial_provider(instance)
        if target_serial is not None:
            target_serial = _safe_serial(target_serial)
        auto_target = target_serial is None
        if not self._inventory_is_safe(
            devices,
            target_serial,
            allow_singleton=auto_target,
        ):
            return False
        if auto_target:
            target_serial = devices[0].serial
        kill = self._runner((adb, "kill-server"))
        start = self._runner((adb, "start-server"))
        ready = self._runner((adb, "devices"))
        if not all(self._returncode(result) == 0 for result in (kill, start, ready)):
            return False
        ready_devices = self._parse_devices(ready)
        return (
            ready_devices is not None
            and self._inventory_is_safe(
                ready_devices,
                target_serial,
            )
            and self._target_is_ready(ready_devices, target_serial)
        )

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
        try:
            from module.webui.setting import State

            configured = State.deploy_config.AdbExecutable
            root = State.deploy_config.root_filepath
            if configured:
                candidate = Path(root) / configured
                if candidate.is_file():
                    return str(candidate.resolve())
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        discovered = _first_existing_adb_path()
        if discovered is not None:
            return discovered
        raise ValueError("Исполняемый файл ADB не найден.")


__all__ = [
    "LegacyAdbAdapter",
    "LegacyConfigAdapter",
    "LegacyEmulatorAdapter",
    "LegacyProcessManagerAdapter",
    "LegacyRuntimeLogAdapter",
    "LegacyScreenshotAdapter",
    "legacy_current_time",
]
