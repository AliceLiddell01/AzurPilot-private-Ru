from __future__ import annotations

import builtins
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest

from module.application import (
    REDACTED_CONFIG_VALUE,
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
    OperationFailedError,
    OwnershipAmbiguousError,
    PostconditionFailedError,
    host_lock,
    legacy_game_adapters,
)
from module.application.game_models import MediaFrame
from module.application.legacy_adapters import GeneratedTaskCatalogAdapter
from module.application.runtime_control import RuntimeControlError
from module.application.legacy_game_adapters import (
    LegacyAdbAdapter,
    LegacyConfigAdapter,
    LegacyEmulatorAdapter,
    LegacyProcessManagerAdapter,
    LegacyRuntimeLogAdapter,
    LegacyScreenshotAdapter,
)

ARGS = {
    "Main": {
        "General": {
            "Count": {"type": "input", "value": 1, "validate": [1, 6]},
            "Secret": {"type": "textarea", "value": "", "sensitive": True},
        },
        "Scheduler": {
            "Command": {"value": "Main"},
            "NextRun": {
                "type": "datetime",
                "value": "2020-01-01 00:00:00",
                "validate": "datetime",
            },
        },
        "Storage": {"Internal": {"type": "storage", "value": {}}},
    },
    "OpsiGeneral": {
        "OpsiGeneral": {
            "OpsiOnePushConfig": {
                "type": "textarea",
                "value": "provider: null",
                "sensitive": True,
            },
        },
    },
    "Event": {
        "Scheduler": {"Command": {"value": "Event"}},
    },
}
I18N = {
    "Task": {"Main": {"name": "Главная", "help": "help"}},
    "Main": {"General": {"_info": {"name": "Общее"}}},
    "Gui": {"Dashboard": {"Oil": "Нефть"}},
}


class _Updater:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    def read_file(self, config_name: str) -> dict[str, object]:
        return self.data


class _Config:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.auto_update = True
        self.changes: list[tuple[str, object]] = []
        self.update_calls = 0

    def cross_set(self, path: str, value: object) -> None:
        self.changes.append((path, value))

    def update(self) -> None:
        self.update_calls += 1


def test_generated_metadata_defines_scheduler_and_sensitive_config_boundaries():
    metadata = GeneratedTaskCatalogAdapter(ARGS, I18N)

    assert metadata.list_schedulable_task_names() == ("Main", "Event")
    definition = metadata.read_argument_definition("Main", "General", "Count")
    assert definition == ConfigArgumentDefinition(
        "Main", "General", "Count", "input", 1, validation=(1, 6)
    )
    assert metadata.read_argument_definition("Main", "Storage", "Internal") is None
    secret = metadata.read_argument_definition("Main", "General", "Secret")
    assert secret is not None and secret.sensitive is True

    redacted = metadata.redact_config(
        {
            "Main": {"General": {"Secret": "token", "Count": 2}},
            "OpsiGeneral": {
                "OpsiGeneral": {"OpsiOnePushConfig": "token"},
            },
        }
    )
    assert redacted["Main"]["General"]["Secret"] == REDACTED_CONFIG_VALUE  # type: ignore[index]
    assert (
        redacted["OpsiGeneral"]["OpsiGeneral"]["OpsiOnePushConfig"]
        == REDACTED_CONFIG_VALUE
    )  # type: ignore[index]


def test_legacy_config_adapter_reads_redacted_data_and_limits_scheduler_mutation():
    data = {
        "Main": {
            "General": {"Secret": "token", "Count": 1},
            "Scheduler": {"Enable": True, "NextRun": datetime(2026, 8, 31, 12, tzinfo=UTC)},
        },
        "Event": {
            "Scheduler": {"Enable": False, "NextRun": datetime(2026, 8, 31, 13, tzinfo=UTC)},
        },
        "DisabledMissing": {"Scheduler": {"Enable": False}},
        "Other": {"Scheduler": {"Enable": True}},
        "Dashboard": {"Oil": {"Value": 10, "Limit": 100, "Record": datetime(2026, 8, 31, tzinfo=UTC)}},
    }
    updater = _Updater(data)
    configs: list[_Config] = []

    def config_factory(instance: str) -> _Config:
        config = _Config(data)
        configs.append(config)
        return config

    adapter = LegacyConfigAdapter(
        GeneratedTaskCatalogAdapter(ARGS, I18N),
        updater_factory=lambda: updater,
        config_factory=config_factory,
    )

    snapshot = adapter.read_config("ap")
    assert snapshot["Main"]["General"]["Secret"] == REDACTED_CONFIG_VALUE  # type: ignore[index]
    assert adapter.read_config("ap", "Main")["General"]["Count"] == 1  # type: ignore[index]
    queue = adapter.read_scheduler_queue("ap", ("Main", "Event", "DisabledMissing"))
    assert tuple(item.task for item in queue) == ("Main",)
    resources = adapter.read_resources("ap")
    assert resources.items[0].label == "Нефть"

    adapter.update_config(ConfigUpdateRequest("ap", "Main", "General", "Count", 2))
    assert configs[-1].changes == [("Main.General.Count", 2)]
    assert configs[-1].update_calls == 1

    adapter.update_config(
        ConfigUpdateRequest(
            "ap",
            "Main",
            "Scheduler",
            "NextRun",
            "2026-08-31 14:00:00+00:00",
        )
    )
    assert configs[-1].changes == [
        ("Main.Scheduler.NextRun", datetime(2026, 8, 31, 14, tzinfo=UTC))
    ]

    adapter.schedule_task("ap", "Main", datetime(2026, 8, 31, 14, tzinfo=UTC))
    assert configs[-1].changes == [
        ("Main.Scheduler.Enable", True),
        ("Main.Scheduler.NextRun", datetime(2026, 8, 31, 14, tzinfo=UTC)),
    ]

    assert adapter.clear_scheduler_queue("ap", ("Main", "Event")) == ("Main",)
    assert configs[-1].changes == [("Main.Scheduler.Enable", False)]


