from __future__ import annotations

import builtins
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
    calls: list[tuple[str, ...]] = []
    frame = b"\x89PNG\r\n\x1a\nframe"
    screenshot = LegacyScreenshotAdapter(
        runner=lambda argv: calls.append(tuple(argv))
        or _CommandResult(0, frame),
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    monkeypatch.delenv("ALAS_CONFIG_NAME", raising=False)
    assert screenshot.read_frame("secondary") == MediaFrame(frame, "image/png")
    assert calls == [("adb", "-s", "serial-a", "exec-out", "screencap", "-p")]
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

    invalid_frame = LegacyScreenshotAdapter(
        runner=lambda argv: _CommandResult(0, b"not a png"),
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: "serial-a",
    )
    with pytest.raises(OSError, match="PNG"):
        invalid_frame.read_frame("secondary")

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
) -> tuple[LegacyAdbAdapter, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    inventory_iter = iter(inventories)

    def runner(argv: tuple[str, ...]) -> _CommandResult:
        calls.append(argv)
        if argv[-1] == "devices":
            inventory = next(inventory_iter, None)
            assert inventory is not None, "адаптер запросил инвентарь лишний раз"
            return _CommandResult(0, inventory)
        return _CommandResult(0)

    adapter = LegacyAdbAdapter(
        runner=runner,
        adb_path_provider=lambda: "adb",
        target_serial_provider=lambda instance: target_serial,
    )
    return adapter, calls


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


def test_legacy_adb_adapter_rejects_offline_target_that_stays_offline():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\toffline\n",
        "List of devices attached\nserial-a\toffline\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls) == _EXPECTED_RESTART_CALLS


def test_legacy_adb_adapter_rejects_unauthorized_post_restart_state():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-a\tunauthorized\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls) == _EXPECTED_RESTART_CALLS


def test_legacy_adb_adapter_rejects_target_that_disappears_after_restart():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls) == _EXPECTED_RESTART_CALLS


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


def test_legacy_adb_adapter_rejects_changed_explicit_target_inventory():
    adapter, calls = _make_adb_adapter(
        "List of devices attached\nserial-a\tdevice\n",
        "List of devices attached\nserial-b\tdevice\n",
    )

    assert adapter.restart_adb("secondary") is False
    assert tuple(calls) == _EXPECTED_RESTART_CALLS


def test_legacy_adb_adapter_rejects_unscoped_restart_without_touching_adb():
    calls: list[tuple[str, ...]] = []

    adapter = LegacyAdbAdapter(
        runner=lambda argv: calls.append(tuple(argv)) or _CommandResult(0),
        adb_path_provider=lambda: "adb",
    )

    assert adapter.restart_adb() is False
    assert calls == []
