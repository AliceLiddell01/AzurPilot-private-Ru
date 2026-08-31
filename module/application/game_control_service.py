"""Сервисы управления нейтральной application boundary game/runtime."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from module.application.errors import (
    ApplicationError,
    InvalidRequestError,
    OperationFailedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from module.application.game_models import (
    AdbRestartResult,
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    EmulatorRestartResult,
    LifecycleOutcome,
    LifecycleResult,
    SchedulerQueueClearResult,
    ScheduleTaskRequest,
    ScheduleTaskResult,
)
from module.application.game_ports import (
    AdbController,
    ConfigSchemaReader,
    EmulatorController,
    GameConfigWriter,
    InstanceLifecycleController,
    SchedulerTaskReader,
)
from module.application.game_validation import (
    known_instance,
    require_bool,
    safe_control,
    scheduler_tasks,
    validate_config_value,
    validated_segment,
)
from module.application.ports import InstanceRuntimeReader


class GameControlService:
    """Control API игры с явными typed requests и fail-closed результатами."""

    def __init__(
        self,
        instance_reader: InstanceRuntimeReader,
        config_schema: ConfigSchemaReader,
        config_writer: GameConfigWriter,
        scheduler_tasks: SchedulerTaskReader,
        lifecycle: InstanceLifecycleController,
        emulator: EmulatorController,
        adb: AdbController,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._instance_reader = instance_reader
        self._config_schema = config_schema
        self._config_writer = config_writer
        self._scheduler_tasks = scheduler_tasks
        self._lifecycle = lifecycle
        self._emulator = emulator
        self._adb = adb
        self._clock = clock or datetime.now

    def update_config(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        if not isinstance(request, ConfigUpdateRequest):
            raise InvalidRequestError("Запрос изменения конфигурации имеет неверный тип.")
        instance = known_instance(self._instance_reader, request.instance)
        task = validated_segment(request.task, resource="задачи")
        group = validated_segment(request.group, resource="группы")
        argument = validated_segment(request.argument, resource="аргумента")
        try:
            definition = self._config_schema.read_argument_definition(
                task,
                group,
                argument,
            )
        except ApplicationError:
            raise ServiceUnavailableError(
                "Не удалось проверить metadata конфигурации."
            ) from None
        except Exception:  # noqa: BLE001
            raise ServiceUnavailableError(
                "Не удалось проверить metadata конфигурации."
            ) from None
        if not isinstance(definition, ConfigArgumentDefinition):
            raise ResourceNotFoundError("Параметр конфигурации не найден.")
        if (
            definition.task != task
            or definition.group != group
            or definition.argument != argument
        ):
            raise ServiceUnavailableError("Metadata конфигурации не совпадает с запросом.")
        validate_config_value(definition, request.value)
        canonical_request = ConfigUpdateRequest(
            instance=instance,
            task=task,
            group=group,
            argument=argument,
            value=request.value,
        )
        safe_control(
            "изменения конфигурации",
            lambda: self._config_writer.update_config(canonical_request),
        )
        return ConfigUpdateResult(request=canonical_request)

    def start_instance(self, instance: str) -> LifecycleResult:
        instance = known_instance(self._instance_reader, instance)
        running = require_bool(
            safe_control(
                "проверки состояния перед запуском",
                lambda: self._lifecycle.is_running(instance),
            ),
            operation="запуска",
        )
        if running:
            return LifecycleResult(instance, LifecycleOutcome.ALREADY_RUNNING)
        started = require_bool(
            safe_control(
                "запуска экземпляра",
                lambda: self._lifecycle.start_instance(instance),
            ),
            operation="запуска",
        )
        if not started:
            raise OperationFailedError("Экземпляр не подтвердил запуск.")
        confirmed = require_bool(
            safe_control(
                "подтверждения запуска",
                lambda: self._lifecycle.is_running(instance),
            ),
            operation="запуска",
        )
        if not confirmed:
            raise OperationFailedError("Экземпляр не подтвердил запуск.")
        return LifecycleResult(instance, LifecycleOutcome.STARTED)

    def stop_instance(self, instance: str) -> LifecycleResult:
        instance = known_instance(self._instance_reader, instance)
        running = require_bool(
            safe_control(
                "проверки состояния перед остановкой",
                lambda: self._lifecycle.is_running(instance),
            ),
            operation="остановки",
        )
        if not running:
            return LifecycleResult(instance, LifecycleOutcome.ALREADY_STOPPED)
        stopped = require_bool(
            safe_control(
                "остановки экземпляра",
                lambda: self._lifecycle.stop_instance(instance),
            ),
            operation="остановки",
        )
        if not stopped:
            raise OperationFailedError("Экземпляр не подтвердил остановку.")
        confirmed = require_bool(
            safe_control(
                "подтверждения остановки",
                lambda: self._lifecycle.is_running(instance),
            ),
            operation="остановки",
        )
        if confirmed:
            raise OperationFailedError("Экземпляр не подтвердил остановку.")
        return LifecycleResult(instance, LifecycleOutcome.STOPPED)

    def trigger_task(self, request: ScheduleTaskRequest) -> ScheduleTaskResult:
        if not isinstance(request, ScheduleTaskRequest):
            raise InvalidRequestError("Запрос планирования имеет неверный тип.")
        instance = known_instance(self._instance_reader, request.instance)
        task = validated_segment(request.task, resource="задачи")
        tasks = scheduler_tasks(self._scheduler_tasks)
        if task not in tasks:
            raise ResourceNotFoundError("Задача не входит в generated scheduler registry.")
        scheduled_at = safe_control(
            "получения времени планирования",
            self._clock,
        )
        if not isinstance(scheduled_at, datetime):
            raise OperationFailedError("Источник времени вернул некорректное значение.")
        canonical_request = ScheduleTaskRequest(instance=instance, task=task)
        safe_control(
            "немедленного планирования задачи",
            lambda: self._config_writer.schedule_task(
                instance,
                task,
                scheduled_at,
            ),
        )
        return ScheduleTaskResult(
            request=canonical_request,
            scheduled_at=scheduled_at,
        )

    def clear_scheduler_queue(self, instance: str) -> SchedulerQueueClearResult:
        instance = known_instance(self._instance_reader, instance)
        tasks = scheduler_tasks(self._scheduler_tasks)
        result = safe_control(
            "очистки очереди scheduler",
            lambda: self._config_writer.clear_scheduler_queue(instance, tasks),
        )
        if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
            raise OperationFailedError("Адаптер очистки очереди вернул неверный результат.")
        cleared = tuple(result)
        if any(
            not isinstance(task, str) or task not in tasks for task in cleared
        ) or len(cleared) != len(set(cleared)):
            raise OperationFailedError("Адаптер очистки очереди вернул неверный результат.")
        return SchedulerQueueClearResult(instance=instance, cleared_tasks=cleared)

    def restart_emulator(self, instance: str) -> EmulatorRestartResult:
        instance = known_instance(self._instance_reader, instance)
        result = require_bool(
            safe_control(
                "перезапуска эмулятора",
                lambda: self._emulator.restart_emulator(instance),
            ),
            operation="перезапуска эмулятора",
        )
        if not result:
            raise OperationFailedError("Эмулятор не подтвердил перезапуск.")
        return EmulatorRestartResult(instance=instance)

    def restart_adb(self, instance: str | None = None) -> AdbRestartResult:
        if instance is None:
            raise OperationFailedError("Перезапуск ADB без экземпляра запрещён.")
        instance = known_instance(self._instance_reader, instance)
        result = require_bool(
            safe_control(
                "перезапуска ADB",
                lambda: self._adb.restart_adb(instance),
            ),
            operation="перезапуска ADB",
        )
        if not result:
            raise OperationFailedError("ADB не подтвердил перезапуск.")
        return AdbRestartResult(instance=instance)


__all__ = ["GameControlService"]
