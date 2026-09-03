from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alas import AzurLaneAutoScript
from module.application.morale_bootstrap import CampaignMoraleBootstrapError
from module.config.time_source import now as current_time
from module.exception import RequestHumanTakeover
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

    def cross_get(self, keys, default=None):
        path = keys.split('.') if isinstance(keys, str) else tuple(keys)
        value = getattr(self, 'task_groups', {})
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


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
        task_groups={"Main": {"Campaign": {}, "Emotion": {}}},
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


def test_prepare_boundary_only_processes_manual_command_for_non_campaign() -> None:
    events = []
    script = _script()
    script.device = _Device(events)
    script.fleet_manual_scan = _ManualCoordinator(events)

    assert script._prepare_task_boundary("Commission")
    assert events == [("manual", "profile-a")]
    assert script.device.config is script.config


def test_prepare_boundary_scans_campaign_morale_after_manual_command() -> None:
    events = []
    script = _script()
    script.config.Emotion_Mode = "calculate"
    script.fleet_manual_scan = _ManualCoordinator(events)
    script._scan_campaign_morale = (
        lambda task, *, source: events.append(("morale", task, source))
    )

    assert script._prepare_task_boundary("Main")
    assert events == [
        ("manual", "profile-a"),
        ("morale", "Main", "campaign:first_run"),
    ]


def test_prepare_boundary_bootstrap_failure_stops_only_current_task() -> None:
    events = []
    script = _script()
    script.config.Emotion_Mode = "calculate"
    script.fleet_manual_scan = _ManualCoordinator(events)

    def fail_bootstrap(task, *, source):
        events.append(("morale", task, source))
        raise CampaignMoraleBootstrapError(
            "target_lookup_failed",
            "synthetic target evidence failure",
        )

    script._scan_campaign_morale = fail_bootstrap

    assert script._prepare_task_boundary("Main") is False
    assert events == [
        ("manual", "profile-a"),
        ("morale", "Main", "campaign:first_run"),
    ]


def test_campaign_morale_periodic_callback_executes_without_second_gate() -> None:
    script = _script()
    script._morale_scan_state = {
        "Main": {"last_scan": 100.0, "completed_runs": 0}
    }
    calls = []
    script._scan_campaign_morale = (
        lambda task, *, source: calls.append((task, source))
    )

    script._campaign_morale_after_clear("Main", 3)

    assert calls == [("Main", "campaign:periodic_3")]
    assert script._morale_scan_state["Main"]["completed_runs"] == 3


def test_campaign_morale_periodic_callback_scans_when_state_is_missing() -> None:
    script = _script()
    script._morale_scan_state = {}
    calls = []
    script._scan_campaign_morale = (
        lambda task, *, source: calls.append((task, source))
    )

    script._campaign_morale_after_clear("Main", 10)

    assert calls == [("Main", "campaign:periodic_10")]


def test_campaign_morale_scan_wraps_takeover_as_task_level_failure() -> None:
    script = _script()
    script._scan_campaign_morale = (
        lambda _task, *, source: (_ for _ in ()).throw(
            RequestHumanTakeover(f"synthetic failure: {source}")
        )
    )

    with pytest.raises(CampaignMoraleBootstrapError) as exc:
        script._campaign_morale_scan_safely("Main", source="campaign:first_run")

    assert exc.value.code == "scan_evidence_incomplete"
    assert script.config.delay_calls == [{"success": False}]


def test_periodic_morale_scan_updates_completed_runs_only_after_success() -> None:
    script = _script()
    script._morale_scan_state = {
        "Main": {"last_scan": 100.0, "completed_runs": 0}
    }
    script._scan_campaign_morale = (
        lambda _task, *, source: (_ for _ in ()).throw(
            CampaignMoraleBootstrapError("synthetic_failure", source)
        )
    )

    with pytest.raises(CampaignMoraleBootstrapError):
        script._campaign_morale_after_clear("Main", 3)

    assert script._morale_scan_state["Main"]["completed_runs"] == 0


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


def test_scheduler_source_runs_campaign_morale_scan_after_manual_boundary() -> None:
    source = (ROOT / "alas.py").read_text(encoding="utf-8")
    prepare = source[
        source.index("    def _prepare_task_boundary(self, task):") : source.index(
            "    def loop(self):"
        )
    ]

    assert "fleet_auto_scan" in source
    assert prepare.index("task == 'Restart'") < prepare.index(
        "self._run_fleet_manual_scan_if_pending()"
    )
    assert prepare.index("self._run_fleet_manual_scan_if_pending()") < prepare.index(
        "self._campaign_morale_scan_safely(task, source='campaign:first_run')"
    )
