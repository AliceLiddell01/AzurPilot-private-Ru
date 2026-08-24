from __future__ import annotations

from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alas import AzurLaneAutoScript
from module.config.time_source import now as current_time
from module.application.fleet_autoscan import FleetAutoScanMode
from module.persistence import runtime as persistence_runtime

ROOT = Path(__file__).resolve().parents[1]


class _Coordinator:
    def __init__(self) -> None:
        self.calls = []

    def run_if_due(self, instance, config):
        self.calls.append((instance, config))
        return None


class _ManualCoordinator:
    def __init__(self, events=None, execution=None) -> None:
        self.events = events if events is not None else []
        self.execution = execution

    def process_next(self, instance):
        self.events.append(("manual", instance))
        return self.execution

    def has_pending(self, instance):
        self.events.append(("pending", instance))
        return False


class _Device:
    def __init__(self, events=None) -> None:
        self.config = None
        self.events = events if events is not None else []

    def stuck_record_clear(self):
        self.events.append("stuck-clear")

    def click_record_clear(self):
        self.events.append("click-clear")


def _script(*, mode="disabled", fleets=None):
    script = object.__new__(AzurLaneAutoScript)
    script.config_name = "profile-a"
    script.config = SimpleNamespace(
        FleetAutoScan_Mode=mode,
        FleetAutoScan_Fleets=[1, 2, 3, 4, 5, 6] if fleets is None else fleets,
    )
    script.is_first_task = False
    script.device = _Device()
    script.fleet_autoscan = _Coordinator()
    script.fleet_manual_scan = _ManualCoordinator()
    script._manual_scan_wakeup = False
    return script


def test_disabled_boundary_uses_config_without_building_controller() -> None:
    script = _script()

    assert script._run_fleet_autoscan_if_due() is None

    assert script.fleet_autoscan.calls == []
    assert script.device.events == []


def test_boundary_reloads_mode_and_selection_from_current_config() -> None:
    script = _script(mode="disabled", fleets=[1, 2])
    coordinator = script.fleet_autoscan

    script._run_fleet_autoscan_if_due()
    script.config = SimpleNamespace(
        FleetAutoScan_Mode="daily",
        FleetAutoScan_Fleets=[6, 4, 6],
    )
    script._run_fleet_autoscan_if_due()

    assert len(coordinator.calls) == 1
    assert coordinator.calls[0][1].mode is FleetAutoScanMode.DAILY
    assert coordinator.calls[0][1].selection.fleet_indices == (4, 6)


def test_prepare_boundary_runs_autoscan_before_normal_task() -> None:
    events = []
    script = _script(mode="daily", fleets=[2])
    script.device = _Device(events)
    script._run_fleet_autoscan_if_due = lambda: events.append("autoscan")

    assert script._prepare_task_boundary("Commission")
    events.append("normal-task")

    assert events == ["autoscan", "normal-task"]
    assert script.device.config is script.config


def test_first_restart_skip_does_not_run_autoscan() -> None:
    events = []
    script = _script(mode="every_start", fleets=[1])
    script.is_first_task = True
    script.delay_next_restart = lambda: events.append("delay-restart")
    script._run_fleet_autoscan_if_due = lambda: events.append("autoscan")

    assert not script._prepare_task_boundary("Restart")
    assert events == ["delay-restart"]


def test_restart_boundary_runs_before_manual_scan_and_autoscan() -> None:
    events = []
    script = _script(mode="daily", fleets=[1])
    script.fleet_manual_scan = _ManualCoordinator(
        events,
        execution=SimpleNamespace(
            command=SimpleNamespace(
                selection=SimpleNamespace(fleet_indices=(1,)),
                status=SimpleNamespace(value="succeeded"),
            ),
            batch_result=SimpleNamespace(failed_fleet_index=None),
        ),
    )
    script._run_fleet_autoscan_if_due = lambda: events.append("autoscan")

    assert script._prepare_task_boundary("Restart")
    assert events == []


def test_stop_requested_during_autoscan_prevents_normal_task() -> None:
    script = _script(mode="daily", fleets=[1])
    stop_state = {"set": False}
    script.stop_event = SimpleNamespace(is_set=lambda: stop_state["set"])
    script._run_fleet_autoscan_if_due = lambda: stop_state.__setitem__("set", True)

    assert not script._prepare_task_boundary("Commission")