def test_legacy_config_adapter_sorts_scheduler_datetimes_by_timestamp():
    data = {
        "Main": {
            "Scheduler": {
                "Enable": True,
                "NextRun": datetime(
                    2026,
                    8,
                    31,
                    12,
                    tzinfo=timezone(timedelta(hours=3)),
                ),
            },
        },
        "Event": {
            "Scheduler": {
                "Enable": True,
                "NextRun": datetime(2026, 8, 31, 10, tzinfo=UTC),
            },
        },
    }
    adapter = LegacyConfigAdapter(
        GeneratedTaskCatalogAdapter(ARGS, I18N),
        updater_factory=lambda: _Updater(data),
    )

    queue = adapter.read_scheduler_queue("ap", ("Main", "Event"))

    assert tuple(item.task for item in queue) == ("Main", "Event")


def test_legacy_log_adapter_is_bounded_and_root_safe(tmp_path: Path):
    log_root = tmp_path / "log"
    log_root.mkdir()
    log_file = log_root / "2026-08-31_ap.txt"
    log_file.write_text(
        "old\n<<< Run task Main >>>\nnew\n<<< Run task Event >>>\n",
        encoding="utf-8",
    )
    adapter = LegacyRuntimeLogAdapter(log_root, date_provider=lambda: date(2026, 8, 31))

    assert adapter.read_tail("ap", 2) == ("new\n", "<<< Run task Event >>>\n")
    assert adapter.read_tail("ap", 0) == ()
    assert adapter.read_current_task("ap") == "Event"
    assert adapter.read_current_task("secondary") == "Unknown"
    log_file.write_bytes(b"x" * (2 * 1024 * 1024) + b"\nlast\n")
    assert adapter.read_tail("ap", 1) == ("last\n",)
    with pytest.raises(ValueError):
        adapter.read_tail("../ap", 2)


def test_legacy_log_adapter_falls_back_to_previous_calendar_date(tmp_path: Path):
    log_root = tmp_path / "log"
    log_root.mkdir()
    (log_root / "2026-08-30_ap.txt").write_text(
        "<<< Run task Main >>>\n",
        encoding="utf-8",
    )
    adapter = LegacyRuntimeLogAdapter(
        log_root,
        date_provider=lambda: date(2026, 8, 31),
    )

    assert adapter.read_current_task("ap") == "Main"


