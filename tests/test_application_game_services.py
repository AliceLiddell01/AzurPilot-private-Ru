from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from module.application import (
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
    ConfigurationValidationError,
    CurrentTaskSnapshot,
    DashboardResource,
    DashboardResources,
    GameControlService,
    GameReadService,
    InstanceNotRunningError,
    InvalidRequestError,
    LifecycleOutcome,
    MediaFrame,
    OperationFailedError,
    PostconditionFailedError,
    ResourceNotFoundError,
    RuntimeState,
    SchedulerEntry,
    ScheduleTaskRequest,
    ServiceUnavailableError,
)
from module.application.game_validation import validate_config_value
from module.application.ports import RuntimeSnapshot


class _Instances:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running

    def list_instance_names(self) -> tuple[str, ...]:
        return ("ap", "secondary")

    def read_instance_status(self, name: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            running=self.running,
            state_code=RuntimeState.RUNNING if self.running else RuntimeState.STOPPED,
        )


class _Metadata:
    def __init__(self) -> None:
        self.tasks = ("Main", "Event")
        self.definitions = {
            ("Main", "Fleet", "Count"): ConfigArgumentDefinition(
                task="Main",
                group="Fleet",
                argument="Count",
                input_type="input",
                default=1,
                validation=(1, 6),
            ),
            ("Main", "General", "Mode"): ConfigArgumentDefinition(
                task="Main",
                group="General",
                argument="Mode",
                input_type="select",
                default="safe",
                options=("safe", "fast"),
            ),
            ("Main", "General", "Text"): ConfigArgumentDefinition(
                task="Main",
                group="General",
                argument="Text",
                input_type="input",
                default="",
            ),
            ("Main", "Scheduler", "NextRun"): ConfigArgumentDefinition(
                task="Main",
                group="Scheduler",
                argument="NextRun",
                input_type="datetime",
                default="2020-01-01 00:00:00",
                validation="datetime",
            ),
            ("Main", "Error", "ApiKey"): ConfigArgumentDefinition(
                task="Main",
                group="Error",
                argument="ApiKey",
                input_type="textarea",
                default="",
                sensitive=True,
            ),
        }

    def list_schedulable_task_names(self) -> tuple[str, ...]:
        return self.tasks

    def read_argument_definition(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> ConfigArgumentDefinition | None:
        return self.definitions.get((task, group, argument))


class _ConfigSchema:
    def __init__(
        self,
        definitions: dict[tuple[str, str, str], ConfigArgumentDefinition],
    ) -> None:
        self.definitions = definitions

    def read_argument_definition(
        self,
        task: str,
        group: str,
        argument: str,
    ) -> ConfigArgumentDefinition | None:
        return self.definitions.get((task, group, argument))


class _SchedulerTasks:
    def __init__(self, tasks: tuple[str, ...]) -> None:
        self.tasks = tasks

    def list_schedulable_task_names(self) -> tuple[str, ...]:
        return self.tasks


class _Config:
    def __init__(self) -> None:
        self.updated: list[ConfigUpdateRequest] = []
        self.scheduled: list[tuple[str, str, datetime]] = []
        self.cleared_for: list[tuple[str, tuple[str, ...]]] = []

    def read_config(self, instance: str, task: str | None = None) -> dict[str, object]:
        data: dict[str, object] = {
            "Main": {"Fleet": {"Count": 1}},
            "Error": {"ApiKey": "opaque-value"},
        }
        return data[task] if task else data  # type: ignore[return-value]

    def read_resources(self, instance: str) -> DashboardResources:
        return DashboardResources(
            items=(DashboardResource("Oil", "Нефть", 10, limit=100),)
        )

    def read_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: tuple[str, ...],
    ) -> tuple[SchedulerEntry, ...]:
        assert schedulable_tasks == ("Main", "Event")
        return (SchedulerEntry("Main", datetime(2026, 8, 31, 12, 0, tzinfo=UTC)),)

    def update_config(self, request: ConfigUpdateRequest) -> None:
        self.updated.append(request)

    def schedule_task(self, instance: str, task: str, scheduled_at: datetime) -> None:
        self.scheduled.append((instance, task, scheduled_at))

    def clear_scheduler_queue(
        self,
        instance: str,
        schedulable_tasks: tuple[str, ...],
    ) -> tuple[str, ...]:
        self.cleared_for.append((instance, tuple(schedulable_tasks)))
        return ("Main",)


