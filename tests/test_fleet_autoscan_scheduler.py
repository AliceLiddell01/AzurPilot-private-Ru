from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alas import AzurLaneAutoScript
from module.config.time_source import now as current_time
from module.persistence import runtime as persistence_runtime

ROOT = Path(__file__).resolve().parents[1]


class _Coordinator:
    def __init__(self, execution=None, error=None) -> None:
        self.calls = []
        self.execution = execution
        self.error = error

    def run(self, instance, config):
        self.calls.append((instance, config))
        if self.error is not None:
            raise self.error
        return self.execution


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


class _Config(SimpleNamespace):
    def task_delay(self, **kwargs):
        self.delay_calls.append(kwargs)


class _Device:
    def __init__(self, events=None) -> None:
        self.config = None
        self.events = events if events is not None else []

    def stuck_record_clear(self):
        self.events.append("stuck-clear")

    def click_record_clear(self):
        self.events.append("click-clear")


def _execution(*, incomplete=(), failed=None):
    selected = (1, 2)
    return SimpleNamespace(
        selection=SimpleNamespace(fleet_indices=selected),
        complete_fleet_indices=tuple(i for i in selected if i not in incomplete),
        incomplete_fleet_indices=tuple(incomplete),
        batch_result=SimpleNamespace(failed_fleet_index=failed),
    )


def _script(*, coordinator=None):
    script = object.__new__(AzurLaneAutoScript)
    script.config_name = "profile-a"
    script.config = _Config(
        FleetAutoScan_Fleets=[1, 2],
        delay_calls=[],
    )
    script.is_first_task = False
    script.device = _Device()
    script.fleet_autoscan = coordinator or _Coordinator(_execution())
    script.fleet_manual_scan = _ManualCoordinator()
    script._manual_scan_wakeup = False
    return script


def test_scheduler_task_runs_selected_fleets_and_delays_to_server_update() -> None:
    script = _script()

    execution = script.fleet_auto_scan()

    assert execution is script.fleet_autoscan.execution
    assert script.fleet_autoscan.calls[0][0] == "profile-a"
    assert script.fleet_autoscan.calls[0][1].selection.fleet_indices == (1, 2)
    assert script.config.delay_calls == [{"server_update": True}]


@pytest.mark.parametrize(
    ("execution", "failed"),
    [
        (_execution(incomplete=(2,)), False),
        (_execution(incomplete=(2,), failed=2), False),
        (None, True),
    ],
)
def test_scheduler_failure_policy_uses_failure_interval(execution, failed) -> None:
    error = RuntimeError("scan failed") if failed else None
    script = _script(coordinator=_Coordinator(execution, error))

    if failed:
        with pytest.raises(RuntimeError, match="scan failed"):
            script.fleet_auto_scan()
    else:
        script.fleet_auto_scan()

    assert script.config.delay_calls == [{"success": False}]


def test_prepare_boundary_only_processes_manual_command() -> None:
    events = []
    script = _script()
    script.device = _Device(events)
    script.fleet_manual_scan = _ManualCoordinator(events)

    assert script._prepare_task_boundary("Commission")
    assert events == [("manual", "profile-a")]
    assert script.device.config is script.config


def test_prepare_boundary_skips_manual_command_after_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    script = _script()
    script.device = _Device(events)
    script.fleet_manual_scan = _ManualCoordinator(events)
    monkeypatch.setattr("alas._handover_requested", lambda _config_name: True)

    assert not script._prepare_task_boundary("Commission")
    assert events == []
    assert script.device.config is None