def test_legacy_screenshot_lifecycle_and_emulator_adapters_use_narrow_owners(monkeypatch):
    calls: list[tuple[str, ...]] = []
    frame = b"\x89PNG\r\n\x1a\nframe"

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv == ("adb", "devices"):
            return _CommandResult(0, b"List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0, frame)

    screenshot = LegacyScreenshotAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    monkeypatch.delenv("ALAS_CONFIG_NAME", raising=False)
    assert screenshot.read_frame("secondary") == MediaFrame(frame, "image/png")
    assert calls == [
        ("adb", "devices"),
        ("adb", "-s", "serial-a", "exec-out", "screencap", "-p"),
    ]
    assert "ALAS_CONFIG_NAME" not in os.environ

    class Manager:
        def __init__(self) -> None:
            self.alive = False
            self.calls: list[str] = []
            self.start_context: list[tuple[str | None, str | None]] = []

        def start(
            self,
            *,
            func: str,
            operation_id: str | None = None,
            session_id: str | None = None,
        ) -> None:
            self.calls.append(func)
            self.start_context.append((operation_id, session_id))
            self.alive = True

        def stop(self) -> bool:
            self.calls.append("stop")
            self.alive = False
            return True

    manager = Manager()
    monkeypatch.setenv("AZURPILOT_RUNTIME_OPERATION_ID", "operation-env")
    monkeypatch.setenv("AZURPILOT_DEV_SESSION_ID", "session-env")
    lifecycle = LegacyProcessManagerAdapter(
        manager_factory=lambda instance: manager,
        function_factory=lambda instance: "Main",
    )
    assert lifecycle.start_instance("secondary") is True
    assert lifecycle.stop_instance("secondary") is True
    assert manager.calls == ["Main", "stop"]
    assert manager.start_context == [("operation-env", "session-env")]

    events: list[str] = []

    class Platform:
        def __init__(self) -> None:
            self.running = True

        def is_emulator_instance_running(self) -> bool:
            return self.running

        def emulator_stop(self) -> bool:
            events.append("stop")
            self.running = False
            return True

        def emulator_start(self) -> bool:
            events.append("start")
            self.running = True
            return True

    assert (
        LegacyEmulatorAdapter(platform_factory=lambda instance: Platform()).restart_emulator(
            "secondary"
        )
        is True
    )
    assert events == ["stop", "start"]


def test_legacy_process_manager_translates_direct_control_errors() -> None:
    class FailingControl:
        def call(self, *args: object, **kwargs: object) -> object:
            raise RuntimeControlError("RUNTIME_OWNER_UNAVAILABLE", "owner недоступен")

    for method_name in ("start_instance", "stop_instance"):
        lifecycle = LegacyProcessManagerAdapter(control_client=FailingControl())
        with pytest.raises(OwnershipAmbiguousError, match="owner недоступен"):
            getattr(lifecycle, method_name)("secondary")


def test_typed_emulator_failures_distinguish_ownership_operation_and_postcondition(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )

    class NoChecker:
        def emulator_stop(self) -> bool:
            raise AssertionError("операция не должна начинаться без ownership")

        def emulator_start(self) -> bool:
            raise AssertionError("операция не должна начинаться без ownership")

    with pytest.raises(OwnershipAmbiguousError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: NoChecker(),
            typed_failures=True,
        ).restart_emulator("secondary")

    class StuckAfterStop:
        def __init__(self, *, force_result: bool = True, stop_on_force: bool = True) -> None:
            self.calls: list[str] = []
            self.running = True
            self.force_result = force_result
            self.stop_on_force = stop_on_force

        def is_emulator_instance_running(self) -> bool:
            return self.running

        def emulator_stop(self) -> bool:
            self.calls.append("stop")
            return True

        def emulator_force_stop_instance(self) -> bool:
            self.calls.append("force")
            if self.stop_on_force:
                self.running = False
            return self.force_result

        def emulator_start(self) -> bool:
            self.calls.append("start")
            self.running = True
            return True

    stuck = StuckAfterStop()
    assert (
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: stuck,
            typed_failures=True,
        ).restart_emulator("secondary")
        is True
    )
    assert stuck.calls == ["stop", "force", "start"]

    class FailedStart:
        def __init__(self) -> None:
            self.running = True

        def is_emulator_instance_running(self) -> bool:
            return self.running

        def emulator_stop(self) -> bool:
            self.running = False
            return True

        def emulator_start(self) -> bool:
            return False

    with pytest.raises(OperationFailedError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: FailedStart(),
            typed_failures=True,
        ).restart_emulator("secondary")


def test_emulator_restart_does_not_force_kill_after_delayed_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    states = iter((True, True, False, True))
    calls: list[str] = []

    class Platform:
        def is_emulator_instance_running(self) -> bool:
            return next(states)

        def emulator_stop(self) -> bool:
            calls.append("stop")
            return False

        def emulator_force_stop_instance(self) -> bool:
            calls.append("force")
            return True

        def emulator_start(self) -> bool:
            calls.append("start")
            return True

    assert LegacyEmulatorAdapter(
        platform_factory=lambda instance: Platform(),
        typed_failures=True,
    ).restart_emulator("secondary") is True
    assert calls == ["stop", "start"]


def test_emulator_restart_uses_authoritative_state_when_force_command_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    platform_state = {"running": True}
    calls: list[str] = []

    class Platform:
        def is_emulator_instance_running(self) -> bool:
            return platform_state["running"]

        def emulator_stop(self) -> bool:
            calls.append("stop")
            return True

        def emulator_force_stop_instance(self) -> bool:
            calls.append("force")
            platform_state["running"] = False
            return False

        def emulator_start(self) -> bool:
            calls.append("start")
            platform_state["running"] = True
            return True

    assert LegacyEmulatorAdapter(
        platform_factory=lambda instance: Platform(),
        typed_failures=True,
    ).restart_emulator("secondary") is True
    assert calls == ["stop", "force", "start"]


