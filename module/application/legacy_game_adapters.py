"""Legacy/runtime адаптеры для нейтральных game application services.

Импорт модуля намеренно не выполняет побочных действий. Legacy dependencies
загружаются только при запросе конкретной операции.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from module.application.game_models import (
    ConfigUpdateRequest,
    DashboardResources,
    MediaFrame,
    SchedulerEntry,
    thaw_payload,
)

_MAX_LOG_LINES = 10_000
_MAX_LOG_BYTES = 2 * 1024 * 1024
_SCHEDULER_FALLBACK_NEXT_RUN = datetime.fromisoformat("2050-01-01")
_INVALID_INSTANCE_CHARS = frozenset("./\\\x00:*?\"<>|")
_TASK_LOG_PATTERNS = (
    re.compile(r"调度器: 开始任务\s*[`'\" ](.*?)[`'\" ]"),
    re.compile(r"<<<\s*Run task\s*(.*?)\s*>>>")
)


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
        or any(char in _INVALID_INSTANCE_CHARS for char in value)
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
        or any(char in _INVALID_INSTANCE_CHARS for char in value)
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


class _GeneratedMetadata(Protocol):
    def read_dashboard_resources(
        self,
        config_data: Mapping[str, Any],
    ) -> DashboardResources: ...


class LegacyConfigAdapter:
    """Адаптер generated config и существующего AzurLaneConfig owner."""

    def __init__(
        self,
        metadata: _GeneratedMetadata,
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
        redactor = getattr(self._metadata, "redact_config", None)
        if callable(redactor):
            data = redactor(data)
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
        return tuple(sorted(entries, key=lambda entry: str(entry.next_run)))

    def update_config(self, request: ConfigUpdateRequest) -> None:
        config = self._make_config(request.instance)
        self._commit_changes(
            config,
            ((request.path, thaw_payload(request.value)),),
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
            self._find_log_file(instance)
        except FileNotFoundError:
            return "Unknown"

        for line in reversed(self.read_tail(instance, _MAX_LOG_LINES)):
            for pattern in _TASK_LOG_PATTERNS:
                match = pattern.search(line)
                if match:
                    candidate = match.group(1).strip(" `'\"")
                    if candidate:
                        return candidate
                    break
        return "Unknown"

    def _find_log_file(self, instance: str) -> Path:
        instance = _safe_instance_name(instance)
        current_date = self._date_provider()
        if not isinstance(current_date, date):
            raise TypeError("date_provider вернул не date")
        date_prefix = current_date.strftime("%Y-%m-%d")
        candidates = (
            self._safe_candidate(f"{date_prefix}_{instance}.txt"),
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
    """Получить кадр через существующий Device fallback и вернуть bytes."""

    def __init__(
        self,
        *,
        device_factory: Callable[[str], object] | None = None,
        frame_encoder: Callable[[object], bytes] | None = None,
    ) -> None:
        self._device_factory = device_factory
        self._frame_encoder = frame_encoder or self._encode_jpeg

    def read_frame(self, instance: str) -> MediaFrame:
        instance = _safe_instance_name(instance)
        device = self._make_device(instance)
        data = self._frame_encoder(device.screenshot())  # type: ignore[attr-defined]
        if not isinstance(data, bytes) or not data:
            raise TypeError("encoder вернул пустой кадр")
        return MediaFrame(data=data, media_type="image/jpeg")

    def _make_device(self, instance: str) -> object:
        if self._device_factory is not None:
            return self._device_factory(instance)
        try:
            from module.webui.fake_pil_module import remove_fake_pil_module

            remove_fake_pil_module()
        except ImportError:
            pass
        from module.config.config import AzurLaneConfig
        from module.device.device import Device

        return Device(AzurLaneConfig(instance))

    @staticmethod
    def _encode_jpeg(image: object) -> bytes:
        from PIL import Image

        try:
            import PIL.JpegImagePlugin  # noqa: F401 - регистрирует JPEG encoder.
        except ImportError:
            pass
        buffered = BytesIO()
        Image.fromarray(image).save(buffered, format="JPEG")
        return buffered.getvalue()


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
        if instance is not None:
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
        target_serial = (
            self._target_serial_provider(instance)
            if instance is not None
            else None
        )
        if target_serial is not None:
            target_serial = _safe_serial(target_serial)
        if not self._inventory_is_safe(devices, target_serial):
            return False
        target_present_before = (
            target_serial is not None and target_serial in devices
        )
        kill = self._runner((adb, "kill-server"))
        start = self._runner((adb, "start-server"))
        ready = self._runner((adb, "devices"))
        if not all(self._returncode(result) == 0 for result in (kill, start, ready)):
            return False
        ready_devices = self._parse_devices(ready)
        return (
            ready_devices is not None
            and self._inventory_is_safe(ready_devices, target_serial)
            and (not target_present_before or target_serial in ready_devices)
        )

    @staticmethod
    def _parse_devices(result: object) -> tuple[str, ...] | None:
        output = getattr(result, "stdout", None)
        if not isinstance(output, str):
            return None
        devices = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith(("List of devices attached", "*")):
                continue
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                devices.append(_safe_serial(fields[0]))
            except (TypeError, ValueError):
                return None
        return tuple(devices)

    @staticmethod
    def _inventory_is_safe(
        devices: Sequence[str],
        target_serial: str | None,
    ) -> bool:
        try:
            normalized = tuple(_safe_serial(serial) for serial in devices)
        except (TypeError, ValueError):
            return False
        if normalized != tuple(devices) or len(devices) != len(set(devices)):
            return False
        if target_serial is None:
            return not devices
        return all(serial == target_serial for serial in devices)

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
        try:
            from module.config.config_updater import ConfigUpdater

            data = ConfigUpdater().read_file(instance)
            alas = data.get("Alas", {}) if isinstance(data, Mapping) else {}
            emulator = alas.get("Emulator", {}) if isinstance(alas, Mapping) else {}
            serial = emulator.get("Serial") if isinstance(emulator, Mapping) else None
        except (AttributeError, OSError, TypeError, ValueError):
            return None
        if not isinstance(serial, str):
            return None
        serial = serial.strip()
        if not serial or serial.casefold() == "auto":
            return None
        return serial

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
        for candidate in (
            Path(".venv/Scripts/adb.exe"),
            Path(".venv/bin/adb"),
            Path("bin/adb/adb.exe"),
        ):
            if candidate.is_file():
                return str(candidate.resolve())
        return shutil.which("adb") or "adb"


__all__ = [
    "LegacyAdbAdapter",
    "LegacyConfigAdapter",
    "LegacyEmulatorAdapter",
    "LegacyProcessManagerAdapter",
    "LegacyRuntimeLogAdapter",
    "LegacyScreenshotAdapter",
    "legacy_current_time",
]
