"""Сервисы управления нейтральной application boundary game/runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from functools import wraps
from math import isfinite
from pathlib import Path
from time import monotonic, sleep
from typing import Concatenate

from module.application.errors import (
    ApplicationError,
    GameRuntimePhaseError,
    InvalidRequestError,
    OperationFailedError,
    OwnershipAmbiguousError,
    PostconditionFailedError,
    PreconditionFailedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from module.application.game_control_lock import (
    GAME_CONTROL_LOCK_TIMEOUT_SECONDS,
    profile_mutation_lock,
)
from module.application.game_models import (
    AdbRestartResult,
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    EmulatorRestartResult,
    GameApplicationState,
    GameLoginResult,
    GameLoginState,
    GameRuntimeRestartResult,
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
    GameApplicationController,
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

_GAME_START_TIMEOUT_SECONDS = 60.0
_GAME_START_RETRY_INTERVAL_SECONDS = 0.5
_GAME_START_MAX_ATTEMPTS = 120
_GAME_LOGIN_TIMEOUT_SECONDS = 120.0


def _control_profile(value: object) -> str | None:
    if isinstance(value, (ConfigUpdateRequest, ScheduleTaskRequest)):
        value = value.instance
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _profile_mutation[**ControlParameters, ControlReturn](
    method: Callable[
        Concatenate[GameControlService, ControlParameters], ControlReturn
    ],
) -> Callable[Concatenate[GameControlService, ControlParameters], ControlReturn]:
    """Сериализовать каждую публичную mutation по canonical profile."""

    @wraps(method)
    def wrapped(
        self: GameControlService,
        *args: ControlParameters.args,
        **kwargs: ControlParameters.kwargs,
    ) -> ControlReturn:
        value = args[0] if args else kwargs.get("request", kwargs.get("instance"))
        profile = _control_profile(value)
        if profile is None:
            return method(self, *args, **kwargs)
        with profile_mutation_lock(
            profile,
            repository_root=self._mutation_lock_root,
            timeout=GAME_CONTROL_LOCK_TIMEOUT_SECONDS,
        ):
            return method(self, *args, **kwargs)

    return wrapped


class GameControlService:
    """API управления игрой с типизированными запросами и проверкой результата."""

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
        application: GameApplicationController | None = None,
        clock: Callable[[], datetime] | None = None,
        config_reader: GameConfigReader,
        mutation_lock_root: Path | str | None = None,
        game_start_timeout_seconds: float = _GAME_START_TIMEOUT_SECONDS,
        game_start_retry_interval_seconds: float = _GAME_START_RETRY_INTERVAL_SECONDS,
        game_start_max_attempts: int = _GAME_START_MAX_ATTEMPTS,
        game_login_timeout_seconds: float = _GAME_LOGIN_TIMEOUT_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if config_reader is None:
            raise TypeError("config_reader обязателен для подтверждения результата")
        for method_name in ("read_config", "read_scheduler_queue"):
            if not callable(getattr(config_reader, method_name, None)):
                raise TypeError(
                    f"config_reader не предоставляет {method_name} для подтверждения результата"
                )
        self._instance_reader = instance_reader
        self._config_schema = config_schema
        self._config_writer = config_writer
        self._scheduler_tasks = scheduler_tasks
        self._lifecycle = lifecycle
        self._emulator = emulator
        self._adb = adb
        self._application = application
        self._clock = clock or datetime.now
        self._config_reader = config_reader
        self._mutation_lock_root = mutation_lock_root
        if (
            type(game_start_timeout_seconds) is not float
            or not isfinite(game_start_timeout_seconds)
            or game_start_timeout_seconds < 0
        ):
            raise ValueError(
                "game_start_timeout_seconds должен быть неотрицательным float"
            )
        if (
            type(game_start_retry_interval_seconds) is not float
            or not isfinite(game_start_retry_interval_seconds)
            or game_start_retry_interval_seconds < 0
        ):
            raise ValueError(
                "game_start_retry_interval_seconds должен быть неотрицательным float"
            )
        if type(game_start_max_attempts) is not int or game_start_max_attempts < 1:
            raise ValueError("game_start_max_attempts должен быть положительным int")
        if (
            type(game_login_timeout_seconds) is not float
            or not isfinite(game_login_timeout_seconds)
            or game_login_timeout_seconds < 0
        ):
            raise ValueError(
                "game_login_timeout_seconds должен быть неотрицательным конечным float"
            )
        self._game_start_timeout_seconds = game_start_timeout_seconds
        self._game_start_retry_interval_seconds = game_start_retry_interval_seconds
        self._game_start_max_attempts = game_start_max_attempts
        self._game_login_timeout_seconds = game_login_timeout_seconds
        self._monotonic = monotonic_clock or monotonic
        self._sleep = sleep_fn or sleep

    @_profile_mutation
    def update_config(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        if not isinstance(request, ConfigUpdateRequest):
            raise InvalidRequestError("Запрос изменения конфигурации имеет неверный тип.")
        instance = known_instance(self._instance_reader, request.instance)
        task = validated_segment(request.task, resource="задачи")
        group = validated_segment(request.group, resource="группы")
        argument = validated_segment(request.argument, resource="аргумента")
        self._readback_method(
            "read_config", "Не удалось подтвердить изменение конфигурации."
        )
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

    @_profile_mutation
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

    @_profile_mutation
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

    @_profile_mutation
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
        self._readback_method(
            "read_scheduler_queue", "Не удалось подтвердить планирование задачи."
        )
        canonical_request = ScheduleTaskRequest(instance=instance, task=task)
        safe_control(
            "немедленного планирования задачи",
            lambda: self._config_writer.schedule_task(
                instance,
                task,
                scheduled_at,
            ),
        )
        verified = self._verify_scheduled_task(
            instance,
            task,
            scheduled_at,
            tasks,
        )
        return ScheduleTaskResult(
            request=canonical_request,
            scheduled_at=scheduled_at,
            verified=verified,
        )

    @_profile_mutation
    def clear_scheduler_queue(self, instance: str) -> SchedulerQueueClearResult:
        instance = known_instance(self._instance_reader, instance)
        tasks = scheduler_tasks(self._scheduler_tasks)
        self._readback_method(
            "read_scheduler_queue", "Не удалось подтвердить очистку очереди scheduler."
        )
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

    @_profile_mutation
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

    @_profile_mutation
    def restart_runtime(self, instance: str) -> GameRuntimeRestartResult:
        """Перезапустить эмулятор и вернуть настроенную игру на передний план."""

        instance = known_instance(self._instance_reader, instance)
        self._run_runtime_phase(
            "emulator_restart",
            lambda: self._restart_emulator_transition(instance),
        )
        state = self._run_runtime_phase(
            "game_start",
            lambda: self._ensure_game_started(instance),
        )
        if not isinstance(state, GameApplicationState) or not self._game_ready(state):
            raise GameRuntimePhaseError(
                "game_start",
                PostconditionFailedError(
                    "Эмулятор перезапущен, но игра не подтвердила рабочее состояние."
                ),
            )
        return GameRuntimeRestartResult(
            instance=instance,
            emulator_verified=True,
            adb_ready=True,
            game_running=True,
            game_foreground=True,
        )

    @_profile_mutation
    def login_runtime(self, instance: str) -> GameLoginResult:
        """Выполнить существующий login flow и подтвердить главный экран."""

        instance = known_instance(self._instance_reader, instance)
        state = self._run_runtime_phase(
            "login",
            lambda: self._ensure_logged_in(instance),
        )
        if not isinstance(state, GameLoginState) or not self._login_ready(state):
            raise GameRuntimePhaseError(
                "login",
                PostconditionFailedError(
                    "Вход в игру не подтвердил рабочее состояние главного экрана."
                ),
            )
        return GameLoginResult(
            instance=instance,
            verified=True,
            adb_ready=True,
            game_running=True,
            game_foreground=True,
            logged_in=True,
            main=True,
        )

    @_profile_mutation
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

    def _restart_emulator_transition(self, instance: str) -> None:
        result = require_bool(
            safe_control(
                "перезапуска эмулятора",
                lambda: self._emulator.restart_emulator(instance),
            ),
            operation="перезапуска эмулятора",
        )
        if not result:
            raise OperationFailedError("Эмулятор не подтвердил перезапуск.")

    def _ensure_game_started(self, instance: str) -> GameApplicationState:
        application = self._application
        if application is None:
            raise ServiceUnavailableError("Application capability запуска игры недоступна.")

        state = self._await_adb_ready(application, instance)
        if self._game_ready(state):
            return state

        start_error: ApplicationError | None = None
        started = False
        try:
            started = require_bool(
                safe_control(
                    "запуска настроенной игры",
                    lambda: application.start_game(instance),
                ),
                operation="запуска игры",
            )
        except ApplicationError as error:
            start_error = error

        if start_error is not None or not started:
            if isinstance(start_error, (OwnershipAmbiguousError, PreconditionFailedError)):
                raise start_error
            state = self._read_application_state(application, instance)
            if self._game_ready(state):
                return state
            raise start_error or OperationFailedError(
                "Команда запуска игры не подтвердила выполнение."
            )
        return self._await_game_ready(application, instance)

    def _await_adb_ready(
        self,
        application: GameApplicationController,
        instance: str,
    ) -> GameApplicationState:
        deadline = self._monotonic() + self._game_start_timeout_seconds
        last_error: ApplicationError | None = None
        for attempt in range(self._game_start_max_attempts):
            try:
                state = self._read_application_state(application, instance)
            except (OwnershipAmbiguousError, PreconditionFailedError) as error:
                last_error = error
            else:
                if state.adb_ready is True:
                    return state
                last_error = PreconditionFailedError(
                    "ADB не подтвердил готовность target после перезапуска эмулятора."
                )
            if (
                attempt + 1 >= self._game_start_max_attempts
                or self._monotonic() >= deadline
            ):
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self._game_start_retry_interval_seconds, remaining))
        if last_error is not None:
            raise last_error
        raise PreconditionFailedError(
            "ADB не подтвердил готовность target после перезапуска эмулятора."
        )

    def _ensure_logged_in(self, instance: str) -> GameLoginState:
        application = self._application
        if application is None:
            raise ServiceUnavailableError("Application capability входа в игру недоступна.")
        state = safe_control(
            "входа в игру и подтверждения главного экрана",
            lambda: application.login_to_main(
                instance,
                timeout_seconds=self._game_login_timeout_seconds,
            ),
        )
        if not isinstance(state, GameLoginState):
            raise OperationFailedError(
                "Application owner вернул некорректное состояние входа в игру."
            )
        return state

    def _await_game_ready(
        self,
        application: GameApplicationController,
        instance: str,
    ) -> GameApplicationState:
        deadline = self._monotonic() + self._game_start_timeout_seconds
        for attempt in range(self._game_start_max_attempts):
            state = self._read_application_state(application, instance)
            if state.adb_ready is not True:
                raise PreconditionFailedError(
                    "ADB потерял готовность target во время запуска игры."
                )
            if self._game_ready(state):
                return state
            if (
                attempt + 1 >= self._game_start_max_attempts
                or self._monotonic() >= deadline
            ):
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self._game_start_retry_interval_seconds, remaining))
        raise PostconditionFailedError(
            "Эмулятор перезапущен, но игра не подтвердила foreground в bounded wait."
        )

    @staticmethod
    def _read_application_state(
        application: GameApplicationController,
        instance: str,
    ) -> GameApplicationState:
        state = safe_control(
            "проверки состояния настроенной игры",
            lambda: application.read_state(instance),
        )
        if not isinstance(state, GameApplicationState):
            raise OperationFailedError(
                "Application owner вернул некорректное состояние игры."
            )
        return state

    @staticmethod
    def _game_ready(state: GameApplicationState) -> bool:
        return (
            state.adb_ready is True
            and state.game_running is True
            and state.game_foreground is True
        )

    @staticmethod
    def _login_ready(state: GameLoginState) -> bool:
        return (
            state.adb_ready is True
            and state.game_running is True
            and state.game_foreground is True
            and state.logged_in is True
            and state.main is True
        )

    @staticmethod
    def _run_runtime_phase(phase: str, callback: Callable[[], object]) -> object:
        try:
            return callback()
        except GameRuntimePhaseError:
            raise
        except ApplicationError as error:
            raise GameRuntimePhaseError(phase, error) from None
        except Exception:  # noqa: BLE001 - composite boundary fails closed.
            message = (
                "Перезапуск эмулятора не подтверждён."
                if phase == "emulator_restart"
                else "Вход в игру не подтверждён."
                if phase == "login"
                else "Запуск игры не подтверждён."
            )
            raise GameRuntimePhaseError(phase, OperationFailedError(message)) from None

    def _verify_config_update(self, request: ConfigUpdateRequest) -> bool:
        reader = self._readback_method(
            "read_config", "Не удалось подтвердить изменение конфигурации."
        )
        try:
            data = reader(request.instance, request.task)
        except ResourceNotFoundError:
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            ) from None
        except Exception:  # noqa: BLE001 - граница постусловия скрывает детали.
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            ) from None
        if not isinstance(data, Mapping):
            raise PostconditionFailedError(
                "Не удалось подтвердить изменение конфигурации."
            )
        group = data.get(request.group)
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

    def _read_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: tuple[str, ...],
        *,
        unavailable_message: str,
        invalid_message: str,
    ) -> tuple[SchedulerEntry, ...]:
        reader = self._readback_method("read_scheduler_queue", unavailable_message)
        try:
            queue = reader(instance, schedulable_tasks)
        except ResourceNotFoundError:
            raise PostconditionFailedError(unavailable_message) from None
        except Exception:  # noqa: BLE001 - граница постусловия скрывает детали.
            raise PostconditionFailedError(unavailable_message) from None
        if isinstance(queue, (str, bytes)) or not isinstance(queue, Sequence):
            raise PostconditionFailedError(invalid_message)
        entries = tuple(queue)
        if any(not isinstance(entry, SchedulerEntry) for entry in entries):
            raise PostconditionFailedError(invalid_message)
        return entries

    def _verify_scheduled_task(
        self,
        instance: str,
        task: str,
        expected_scheduled_at: datetime,
        schedulable_tasks: tuple[str, ...],
    ) -> bool:
        entries = self._read_scheduler_queue(
            instance,
            schedulable_tasks,
            unavailable_message="Не удалось подтвердить планирование задачи.",
            invalid_message="Планирование задачи не подтверждено.",
        )
        if any(
            entry.task == task
            and self._values_equal(expected_scheduled_at, entry.next_run)
            for entry in entries
        ):
            return True
        raise PostconditionFailedError("Планирование задачи не подтверждено.")

    def _verify_cleared_scheduler_queue(
        self,
        instance: str,
        cleared: tuple[str, ...],
        schedulable_tasks: tuple[str, ...],
    ) -> bool:
        entries = self._read_scheduler_queue(
            instance,
            schedulable_tasks,
            unavailable_message="Не удалось подтвердить очистку очереди scheduler.",
            invalid_message="Очистка очереди scheduler не подтверждена.",
        )
        if entries:
            raise PostconditionFailedError(
                "Очистка очереди scheduler не подтверждена."
            )
        return True

    def _readback_method(
        self,
        name: str,
        unavailable_message: str,
    ) -> Callable[..., object]:
        reader = self._config_reader
        method = getattr(reader, name, None)
        if not callable(method):
            raise PostconditionFailedError(unavailable_message)
        return method

    @staticmethod
    def _values_equal(left: object, right: object) -> bool:
        if isinstance(left, datetime) and isinstance(right, datetime):
            return GameControlService._datetimes_equal(left, right)
        if same_value(left, right):
            return True
        if isinstance(left, str) and isinstance(right, datetime):
            try:
                return GameControlService._datetimes_equal(
                    datetime.fromisoformat(left), right
                )
            except ValueError:
                return False
        if isinstance(right, str) and isinstance(left, datetime):
            try:
                return GameControlService._datetimes_equal(
                    left, datetime.fromisoformat(right)
                )
            except ValueError:
                return False
        return False

    @staticmethod
    def _datetimes_equal(left: datetime, right: datetime) -> bool:
        if (left.tzinfo is None) != (right.tzinfo is None):
            return False
        if left.tzinfo is None:
            return left == right
        return left.astimezone(UTC) == right.astimezone(UTC)


__all__ = ["GameControlService"]