def test_emulator_restart_blocks_start_when_force_escalation_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    calls: list[str] = []

    class Platform:
        def is_emulator_instance_running(self) -> bool:
            return True

        def emulator_stop(self) -> bool:
            calls.append("stop")
            return True

        def emulator_force_stop_instance(self) -> bool:
            calls.append("force")
            return False

        def emulator_start(self) -> bool:
            calls.append("start")
            return True

    with pytest.raises(PostconditionFailedError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: Platform(),
            typed_failures=True,
        ).restart_emulator("secondary")
    assert calls == ["stop", "force"]


def test_emulator_restart_rejects_launch_false_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    calls: list[str] = []

    class Platform:
        def is_emulator_instance_running(self) -> bool:
            return False

        def emulator_stop(self) -> bool:
            calls.append("stop")
            return True

        def emulator_start(self) -> bool:
            calls.append("start")
            return True

    with pytest.raises(PostconditionFailedError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: Platform(),
            typed_failures=True,
        ).restart_emulator("secondary")
    assert calls == ["stop", "start"]


def test_emulator_restart_fails_closed_when_ownership_becomes_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_game_adapters, "_EMULATOR_STATE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_EMULATOR_STATE_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    calls: list[str] = []
    states = iter((True, None, None, None))

    class Platform:
        def is_emulator_instance_running(self) -> object:
            return next(states)

        def emulator_stop(self) -> bool:
            calls.append("stop")
            return True

        def emulator_force_stop_instance(self) -> bool:
            calls.append("force")
            return True

        def emulator_start(self) -> bool:
            calls.append("start")
            return True

    with pytest.raises(OwnershipAmbiguousError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: Platform(),
            typed_failures=True,
        ).restart_emulator("secondary")
    assert calls == ["stop"]


def test_emulator_restart_polls_until_stop_and_start_states_are_confirmed():
    states = iter((True, True, False, False, True))

    class Platform:
        def emulator_stop(self) -> bool:
            return True

        def emulator_start(self) -> bool:
            return True

        def is_emulator_instance_running(self) -> bool:
            return next(states)

    assert LegacyEmulatorAdapter(
        platform_factory=lambda instance: Platform(),
        typed_failures=True,
    ).restart_emulator("secondary") is True


def test_typed_emulator_failures_sanitize_platform_operation_errors():
    class ExplodingStop:
        def is_emulator_instance_running(self) -> bool:
            return True

        def emulator_stop(self) -> bool:
            raise RuntimeError("platform detail")

    with pytest.raises(OperationFailedError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: ExplodingStop(),
            typed_failures=True,
        ).restart_emulator("secondary")

    class MissingStart:
        def __init__(self) -> None:
            self.states = iter((True, False))

        def is_emulator_instance_running(self) -> bool:
            return next(self.states)

        def emulator_stop(self) -> bool:
            return True

    with pytest.raises(OperationFailedError):
        LegacyEmulatorAdapter(
            platform_factory=lambda instance: MissingStart(),
            typed_failures=True,
        ).restart_emulator("secondary")