class _Logs:
    def __init__(self, *, current: str = "Main") -> None:
        self.current = current

    def read_tail(self, instance: str, limit: int) -> tuple[str, ...]:
        return ("line 1\n", "line 2\n")[-limit:]

    def read_current_task(self, instance: str) -> str:
        return self.current


class _Screens:
    def read_frame(self, instance: str) -> MediaFrame:
        return MediaFrame(b"jpeg", "image/jpeg")


class _Lifecycle:
    def __init__(self) -> None:
        self.running = False
        self.calls: list[str] = []

    def is_running(self, instance: str) -> bool:
        self.calls.append("status")
        return self.running

    def start_instance(self, instance: str) -> bool:
        self.calls.append("start")
        self.running = True
        return True

    def stop_instance(self, instance: str) -> bool:
        self.calls.append("stop")
        self.running = False
        return True


class _Emulator:
    def restart_emulator(self, instance: str) -> bool:
        return True


class _Adb:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def restart_adb(self, instance: str | None = None) -> bool:
        self.calls.append(instance)
        return True


def _read_service(
    instances: _Instances | None = None,
    *,
    config: _Config | None = None,
    logs: _Logs | None = None,
    screens: _Screens | None = None,
    metadata: _Metadata | None = None,
) -> GameReadService:
    instances = instances or _Instances()
    config = config or _Config()
    logs = logs or _Logs()
    screens = screens or _Screens()
    metadata = metadata or _Metadata()
    return GameReadService(instances, config, logs, screens, metadata)


def _control_service(
    instances: _Instances | None = None,
    *,
    config: _Config | None = None,
    metadata: _Metadata | None = None,
    lifecycle: _Lifecycle | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[GameControlService, _Config, _Lifecycle]:
    instances = instances or _Instances()
    config = config or _Config()
    metadata = metadata or _Metadata()
    lifecycle = lifecycle or _Lifecycle()
    config_schema = _ConfigSchema(metadata.definitions)
    scheduler_tasks = _SchedulerTasks(metadata.tasks)
    return (
        GameControlService(
            instance_reader=instances,
            config_schema=config_schema,
            config_writer=config,
            scheduler_tasks=scheduler_tasks,
            lifecycle=lifecycle,
            emulator=_Emulator(),
            adb=_Adb(),
            clock=clock or (lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC)),
        ),
        config,
        lifecycle,
    )


def test_read_service_returns_typed_bounded_results_and_canonical_instance():
    service = _read_service()

    assert service.get_resources(" ap ").items[0].value == 10
    snapshot = service.get_config("ap")
    assert snapshot.instance == "ap"
    assert snapshot.data["Main"]["Fleet"]["Count"] == 1  # type: ignore[index]
    assert service.get_recent_logs("ap", 1).lines == ("line 2\n",)
    assert service.get_current_running_task("ap") == CurrentTaskSnapshot("ap", "Main")
    assert service.get_scheduler_queue("ap").entries[0].task == "Main"
    assert service.get_screenshot("ap").media_type == "image/jpeg"


def test_read_service_rejects_invalid_unknown_and_not_running_instances():
    service = _read_service(_Instances(running=False))

    with pytest.raises(InvalidRequestError):
        service.get_config("../ap")
    with pytest.raises(ResourceNotFoundError):
        service.get_config("missing")
    with pytest.raises(InstanceNotRunningError):
        service.get_current_running_task("ap")
    with pytest.raises(InvalidRequestError):
        service.get_recent_logs("ap", -1)
    with pytest.raises(InvalidRequestError):
        service.get_recent_logs("ap", 10_001)