def test_loop_does_not_finish_task_cancelled_before_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    script.config.EmulatorManagement_ScheduledEmulatorRestart = False
    script.checker = SimpleNamespace(
        wait_until_available=lambda: None,
        is_recovered=lambda: False,
    )
    script.failure_record = {}
    script._emulator_recovery_transport_lost = False
    script.get_next_task = lambda: "Commission"
    script._prepare_task_boundary = lambda _task: True
    started: list[str] = []
    finished: list[str] = []
    script._record_dev_runtime_task_started = (
        lambda task: started.append(task) or True
    )
    script._record_dev_runtime_task_finished = (
        lambda task: finished.append(task) or True
    )

    def fail_run(_command: str) -> object:
        raise AssertionError("Запуск задачи не должен выполняться")

    script.run = fail_run
    handover_values = iter((False, False, True))
    monkeypatch.setattr(
        "alas._handover_requested",
        lambda _config_name: next(handover_values),
    )
    monkeypatch.setattr(
        "alas.logger",
        SimpleNamespace(
            set_file_logger=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr("module.config.utils.is_oobe_needed", lambda: False)

    script.loop()

    assert started == []
    assert finished == []


def test_loop_does_not_enter_task_when_handover_arrives_after_started_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    script.config.EmulatorManagement_ScheduledEmulatorRestart = False
    script.checker = SimpleNamespace(
        wait_until_available=lambda: None,
        is_recovered=lambda: False,
    )
    script.failure_record = {}
    script._emulator_recovery_transport_lost = False
    script.get_next_task = lambda: "Commission"
    script._prepare_task_boundary = lambda _task: True
    boundary_entered = Event()
    allow_boundary_return = Event()
    handover_accepted = Event()
    started: list[str] = []

    def record_started(task: str) -> bool:
        started.append(task)
        boundary_entered.set()
        assert allow_boundary_return.wait(5)
        return True

    script._record_dev_runtime_task_started = record_started
    script._record_dev_runtime_task_finished = lambda _task: True
    script.run = lambda _command: pytest.fail("Запуск задачи не должен выполняться")

    def accept_handover() -> None:
        assert boundary_entered.wait(5)
        handover_accepted.set()
        allow_boundary_return.set()

    handover_thread = Thread(target=accept_handover)
    handover_thread.start()
    monkeypatch.setattr(
        "alas._handover_requested",
        lambda _config_name: handover_accepted.is_set(),
    )
    monkeypatch.setattr(
        "alas.logger",
        SimpleNamespace(
            set_file_logger=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr("module.config.utils.is_oobe_needed", lambda: False)

    try:
        script.loop()
    finally:
        allow_boundary_return.set()
        handover_thread.join(timeout=5)

    assert not handover_thread.is_alive()
    assert started == ["Commission"]


def test_long_wait_manual_wakeup_does_not_run_future_normal_task_early() -> None:
    script = _script()
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

    assert not script._prepare_task_boundary("Commission")
    assert script._manual_scan_wakeup is False


def test_idle_wait_wakes_immediately_for_pending_manual_command() -> None:
    script = _script()
    script.config = SimpleNamespace(start_watching=lambda: None)
    script.fleet_manual_scan.has_pending = lambda instance: instance == "profile-a"

    assert script.wait_until(current_time() + timedelta(hours=4))
    assert script._manual_scan_wakeup is True


def test_idle_wait_aborts_for_handover_before_next_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    script.config = SimpleNamespace(start_watching=lambda: None)
    monkeypatch.setattr(
        "alas._handover_requested",
        lambda _config_name: True,
    )

    assert not script.wait_until(current_time() + timedelta(hours=4))


def test_get_next_task_does_not_read_scheduler_after_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    calls: list[str] = []
    script.config = SimpleNamespace(
        get_next=lambda: calls.append("get_next")
    )
    monkeypatch.setattr(
        "alas._handover_requested",
        lambda _config_name: True,
    )

    assert script.get_next_task() is None
    assert calls == []


def test_controller_factory_reuses_scheduler_owned_device(monkeypatch) -> None:
    script = _script()
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


def test_scheduler_source_has_no_hidden_autoscan_boundary() -> None:
    source = (ROOT / "alas.py").read_text(encoding="utf-8")
    prepare = source[
        source.index("    def _prepare_task_boundary(self, task):") : source.index(
            "    def loop(self):"
        )
    ]

    assert "_run_fleet_autoscan_if_due" not in source
    assert "fleet_auto_scan" in source
    assert prepare.index("task == 'Restart'") < prepare.index(
        "self._run_fleet_manual_scan_if_pending()"
    )