def test_emulator_restart_serializes_with_passive_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_runtime = tmp_path / "host-runtime"
    monkeypatch.setattr(host_lock, "host_runtime_root", lambda: host_runtime)
    screenshot_entered = Event()
    release_screenshot = Event()
    screenshot_errors: list[BaseException] = []
    emulator_errors: list[BaseException] = []
    emulator_done = Event()

    def screenshot_runner(argv: tuple[str, ...]) -> _CommandResult:
        if argv == ("adb", "devices"):
            screenshot_entered.set()
            assert release_screenshot.wait(timeout=5)
            return _CommandResult(0, "List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0, b"\x89PNG\r\n\x1a\nframe")

    class Platform:
        def __init__(self) -> None:
            self.running = True

        def is_emulator_instance_running(self) -> bool:
            return self.running

        def emulator_stop(self) -> bool:
            self.running = False
            return True

        def emulator_start(self) -> bool:
            self.running = True
            return True

    screenshot = LegacyScreenshotAdapter(
        runner=screenshot_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    emulator = LegacyEmulatorAdapter(
        platform_factory=lambda instance: Platform(),
        typed_failures=True,
    )

    def read_screenshot() -> None:
        try:
            screenshot.read_frame("secondary")
        except BaseException as error:  # noqa: BLE001 - передача ошибки из тестового потока.
            screenshot_errors.append(error)

    def restart_emulator() -> None:
        try:
            assert emulator.restart_emulator("secondary") is True
        except BaseException as error:  # noqa: BLE001 - передача ошибки из тестового потока.
            emulator_errors.append(error)
        finally:
            emulator_done.set()

    screenshot_thread = Thread(target=read_screenshot)
    emulator_thread = Thread(target=restart_emulator)
    screenshot_thread.start()
    assert screenshot_entered.wait(timeout=5)
    emulator_thread.start()
    assert not emulator_done.wait(timeout=0.1)
    release_screenshot.set()
    screenshot_thread.join(timeout=5)
    emulator_thread.join(timeout=5)

    assert not screenshot_thread.is_alive()
    assert not emulator_thread.is_alive()
    assert screenshot_errors == []
    assert emulator_errors == []


def test_host_lock_releases_process_lock_when_os_unlock_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    lock_path = tmp_path / "host.lock"
    real_release = host_lock._release_os_lock

    def broken_release(handle: object) -> None:
        raise OSError("simulated unlock failure")

    monkeypatch.setattr(host_lock, "_release_os_lock", broken_release)
    with pytest.raises(OSError, match="simulated unlock failure"), host_lock.application_host_lock(
        lock_path
    ):
        pass

    monkeypatch.setattr(host_lock, "_release_os_lock", real_release)
    with host_lock.application_host_lock(lock_path):
        pass


def test_legacy_screenshot_is_passive_and_unavailable_path_does_not_recover(monkeypatch):
    calls: list[tuple[str, ...]] = []
    mutations: list[str] = []
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {
            "module.config.config",
            "module.device.device",
            "module.webui.fake_pil_module",
        }:
            mutations.append(f"import:{name}")
            raise AssertionError("пассивный screenshot импортировал control path")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if any(
            command in argv
            for command in ("kill-server", "start-server", "input", "emulator")
        ):
            mutations.append("control command")
            raise AssertionError("пассивный screenshot вызвал control command")
        if argv[-1] == "devices":
            return _CommandResult(0, b"List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0, b"\x89PNG\r\n\x1a\nframe")

    screenshot = LegacyScreenshotAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: None,
    )

    assert screenshot.read_frame("secondary") == MediaFrame(
        b"\x89PNG\r\n\x1a\nframe", "image/png"
    )
    assert calls == [
        ("adb", "devices"),
        ("adb", "-s", "serial-a", "exec-out", "screencap", "-p"),
    ]
    assert mutations == []

    unavailable_calls: list[tuple[str, ...]] = []

    def unavailable_runner(argv: tuple[str, ...]) -> _CommandResult:
        unavailable_calls.append(argv)
        return _CommandResult(1, b"")

    unavailable = LegacyScreenshotAdapter(
        runner=unavailable_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: None,
    )
    with pytest.raises(OSError):
        unavailable.read_frame("secondary")
    assert unavailable_calls == [("adb", "devices")]
    assert mutations == []

    def invalid_runner(argv: tuple[str, ...]) -> _CommandResult:
        if argv == ("adb", "devices"):
            return _CommandResult(0, b"List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0, b"not a png")

    invalid_frame = LegacyScreenshotAdapter(
        runner=invalid_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    with pytest.raises(OSError, match="PNG"):
        invalid_frame.read_frame("secondary")


def test_legacy_adb_restart_serializes_with_passive_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_runtime = tmp_path / "host-runtime"
    monkeypatch.setattr(host_lock, "host_runtime_root", lambda: host_runtime)
    screenshot_entered = Event()
    release_screenshot = Event()
    screenshot_errors: list[BaseException] = []
    restart_errors: list[BaseException] = []
    restart_done = Event()

    def screenshot_runner(argv: tuple[str, ...]) -> _CommandResult:
        if argv == ("adb", "devices"):
            screenshot_entered.set()
            assert release_screenshot.wait(timeout=5)
            return _CommandResult(0, "List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0, b"\x89PNG\r\n\x1a\nframe")

    def restart_runner(argv: tuple[str, ...]) -> _CommandResult:
        if argv[-1] == "devices":
            return _CommandResult(0, "List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0)

    screenshot = LegacyScreenshotAdapter(
        runner=screenshot_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    restart = LegacyAdbAdapter(
        runner=restart_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )

    def read_screenshot() -> None:
        try:
            screenshot.read_frame("secondary")
        except BaseException as error:  # noqa: BLE001 - передача ошибки из тестового потока.
            screenshot_errors.append(error)

    def restart_adb() -> None:
        try:
            assert restart.restart_adb("secondary") is True
        except BaseException as error:  # noqa: BLE001 - передача ошибки из тестового потока.
            restart_errors.append(error)
        finally:
            restart_done.set()

    screenshot_thread = Thread(target=read_screenshot)
    restart_thread = Thread(target=restart_adb)
    screenshot_thread.start()
    assert screenshot_entered.wait(timeout=5)
    restart_thread.start()
    assert not restart_done.wait(timeout=0.1)
    release_screenshot.set()
    screenshot_thread.join(timeout=5)
    restart_thread.join(timeout=5)

    assert not screenshot_thread.is_alive()
    assert not restart_thread.is_alive()
    assert screenshot_errors == []
    assert restart_errors == []


def test_passive_screenshot_resolves_only_canonical_emulator_aliases():
    calls: list[tuple[str, ...]] = []
    frame = b"\x89PNG\r\n\x1a\nframe"

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv == ("adb", "devices"):
            return _CommandResult(
                0,
                "List of devices attached\nemulator-5560\tdevice\n",
            )
        return _CommandResult(0, frame)

    screenshot = LegacyScreenshotAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "127.0.0.1:16448",
        target_serial_aliases_provider=lambda serial: (
            serial,
            " ",
            "127.0.0.1:5561",
            "emulator-5560",
        ),
    )

    assert screenshot.read_frame("secondary") == MediaFrame(frame, "image/png")
    assert calls == [
        ("adb", "devices"),
        ("adb", "-s", "emulator-5560", "exec-out", "screencap", "-p"),
    ]


def test_passive_screenshot_does_not_singleton_fallback_for_explicit_target():
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        return _CommandResult(0, "List of devices attached\nforeign\tdevice\n")

    screenshot = LegacyScreenshotAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "configured-target",
        target_serial_aliases_provider=lambda serial: (),
    )

    with pytest.raises(OSError, match="Настроенный ADB target"):
        screenshot.read_frame("secondary")
    assert calls == [("adb", "devices")]


def test_passive_screenshot_bounds_alias_discovery_cache(monkeypatch):
    calls: list[tuple[str, ...]] = []
    alias_calls: list[str] = []
    clock = [100.0]

    monkeypatch.setattr(legacy_game_adapters, "monotonic", lambda: clock[0])

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv == ("adb", "devices"):
            return _CommandResult(
                0,
                "List of devices attached\nemulator-5560\tdevice\n",
            )
        return _CommandResult(0, b"\x89PNG\r\n\x1a\nframe")

    def aliases_provider(serial: str) -> tuple[str, ...]:
        alias_calls.append(serial)
        return (serial, "emulator-5560")

    screenshot = LegacyScreenshotAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "127.0.0.1:16416",
        target_serial_aliases_provider=aliases_provider,
    )

    screenshot.read_frame("ap")
    screenshot.read_frame("ap")
    assert alias_calls == ["127.0.0.1:16416"]
    assert calls.count(("adb", "devices")) == 2

    clock[0] += legacy_game_adapters._PASSIVE_EMULATOR_ALIASES_CACHE_TTL_SECONDS
    screenshot.read_frame("ap")
    assert alias_calls == ["127.0.0.1:16416", "127.0.0.1:16416"]
    assert calls.count(("adb", "devices")) == 3


@pytest.mark.skipif(os.name != "nt", reason="Проверяется Windows vbox/nemu parser")
def test_mumu_vbox_parser_exposes_all_adb_forwarding_aliases(tmp_path: Path):
    from module.device.platform.emulator_windows import Emulator

    vbox_file = tmp_path / "instance.nemu"
    vbox_file.write_text(
        '<Forwarding name="ADB_PORT" hostport="16448" guestport="5555"/>\n'
        '<Forwarding name="ADB_PORT_EX" hostport="5561" guestport="5555"/>\n'
        '<Forwarding name="ADB_PORT_OLD" hostport="7555" guestport="5555"/>\n',
        encoding="utf-8",
    )

    assert Emulator.vbox_file_to_serials(str(vbox_file)) == (
        "127.0.0.1:16448",
        "127.0.0.1:5561",
        "emulator-5560",
        "127.0.0.1:7555",
    )


def test_passive_screenshot_bounds_large_png_in_memory(monkeypatch):
    from io import BytesIO

    from PIL import Image

    image = Image.effect_noise((512, 512), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="PNG")
    source_data = source.getvalue()
    monkeypatch.setattr(
        legacy_game_adapters,
        "_PASSIVE_SCREENSHOT_MAX_BYTES",
        len(source_data) // 2,
    )

    bounded, media_type = legacy_game_adapters._bound_passive_screenshot(source_data)

    assert media_type == "image/jpeg"
    assert len(bounded) <= len(source_data) // 2
    with Image.open(BytesIO(bounded)) as decoded:
        assert decoded.size == image.size


def test_passive_adb_discovery_is_independent_of_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    process_cwd = tmp_path / "caller"
    adb = repository_root / ".venv" / "Scripts" / "adb.exe"
    adb.parent.mkdir(parents=True)
    process_cwd.mkdir()
    adb.write_bytes(b"adb")

    monkeypatch.setattr(legacy_game_adapters, "_REPOSITORY_ROOT", repository_root)
    monkeypatch.setattr(
        legacy_game_adapters,
        "_ADB_PATH_CANDIDATES",
        (Path(".venv/Scripts/adb.exe"),),
    )
    monkeypatch.chdir(process_cwd)

    assert legacy_game_adapters._find_passive_adb_path() == str(adb.resolve())


def test_adb_host_lock_is_scoped_by_server_endpoint_not_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_runtime = tmp_path / "host-runtime"
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(host_lock, "host_runtime_root", lambda: host_runtime)
    monkeypatch.setattr(legacy_game_adapters, "_REPOSITORY_ROOT", checkout)

    primary = "tcp:127.0.0.1:5037"
    secondary = "tcp:127.0.0.1:5038"
    primary_path = legacy_game_adapters._adb_host_lock_path(primary)
    secondary_path = legacy_game_adapters._adb_host_lock_path(secondary)

    assert primary_path != secondary_path
    assert str(checkout) not in str(primary_path)
    with (
        legacy_game_adapters._adb_host_lock(primary),
        legacy_game_adapters._adb_host_lock(secondary),
    ):
        pass
    assert host_runtime.is_dir()
    if os.name != "nt":
        assert host_runtime.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name == "nt", reason="Проверяется POSIX-защита ссылок")
@pytest.mark.parametrize("symlink_scope", ("root", "ancestor"))
def test_adb_host_lock_rejects_symlinked_runtime_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_scope: str,
) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    if symlink_scope == "root":
        host_runtime = tmp_path / "host-runtime"
        host_runtime.symlink_to(real_root, target_is_directory=True)
    else:
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_root, target_is_directory=True)
        host_runtime = linked_parent / "host-runtime"
    monkeypatch.setattr(host_lock, "host_runtime_root", lambda: host_runtime)

    with pytest.raises(OSError, match="ссылкой"), legacy_game_adapters._adb_host_lock(
        "tcp:127.0.0.1:5037"
    ):
        pass

    assert not (real_root / "host-runtime").exists()


def test_adb_server_identity_prefers_explicit_socket_over_tcp_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "10.0.0.2")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "5038")
    assert legacy_game_adapters._adb_server_identity() == "tcp:10.0.0.2:5038"

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.2:5038")
    assert legacy_game_adapters._adb_server_identity() == "tcp:10.0.0.2:5038"

    monkeypatch.setenv("ADB_SERVER_SOCKET", "local:/tmp/adb.sock")
    assert legacy_game_adapters._adb_server_identity() == "socket:local:/tmp/adb.sock"