def test_read_service_sanitizes_malformed_adapter_results_and_exceptions():
    class BrokenConfig(_Config):
        def read_resources(self, instance: str) -> DashboardResources:
            raise RuntimeError("internal config adapter detail")

    with pytest.raises(ServiceUnavailableError) as failure:
        _read_service(config=BrokenConfig()).get_resources("ap")
    assert "internal" not in str(failure.value)
    assert "detail" not in str(failure.value)
    assert failure.value.__cause__ is None

    class TooManyLogs(_Logs):
        def read_tail(self, instance: str, limit: int) -> tuple[str, ...]:
            return tuple("x\n" for _ in range(limit + 1))

    with pytest.raises(ServiceUnavailableError):
        _read_service(logs=TooManyLogs()).get_recent_logs("ap", 2)

    class BrokenScreenshot:
        def read_frame(self, instance: str) -> bytes:
            return b"not a MediaFrame"

    with pytest.raises(ServiceUnavailableError):
        GameReadService(_Instances(), _Config(), _Logs(), BrokenScreenshot(), _Metadata()).get_screenshot("ap")  # type: ignore[arg-type]


def test_application_boundaries_sanitize_application_errors_from_ports():
    class BrokenInstances(_Instances):
        def list_instance_names(self) -> tuple[str, ...]:
            raise ServiceUnavailableError("internal instances adapter detail")

    with pytest.raises(ServiceUnavailableError) as read_failure:
        _read_service(instances=BrokenInstances()).get_config("ap")
    assert "internal" not in str(read_failure.value)
    assert "detail" not in str(read_failure.value)

    class BrokenWriter(_Config):
        def update_config(self, request: ConfigUpdateRequest) -> None:
            raise ServiceUnavailableError("internal profile adapter detail")

    service, _config, _lifecycle = _control_service(config=BrokenWriter())
    with pytest.raises(OperationFailedError) as control_failure:
        service.update_config(ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 2))
    assert "internal" not in str(control_failure.value)
    assert "detail" not in str(control_failure.value)


def test_control_service_validates_config_scheduler_and_lifecycle_postconditions():
    service, config, lifecycle = _control_service()
    update = service.update_config(
        ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 4)
    )
    assert update.request.path == "Main.Fleet.Count"
    assert config.updated[0].value == 4

    assert service.start_instance("ap").outcome is LifecycleOutcome.STARTED
    assert service.start_instance("ap").outcome is LifecycleOutcome.ALREADY_RUNNING
    assert service.stop_instance("ap").outcome is LifecycleOutcome.STOPPED
    assert service.stop_instance("ap").outcome is LifecycleOutcome.ALREADY_STOPPED
    assert service.trigger_task(ScheduleTaskRequest("ap", "Event")).request.task == "Event"
    assert service.clear_scheduler_queue("ap").cleared_tasks == ("Main",)
    assert service.restart_emulator("ap").instance == "ap"
    assert service.restart_adb("secondary").instance == "secondary"
    assert lifecycle.calls == [
        "status",
        "start",
        "status",
        "status",
        "status",
        "stop",
        "status",
        "status",
    ]


