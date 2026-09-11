"""Сервисы чтения нейтральной application boundary game/runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from module.application.errors import InvalidRequestError, ServiceUnavailableError
from module.application.game_models import (
    ConfigSnapshot,
    CurrentTaskSnapshot,
    DashboardResources,
    MediaFrame,
    RuntimeLogTail,
    SchedulerEntry,
    SchedulerQueueSnapshot,
)
from module.application.game_ports import (
    GameConfigReader,
    RuntimeExecutionStateReader,
    RuntimeLogReader,
    SchedulerTaskReader,
    ScreenshotReader,
)
from module.application.game_validation import (
    MAX_RECENT_LOG_LINES,
    known_instance,
    safe_read,
    scheduler_tasks,
    validated_segment,
)
from module.application.ports import InstanceRuntimeReader


class GameReadService:
    """Read API игры поверх узких runtime/config/device ports."""

    def __init__(
        self,
        instance_reader: InstanceRuntimeReader,
        config_reader: GameConfigReader,
        log_reader: RuntimeLogReader,
        screenshot_reader: ScreenshotReader,
        scheduler_tasks: SchedulerTaskReader,
        runtime_execution_reader: RuntimeExecutionStateReader | None = None,
    ) -> None:
        self._instance_reader = instance_reader
        self._config_reader = config_reader
        self._log_reader = log_reader
        self._screenshot_reader = screenshot_reader
        self._scheduler_tasks = scheduler_tasks
        self._runtime_execution_reader = runtime_execution_reader

    def get_resources(self, instance: str) -> DashboardResources:
        instance = known_instance(self._instance_reader, instance)
        result = safe_read(
            "ресурсов dashboard",
            lambda: self._config_reader.read_resources(instance),
        )
        if not isinstance(result, DashboardResources):
            raise ServiceUnavailableError("Адаптер вернул некорректные ресурсы dashboard.")
        return result

    def get_config(self, instance: str, task: str | None = None) -> ConfigSnapshot:
        instance = known_instance(self._instance_reader, instance)
        if task is not None:
            task = validated_segment(task, resource="задачи")
        result = safe_read(
            "снимка конфигурации",
            lambda: self._config_reader.read_config(instance, task),
        )
        if not isinstance(result, Mapping):
            raise ServiceUnavailableError("Адаптер вернул некорректную конфигурацию.")
        try:
            return ConfigSnapshot(instance=instance, task=task, data=result)
        except (TypeError, ValueError):
            raise ServiceUnavailableError(
                "Адаптер вернул конфигурацию неподдерживаемой структуры."
            ) from None

    def get_recent_logs(self, instance: str, limit: int = 50) -> RuntimeLogTail:
        instance = known_instance(self._instance_reader, instance)
        if type(limit) is not int or not 0 <= limit <= MAX_RECENT_LOG_LINES:
            raise InvalidRequestError(
                f"Количество строк должно быть целым числом от 0 до {MAX_RECENT_LOG_LINES}."
            )
        if limit == 0:
            return RuntimeLogTail(instance=instance, lines=())
        result = safe_read(
            "последних строк журнала",
            lambda: self._log_reader.read_tail(instance, limit),
        )
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise ServiceUnavailableError("Адаптер вернул некорректный tail журнала.")
        lines = tuple(result)
        if any(not isinstance(line, str) for line in lines) or len(lines) > limit:
            raise ServiceUnavailableError("Адаптер вернул некорректный tail журнала.")
        return RuntimeLogTail(instance=instance, lines=lines)

    def get_current_running_task(self, instance: str) -> CurrentTaskSnapshot:
        instance = known_instance(self._instance_reader, instance)
        runtime_execution_reader = self._runtime_execution_reader
        if runtime_execution_reader is None:
            raise ServiceUnavailableError(
                "Источник подтверждённого состояния выполнения недоступен."
            )
        result = safe_read(
            "текущей задачи",
            lambda: runtime_execution_reader.read_current_task(instance),
        )
        if not isinstance(result, CurrentTaskSnapshot) or result.instance != instance:
            raise ServiceUnavailableError(
                "Источник подтверждённого runtime вернул некорректное состояние."
            )
        return result

    def get_scheduler_queue(self, instance: str) -> SchedulerQueueSnapshot:
        instance = known_instance(self._instance_reader, instance)
        tasks = scheduler_tasks(self._scheduler_tasks)
        result = safe_read(
            "очереди scheduler",
            lambda: self._config_reader.read_scheduler_queue(instance, tasks),
        )
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise ServiceUnavailableError("Адаптер вернул некорректную очередь scheduler.")
        entries = tuple(result)
        if len(entries) > len(tasks) or any(
            not isinstance(entry, SchedulerEntry) or entry.task not in tasks
            for entry in entries
        ):
            raise ServiceUnavailableError("Адаптер вернул некорректную очередь scheduler.")
        try:
            return SchedulerQueueSnapshot(instance=instance, entries=entries)
        except (TypeError, ValueError):
            raise ServiceUnavailableError(
                "Адаптер вернул некорректную очередь scheduler."
            ) from None

    def get_screenshot(self, instance: str) -> MediaFrame:
        instance = known_instance(self._instance_reader, instance)
        result = safe_read(
            "снимка экрана",
            lambda: self._screenshot_reader.read_frame(instance),
        )
        if not isinstance(result, MediaFrame):
            raise ServiceUnavailableError("Адаптер вернул некорректный кадр.")
        return result


__all__ = ["GameReadService"]