def test_adb_discovery_prioritizes_supplied_root_before_candidate_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured_root = tmp_path / "configured"
    repository_root = tmp_path / "repository"
    configured_adb = configured_root / "bin" / "adb" / "adb"
    repository_adb = repository_root / ".venv" / "Scripts" / "adb.exe"
    configured_adb.parent.mkdir(parents=True)
    repository_adb.parent.mkdir(parents=True)
    configured_adb.write_bytes(b"configured")
    repository_adb.write_bytes(b"repository")

    monkeypatch.setattr(legacy_game_adapters, "_REPOSITORY_ROOT", repository_root)

    assert legacy_game_adapters._first_existing_adb_path(configured_root) == str(
        configured_adb.resolve()
    )


@pytest.mark.skipif(os.name != "nt", reason="Проверяется Windows MuMu instance discovery")
def test_mumu_instance_alias_discovery_scans_all_nemu_files(tmp_path: Path) -> None:
    from module.device.platform.emulator_windows import EmulatorInstance

    emulator_root = tmp_path / "mumu"
    emulator_path = emulator_root / "nx_main" / "MuMuNxMain.exe"
    vms_folder = emulator_root / "vms" / "MuMuPlayerGlobal-15.0-1"
    vms_folder.mkdir(parents=True)
    (vms_folder / "renamed-instance.nemu").write_text(
        '<Forwarding name="ADB_PORT" hostport="16416" guestport="5555"/>\n'
        '<Forwarding name="ADB_PORT_EX" hostport="5557" guestport="5555"/>\n'
        '<Forwarding name="ADB_PORT_OLD" hostport="7555" guestport="5555"/>\n',
        encoding="utf-8",
    )

    instance = EmulatorInstance(
        serial="127.0.0.1:16416",
        name="MuMuPlayerGlobal-15.0-1",
        path=str(emulator_path),
    )

    assert instance.adb_serials == (
        "127.0.0.1:16416",
        "127.0.0.1:5557",
        "emulator-5556",
        "127.0.0.1:7555",
    )


