from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from module.application import (
    REDACTED_CONFIG_VALUE,
    ConfigArgumentDefinition,
    ConfigUpdateRequest,
)
from module.application.game_models import MediaFrame
from module.application.legacy_adapters import GeneratedTaskCatalogAdapter
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
    queue = adapter.read_scheduler_queue("ap", ("Main", "Event"))
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
    class Device:
        def screenshot(self) -> object:
            return "frame"

    screenshot = LegacyScreenshotAdapter(
        device_factory=lambda instance: Device(),
        frame_encoder=lambda image: b"encoded",
    )
    monkeypatch.delenv("ALAS_CONFIG_NAME", raising=False)
    assert screenshot.read_frame("secondary") == MediaFrame(b"encoded", "image/jpeg")
    assert "ALAS_CONFIG_NAME" not in os.environ

    class Manager:
        def __init__(self) -> None:
            self.alive = False
            self.calls: list[str] = []

        def start(self, *, func: str) -> None:
            self.calls.append(func)
            self.alive = True

        def stop(self) -> bool:
            self.calls.append("stop")
            self.alive = False
            return True

    manager = Manager()
    lifecycle = LegacyProcessManagerAdapter(
        manager_factory=lambda instance: manager,
        function_factory=lambda instance: "Main",
    )
    assert lifecycle.start_instance("secondary") is True
    assert lifecycle.stop_instance("secondary") is True
    assert manager.calls == ["Main", "stop"]

    events: list[str] = []

    class Platform:
        def emulator_stop(self) -> bool:
            events.append("stop")
            return True

        def emulator_start(self) -> bool:
            events.append("start")
            return True

    assert LegacyEmulatorAdapter(platform_factory=lambda instance: Platform()).restart_emulator("secondary") is True
    assert events == ["stop", "start"]


@dataclass
class _CommandResult:
    returncode: int
    stdout: str = ""


def test_legacy_adb_adapter_requires_target_only_inventory_before_restart():
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv[-1] == "devices":
            return _CommandResult(0, "List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    assert adapter.restart_adb("secondary") is True
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
    ]

    foreign_calls: list[tuple[str, ...]] = []

    def foreign_runner(argv: tuple[str, ...]) -> _CommandResult:
        foreign_calls.append(argv)
        return _CommandResult(0, "List of devices attached\nforeign\tdevice\n")

    foreign = LegacyAdbAdapter(
        runner=foreign_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    assert foreign.restart_adb("secondary") is False
    assert foreign_calls == [("adb", "devices")]

    malformed = LegacyAdbAdapter(
        runner=lambda argv: type("Result", (), {"returncode": 0})(),
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    assert malformed.restart_adb("secondary") is False

    malformed_inventory = LegacyAdbAdapter(
        runner=lambda argv: _CommandResult(0, "List of devices attached\nbroken\n"),
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "broken",
    )
    assert malformed_inventory.restart_adb("secondary") is False


def test_legacy_adb_adapter_allows_only_singleton_auto_target():
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv[-1] == "devices":
            return _CommandResult(0, "List of devices attached\nserial-a\tdevice\n")
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: None,
    )

    assert adapter.restart_adb("secondary") is True
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
    ]

    no_device_calls: list[tuple[str, ...]] = []

    def no_device_runner(argv: tuple[str, ...]) -> _CommandResult:
        no_device_calls.append(argv)
        return _CommandResult(0, "List of devices attached\n")

    no_device = LegacyAdbAdapter(
        runner=no_device_runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: None,
    )

    assert no_device.restart_adb("secondary") is False
    assert no_device_calls == [("adb", "devices")]


def test_legacy_adb_adapter_preserves_global_inventory():
    calls: list[tuple[str, ...]] = []
    inventories = iter(
        (
            "List of devices attached\nserial-a\tdevice\nserial-b\tdevice\n",
            "List of devices attached\nserial-b\tdevice\nserial-a\tdevice\n",
        )
    )

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv[-1] == "devices":
            return _CommandResult(0, next(inventories))
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
    )

    assert adapter.restart_adb() is True
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
    ]


def test_legacy_adb_adapter_rejects_changed_global_inventory():
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if len(calls) == 1:
            return _CommandResult(
                0,
                "List of devices attached\nserial-a\tdevice\n",
            )
        if argv[-1] == "devices":
            return _CommandResult(
                0,
                "List of devices attached\nserial-b\tdevice\n",
            )
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
    )

    assert adapter.restart_adb() is False
    assert calls == [
        ("adb", "devices"),
        ("adb", "kill-server"),
        ("adb", "start-server"),
        ("adb", "devices"),
    ]