def test_manual_scan_has_priority_and_suppresses_same_boundary_autoscan() -> None:
    events = []
    script = _script(mode="daily", fleets=[1])
    script.fleet_manual_scan = _ManualCoordinator(
        events,
        execution=SimpleNamespace(command=SimpleNamespace(
            selection=SimpleNamespace(fleet_indices=(1, 2)),
            status=SimpleNamespace(value="succeeded"),
        ), batch_result=SimpleNamespace(failed_fleet_index=None)),
    )
    script._run_fleet_autoscan_if_due = lambda: events.append("autoscan")

    assert script._prepare_task_boundary("Commission")
    assert events == [("manual", "profile-a")]


def test_long_wait_manual_wakeup_does_not_run_future_normal_task_early() -> None:
    script = _script(mode="daily", fleets=[1])
    script._manual_scan_wakeup = True
    script.fleet_manual_scan = _ManualCoordinator(
        execution=SimpleNamespace(
            command=SimpleNamespace(
                selection=SimpleNamespace(fleet_indices=(1,)),
                status=SimpleNamespace(value="succeeded"),
            ),
            batch_result=SimpleNamespace(failed_fleet_index=None),
        )
    )
    script._run_fleet_autoscan_if_due = lambda: pytest.fail(
        "Autoscan не должен идти после manual command"
    )

    assert not script._prepare_task_boundary("Commission")
    assert script._manual_scan_wakeup is False


def test_idle_wait_wakes_immediately_for_pending_manual_command() -> None:
    script = _script()
    script.config = SimpleNamespace(start_watching=lambda: None)
    script.fleet_manual_scan.has_pending = lambda instance: instance == "profile-a"

    assert script.wait_until(current_time() + timedelta(hours=4))
    assert script._manual_scan_wakeup is True


def test_controller_factory_reuses_scheduler_owned_device(monkeypatch) -> None:
    script = _script(mode="daily", fleets=[1])
    captured = {}

    class Controller:
        def __init__(self, *, config, device):
            captured.update(config=config, device=device)

    monkeypatch.setattr(
        "module.formation.navigation.FormationFleetController",
        Controller,
    )

    controller = script._build_fleet_autoscan_controller()

    assert isinstance(controller, Controller)
    assert captured == {"config": script.config, "device": script.device}
    assert script.device.events == ["stuck-clear", "click-clear"]


def test_runtime_factory_is_lazy_and_does_not_create_second_engine(monkeypatch) -> None:
    engine = object()
    service = object()
    controller_calls = []
    monkeypatch.setattr(persistence_runtime, "_engine", engine)
    monkeypatch.setattr(persistence_runtime, "_service", service)
    monkeypatch.setattr(
        persistence_runtime,
        "_runtime_timezone",
        ZoneInfo("Asia/Novosibirsk"),
    )
    monkeypatch.setattr(
        persistence_runtime,
        "bootstrap_runtime_storage",
        lambda **_kwargs: service,
    )
    monkeypatch.setattr(
        persistence_runtime,
        "LazyEngine",
        lambda *_args, **_kwargs: pytest.fail("Второй Engine создаваться не должен"),
    )

    context = persistence_runtime.build_runtime_fleet_state_context(
        lambda: controller_calls.append(True) or object(),
        require_ready=False,
    )

    assert context.runtime_timezone == ZoneInfo("Asia/Novosibirsk")
    assert controller_calls == []
    context.state_service._scan_service_factory()
    assert controller_calls == [True]


def test_scheduler_source_keeps_safe_boundary_and_recovery_ordering() -> None:
    source = (ROOT / "alas.py").read_text(encoding="utf-8")
    loop = source[source.index("    def loop(self):") :]

    stop_check = loop.index("if self.stop_event.is_set():")
    scheduled_restart = loop.index("EmulatorManagement_ScheduledEmulatorRestart")
    get_task = loop.index("task = self.get_next_task()")
    autoscan_boundary = loop.index("self._prepare_task_boundary(task)")
    normal_run = loop.index("success = self.run(inflection.underscore(task))")

    assert stop_check < scheduled_restart < get_task < autoscan_boundary < normal_run
    prepare = source[
        source.index("    def _prepare_task_boundary(self, task):") : source.index(
            "    def loop(self):"
        )
    ]
    assert prepare.index("task == 'Restart'") < prepare.index(
        "self._run_fleet_manual_scan_if_pending()"
    )
    assert prepare.index("task == 'Restart'") < prepare.index(
        "self._run_fleet_autoscan_if_due()"
    )