@dataclass
class _CommandResult:
    returncode: int
    stdout: str | bytes = ""


_EXPECTED_RESTART_CALLS = (
    ("adb", "devices"),
    ("adb", "kill-server"),
    ("adb", "start-server"),
    ("adb", "devices"),
)


def _make_adb_adapter(
    *inventories: str,
    target_serial: str | None = "serial-a",
    typed_failures: bool = False,
) -> tuple[LegacyAdbAdapter, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    inventory_iter = iter(inventories)
    last_inventory: str | None = None

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv[-1] == "devices":
            nonlocal last_inventory
            inventory = next(inventory_iter, last_inventory)
            assert inventory is not None, "адаптер запросил инвентарь лишний раз"
            last_inventory = inventory
            return _CommandResult(0, inventory)
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: target_serial,
        typed_failures=typed_failures,
    )
    return adapter, calls


@pytest.fixture
def fast_adb_restart_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        legacy_game_adapters,
        "_ADB_RESTART_READY_RETRY_INTERVAL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(legacy_game_adapters, "_ADB_RESTART_READY_MAX_ATTEMPTS", 2)


def test_legacy_adb_adapter_preserves_device_state_in_inventory():
    parsed = LegacyAdbAdapter._parse_devices(
        _CommandResult(0, "List of devices attached\nserial-a\toffline\n")
    )

    assert parsed is not None
    assert parsed[0].serial == "serial-a"
    assert parsed[0].state == "offline"