def test_control_service_verifies_config_and_scheduler_readbacks():
    class _AuthoritativeConfig(_Config):
        def __init__(self) -> None:
            super().__init__()
            self.count = 1
            self.scheduled_tasks: set[str] = set()

        def update_config(self, request: ConfigUpdateRequest) -> None:
            super().update_config(request)
            if request.task == "Main" and request.group == "Fleet":
                self.count = request.value  # type: ignore[assignment]

        def read_config(self, instance: str, task: str | None = None) -> dict[str, object]:
            data: dict[str, object] = {
                "Main": {"Fleet": {"Count": self.count}},
            }
            return data[task] if task else data  # type: ignore[return-value]

        def schedule_task(self, instance: str, task: str, scheduled_at: datetime) -> None:
            super().schedule_task(instance, task, scheduled_at)
            self.scheduled_tasks.add(task)

        def read_scheduler_queue(
            self,
            instance: str,
            schedulable_tasks: tuple[str, ...],
        ) -> tuple[SchedulerEntry, ...]:
            return tuple(
                SchedulerEntry(task, datetime(2026, 8, 31, 12, 0, tzinfo=UTC))
                for task in schedulable_tasks
                if task in self.scheduled_tasks
            )

        def clear_scheduler_queue(
            self,
            instance: str,
            schedulable_tasks: tuple[str, ...],
        ) -> tuple[str, ...]:
            cleared = tuple(task for task in schedulable_tasks if task in self.scheduled_tasks)
            self.scheduled_tasks.difference_update(cleared)
            return cleared

    config = _AuthoritativeConfig()
    metadata = _Metadata()
    service = GameControlService(
        instance_reader=_Instances(),
        config_schema=_ConfigSchema(metadata.definitions),
        config_writer=config,
        scheduler_tasks=_SchedulerTasks(metadata.tasks),
        lifecycle=_Lifecycle(),
        emulator=_Emulator(),
        adb=_Adb(),
        clock=lambda: datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        config_reader=config,
    )

    updated = service.update_config(
        ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 4)
    )
    assert updated.verified is True
    scheduled = service.trigger_task(ScheduleTaskRequest("ap", "Event"))
    assert scheduled.verified is True
    cleared = service.clear_scheduler_queue("ap")
    assert cleared.verified is True
    assert cleared.cleared_tasks == ("Event",)

    stale_writer = _Config()
    stale_reader = _Config()
    stale_service = GameControlService(
        instance_reader=_Instances(),
        config_schema=_ConfigSchema(metadata.definitions),
        config_writer=stale_writer,
        scheduler_tasks=_SchedulerTasks(metadata.tasks),
        lifecycle=_Lifecycle(),
        emulator=_Emulator(),
        adb=_Adb(),
        config_reader=stale_reader,
    )
    with pytest.raises(PostconditionFailedError):
        stale_service.update_config(
            ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 5)
        )

    class StaleQueueConfig(_AuthoritativeConfig):
        def clear_scheduler_queue(
            self,
            instance: str,
            schedulable_tasks: tuple[str, ...],
        ) -> tuple[str, ...]:
            return tuple(task for task in schedulable_tasks if task in self.scheduled_tasks)

    stale_queue = StaleQueueConfig()
    stale_queue.scheduled_tasks.add("Event")
    stale_queue_service = GameControlService(
        instance_reader=_Instances(),
        config_schema=_ConfigSchema(metadata.definitions),
        config_writer=stale_queue,
        scheduler_tasks=_SchedulerTasks(metadata.tasks),
        lifecycle=_Lifecycle(),
        emulator=_Emulator(),
        adb=_Adb(),
        config_reader=stale_queue,
    )
    with pytest.raises(PostconditionFailedError):
        stale_queue_service.clear_scheduler_queue("ap")

    class MissingReadback(_AuthoritativeConfig):
        def read_config(self, instance: str, task: str | None = None) -> dict[str, object]:
            raise ResourceNotFoundError("readback unavailable")

        def read_scheduler_queue(
            self,
            instance: str,
            schedulable_tasks: tuple[str, ...],
        ) -> tuple[SchedulerEntry, ...]:
            raise ResourceNotFoundError("readback unavailable")

    missing = MissingReadback()
    missing_service = GameControlService(
        instance_reader=_Instances(),
        config_schema=_ConfigSchema(metadata.definitions),
        config_writer=missing,
        scheduler_tasks=_SchedulerTasks(metadata.tasks),
        lifecycle=_Lifecycle(),
        emulator=_Emulator(),
        adb=_Adb(),
        config_reader=missing,
    )
    with pytest.raises(PostconditionFailedError):
        missing_service.update_config(
            ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 5)
        )
    with pytest.raises(PostconditionFailedError):
        missing_service.trigger_task(ScheduleTaskRequest("ap", "Event"))
    with pytest.raises(PostconditionFailedError):
        missing_service.clear_scheduler_queue("ap")


def test_control_service_rejects_unbounded_config_values():
    service, _config, _lifecycle = _control_service()
    long_value = "x" * 4097
    with pytest.raises(ConfigurationValidationError):
        service.update_config(
            ConfigUpdateRequest("ap", "Main", "General", "Text", long_value)
        )
    with pytest.raises(ConfigurationValidationError):
        service.update_config(
            ConfigUpdateRequest(
                "ap",
                "Main",
                "General",
                "Text",
                Decimal("1000000000001"),
            )
        )


