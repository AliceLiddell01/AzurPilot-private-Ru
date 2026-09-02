"""Сервисы управления нейтральной application boundary game/runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from module.application.errors import (
    ApplicationError,
    InvalidRequestError,
    OperationFailedError,
    PostconditionFailedError,
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
    SchedulerEntry,
    SchedulerQueueClearResult,
    ScheduleTaskRequest,
    ScheduleTaskResult,
    freeze_payload,
)
from module.application.game_ports import (
    AdbController,
    ConfigSchemaReader,
    EmulatorController,
    GameConfigReader,
    GameConfigWriter,
    InstanceLifecycleController,
    SchedulerTaskReader,
)
from module.application.game_validation import (
    known_instance,
    require_bool,
    safe_control,
    same_value,
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
        config_reader: GameConfigReader | None = None,
    ) -> None:
        self._instance_reader = instance_reader
        self._config_schema = config_schema
        self._config_writer = config_writer
        self._scheduler_tasks = scheduler_tasks
        self._lifecycle = lifecycle
        self._emulator = emulator
        self._adb = adb
        self._clock = clock or datetime.now
        self._config_reader = config_reader

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
        except ResourceNotFoundError:
            raise
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
        verified = self._verify_config_update(canonical_request)
        return ConfigUpdateResult(request=canonical_request, verified=verified)

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
            raise PostconditionFailedError("Экземпляр не подтвердил запуск.")
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
            raise PostconditionFailedError("Экземпляр не подтвердил остановку.")
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
        verified = self._verify_scheduled_task(instance, task, tasks)
        return ScheduleTaskResult(
            request=canonical_request,
            scheduled_at=scheduled_at,
            verified=verified,
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
        verified = self._verify_cleared_scheduler_queue(instance, cleared, tasks)
        return SchedulerQueueClearResult(
            instance=instance,
            cleared_tasks=cleared,
            verified=verified,
        )

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

    def restart_adb(self, instance: str | None) -> AdbRestartResult:
        if instance is None:
            raise InvalidRequestError("Перезапуск ADB требует имя экземпляра.")
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

    def _verify_config_update(self, request: ConfigUpdateRequest) -> bool:
        if self._config_reader is None:
            return False
        reader = getattr(self._config_reader, "read_config", None)
        if not callable(reader):
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            )
        try:
            data = reader(request.instance, request.task)
        except ResourceNotFoundError:
            raise
        except Exception:  # noqa: BLE001 - postcondition boundary is sanitized.
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            ) from None
        if not isinstance(data, Mapping):
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            )
        selected: object = data
        nested_task = data.get(request.task)
        if isinstance(nested_task, Mapping):
            selected = nested_task
        if not isinstance(selected, Mapping):
            raise PostconditionFailedError(
                "Изменение конфигурации не подтверждено."
            )
        group = selected.get(request.group)
        actual = group.get(request.argument) if isinstance(group, Mapping) else None
        if not isinstance(group, Mapping) or request.argument not in group:
            raise PostconditionFailedError(
                "Изменение конфигурации не подтверждено."
            )
        try:
            frozen_actual = freeze_payload(actual, field_name="readback")
        except TypeError:
            raise PostconditionFailedError(
                "Изменение конфигурации не подтверждено."
            ) from None
        if self._values_equal(request.value, frozen_actual):
            return True
        raise PostconditionFailedError("Изменение конфигурации не подтверждено.")

    def _verify_scheduled_task(
        self,
        instance: str,
        task: str,
        schedulable_tasks: tuple[str, ...],
    ) -> bool:
        if self._config_reader is None:
            return False
        reader = getattr(self._config_reader, "read_scheduler_queue", None)
        if not callable(reader):
            raise PostconditionFailedError(
                "Не удалось подтвердить планирование задачи."
            )
        try:
            queue = reader(instance, schedulable_tasks)
        except ResourceNotFoundError:
            raise
        except Exception:  # noqa: BLE001 - postcondition boundary is sanitized.
            raise PostconditionFailedError(
                "Не удалось подтвердить планирование задачи."
            ) from None
        if isinstance(queue, (str, bytes)) or not isinstance(queue, Sequence):
            raise PostconditionFailedError(
                "Планирование задачи не подтверждено."
            )
        entries = tuple(queue)
        if any(not isinstance(entry, SchedulerEntry) for entry in entries):
            raise PostconditionFailedError(
                "Планирование задачи не подтверждено."
            )
        if any(entry.task == task for entry in entries):
            return True
        raise PostconditionFailedError("Планирование задачи не подтверждено.")

    def _verify_cleared_scheduler_queue(
        self,
        instance: str,
        cleared: tuple[str, ...],
        schedulable_tasks: tuple[str, ...],
    ) -> bool:
        if self._config_reader is None:
            return False
        reader = getattr(self._config_reader, "read_scheduler_queue", None)
        if not callable(reader):
            raise PostconditionFailedError(
                "Не удалось подтвердить очистку очереди scheduler."
            )
        try:
            queue = reader(instance, schedulable_tasks)
        except ResourceNotFoundError:
            raise
        except Exception:  # noqa: BLE001 - postcondition boundary is sanitized.
            raise PostconditionFailedError(
                "Не удалось подтвердить очистку очереди scheduler."
            ) from None
        if isinstance(queue, (str, bytes)) or not isinstance(queue, Sequence):
            raise PostconditionFailedError(
                "Очистка очереди scheduler не подтверждена."
            )
        entries = tuple(queue)
        if any(not isinstance(entry, SchedulerEntry) for entry in entries):
            raise PostconditionFailedError(
                "Очистка очереди scheduler не подтверждена."
            )
        if entries:
            raise PostconditionFailedError(
                "Очистка очереди scheduler не подтверждена."
            )
        return True

    @staticmethod
    def _values_equal(left: object, right: object) -> bool:
        if same_value(left, right):
            return True
        if isinstance(left, str) and isinstance(right, datetime):
            try:
                return datetime.fromisoformat(left) == right
            except ValueError:
                return False
        if isinstance(right, str) and isinstance(left, datetime):
            try:
                return left == datetime.fromisoformat(right)
            except ValueError:
                return False
        return False


__all__ = ["GameControlService"]