def test_legacy_adb_adapter_confirms_offline_target_recovers_to_device():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\toffline\n",
        "List of devices attached\nserial-a\tdevice\n",
    )

    assert adapter.restart_adb("secondary") is True
    assert tuple(calls) == _EXPECTED_RESTART_CALLS


def test_legacy_adb_adapter_polls_until_target_is_ready(fast_adb_restart_polling):
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-a\toffline\n",
        "List of devices attached\nserial-a\tdevice\n",
    )

    assert adapter.restart_adb("secondary") is True
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
        ("adb", "devices"),
    ]


def test_legacy_adb_adapter_rejects_offline_target_that_stays_offline(
    fast_adb_restart_polling,
):
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\toffline\n",
        "List of devices attached\nserial-a\toffline\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls[:4]) == _EXPECTED_RESTART_CALLS
    assert len(calls) > len(_EXPECTED_RESTART_CALLS)


def test_legacy_adb_adapter_rejects_unauthorized_post_restart_state(
    fast_adb_restart_polling,
):
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-a\tunauthorized\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls[:4]) == _EXPECTED_RESTART_CALLS
    assert len(calls) > len(_EXPECTED_RESTART_CALLS)


def test_legacy_adb_adapter_rejects_target_that_disappears_after_restart(
    fast_adb_restart_polling,
):
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls[:4]) == _EXPECTED_RESTART_CALLS
    assert len(calls) > len(_EXPECTED_RESTART_CALLS)


def test_legacy_adb_adapter_rejects_unsupported_state_before_restart():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tunknown\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert calls == [("adb", "devices")]


def test_legacy_adb_adapter_rejects_malformed_inventory_before_restart():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert calls == [("adb", "devices")]


def test_legacy_adb_adapter_requires_explicit_target_only_inventory():
    foreign, foreign_calls = _make_adb_adapter(
        "List of devices attached\nforeign\tdevice\n",
    )
    assert foreign.restart_adb("secondary") is False
    assert foreign_calls == [("adb", "devices")]

    multiple, multiple_calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\nforeign\tdevice\n",
    )
    assert multiple.restart_adb("secondary") is False
    assert multiple_calls == [("adb", "devices")]

    missing, missing_calls = _make_adb_adapter(
        "List of devices attached\n",
    )
    assert missing.restart_adb("secondary") is False
    assert missing_calls == [("adb", "devices")]


def test_legacy_adb_adapter_allows_only_singleton_auto_target():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\toffline\n",
        "List of devices attached\nserial-a\tdevice\n",
        target_serial=None,
    )

    assert adapter.restart_adb("secondary") is True
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
    ]


def test_legacy_adb_adapter_rejects_multiple_devices_for_auto_target():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\nserial-b\tdevice\n",
        target_serial=None,
    )

    assert adapter.restart_adb("secondary") is False
    assert calls == [("adb", "devices")]


def test_legacy_adb_adapter_rejects_changed_explicit_target_inventory(
    fast_adb_restart_polling,
):
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-b\tdevice\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls[:4]) == _EXPECTED_RESTART_CALLS
    assert len(calls) > len(_EXPECTED_RESTART_CALLS)


def test_typed_adb_failures_distinguish_ownership_and_postcondition(
    fast_adb_restart_polling,
):
    ambiguous, ambiguous_calls = _make_adb_adapter(
        "List of devices attached\nforeign\tdevice\n",
        typed_failures=True,
    )
    with pytest.raises(OwnershipAmbiguousError):
        ambiguous.restart_adb("secondary")
    assert ambiguous_calls == [("adb", "devices")]

    postcondition, postcondition_calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-b\tdevice\n",
        typed_failures=True,
    )
    with pytest.raises(PostconditionFailedError):
        postcondition.restart_adb("secondary")
    assert tuple(postcondition_calls[:4]) == _EXPECTED_RESTART_CALLS
    assert len(postcondition_calls) > len(_EXPECTED_RESTART_CALLS)


def test_legacy_adb_adapter_rejects_unscoped_restart_without_touching_adb():
    calls: list[tuple[str, ...]] = []

    adapter = LegacyAdbAdapter(
        runner=lambda argv: calls.append(tuple(argv)) or _CommandResult(0),
        adb_path_provider=lambda: "adb",
    )

    assert adapter.restart_adb() is False
    assert calls == []