def test_control_service_rejects_unscoped_adb_restart_before_adapter_call():
    instances = _Instances()
    adb = _Adb()
    metadata = _Metadata()
    service = GameControlService(
        instance_reader=instances,
        config_schema=_ConfigSchema(metadata.definitions),
        config_writer=_Config(),
        scheduler_tasks=_SchedulerTasks(metadata.tasks),
        lifecycle=_Lifecycle(),
        emulator=_Emulator(),
        adb=adb,
    )

    with pytest.raises(InvalidRequestError):
        service.restart_adb(None)  # type: ignore[arg-type]
    assert adb.calls == []


def test_control_service_fails_closed_for_invalid_config_and_state_results():
    service, _config, _lifecycle = _control_service()

    with pytest.raises(ConfigurationValidationError):
        service.update_config(ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 7))
    with pytest.raises(ConfigurationValidationError):
        service.update_config(ConfigUpdateRequest("ap", "Main", "General", "Mode", "unsafe"))
    with pytest.raises(ConfigurationValidationError):
        service.update_config(ConfigUpdateRequest("ap", "Main", "Error", "ApiKey", "new"))
    with pytest.raises(ResourceNotFoundError):
        service.update_config(
            ConfigUpdateRequest("ap", "Main", "Fleet", "Missing", 4)
        )
    with pytest.raises(ConfigurationValidationError):
        service.update_config(
            ConfigUpdateRequest(
                "ap",
                "Main",
                "Scheduler",
                "NextRun",
                "not-a-datetime",
            )
        )
    with pytest.raises(ResourceNotFoundError):
        service.trigger_task(ScheduleTaskRequest("ap", "Unknown"))

    class InvalidLifecycle(_Lifecycle):
        def is_running(self, instance: str) -> int:  # type: ignore[override]
            return 1

    invalid_service, _config, _lifecycle = _control_service(lifecycle=InvalidLifecycle())
    with pytest.raises(OperationFailedError):
        invalid_service.start_instance("ap")

    class StartPostconditionMismatch(_Lifecycle):
        def start_instance(self, instance: str) -> bool:
            self.calls.append("start")
            return True

    start_mismatch, _config, _lifecycle = _control_service(
        lifecycle=StartPostconditionMismatch()
    )
    with pytest.raises(PostconditionFailedError):
        start_mismatch.start_instance("ap")

    class StopPostconditionMismatch(_Lifecycle):
        def __init__(self) -> None:
            super().__init__()
            self.running = True

        def stop_instance(self, instance: str) -> bool:
            self.calls.append("stop")
            return True

    stop_mismatch, _config, _lifecycle = _control_service(
        lifecycle=StopPostconditionMismatch()
    )
    with pytest.raises(PostconditionFailedError):
        stop_mismatch.stop_instance("ap")


def test_control_service_preserves_valid_datetime_string_for_legacy_parser():
    service, config, _lifecycle = _control_service()
    value = "2026-08-31 12:00:00"

    service.update_config(
        ConfigUpdateRequest("ap", "Main", "Scheduler", "NextRun", value)
    )

    assert config.updated[-1].value == value


def test_config_validation_rejects_non_numeric_range_without_raw_type_error():
    definition = ConfigArgumentDefinition(
        task="Main",
        group="General",
        argument="Range",
        input_type="input",
        default="",
        validation=(1, 6),
    )

    with pytest.raises(ConfigurationValidationError, match="числовое"):
        validate_config_value(definition, "not-a-number")


def test_control_service_sanitizes_writer_failure_without_internal_details():
    class BrokenConfig(_Config):
        def update_config(self, request: ConfigUpdateRequest) -> None:
            raise RuntimeError("internal profile adapter detail")

    service, _config, _lifecycle = _control_service(config=BrokenConfig())
    with pytest.raises(OperationFailedError) as failure:
        service.update_config(ConfigUpdateRequest("ap", "Main", "Fleet", "Count", 2))
    assert "internal" not in str(failure.value)
    assert "detail" not in str(failure.value)


def test_control_service_sanitizes_clock_failure():
    def broken_clock() -> datetime:
        raise RuntimeError("internal clock adapter detail")

    service, _config, _lifecycle = _control_service(clock=broken_clock)

    with pytest.raises(OperationFailedError) as failure:
        service.trigger_task(ScheduleTaskRequest("ap", "Event"))
    assert "internal" not in str(failure.value)
    assert "detail" not in str(failure.value)
