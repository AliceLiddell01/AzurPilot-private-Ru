import time
from types import SimpleNamespace

import pytest

from module.application.morale_rescan import MoraleRescanPolicy
from module.campaign.run import CampaignRun


def test_default_rescan_policy_is_ten_runs_or_sixty_minutes():
    policy = MoraleRescanPolicy.from_environment({})

    assert policy.runs == 10
    assert policy.minutes == 60
    assert policy.due(completed_runs=9, elapsed_seconds=3599) == (False, None)
    assert policy.due(completed_runs=10, elapsed_seconds=10) == (True, "runs")
    assert policy.due(completed_runs=3, elapsed_seconds=3600) == (True, "time")


def test_zero_disables_each_periodic_trigger():
    policy = MoraleRescanPolicy.from_environment(
        {
            "AZURPILOT_MORALE_RESCAN_RUNS": "0",
            "AZURPILOT_MORALE_RESCAN_MINUTES": "0",
        }
    )

    assert policy.due(completed_runs=100, elapsed_seconds=100000) == (False, None)


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({"AZURPILOT_MORALE_RESCAN_RUNS": "-1"}, "RESCAN_RUNS"),
        ({"AZURPILOT_MORALE_RESCAN_MINUTES": "abc"}, "RESCAN_MINUTES"),
    ),
)
def test_invalid_rescan_policy_fails_closed(environment, message):
    with pytest.raises(ValueError, match=message):
        MoraleRescanPolicy.from_environment(environment)


def _runner(run_count):
    runner = object.__new__(CampaignRun)
    runner.run_count = run_count
    runner.campaign = SimpleNamespace(
        emotion=SimpleNamespace(
            is_calculate=False,
            log_working_fleets=lambda _source: None,
        )
    )
    return runner


def test_campaign_hook_invokes_periodic_callback_only_when_policy_is_due(monkeypatch):
    monkeypatch.setenv("AZURPILOT_MORALE_RESCAN_RUNS", "3")
    monkeypatch.setenv("AZURPILOT_MORALE_RESCAN_MINUTES", "0")
    calls = []
    runner = _runner(2)
    runner.morale_campaign_clear_callback = calls.append
    runner._morale_rescan_last_at = time.monotonic()

    runner.after_campaign_run()
    assert calls == []

    runner.run_count = 3
    runner.after_campaign_run()
    assert calls == [10]


def test_campaign_hook_time_trigger_uses_same_safe_boundary(monkeypatch):
    monkeypatch.setenv("AZURPILOT_MORALE_RESCAN_RUNS", "0")
    monkeypatch.setenv("AZURPILOT_MORALE_RESCAN_MINUTES", "1")
    calls = []
    runner = _runner(1)
    runner.morale_campaign_clear_callback = calls.append
    runner._morale_rescan_last_at = time.monotonic() - 61

    runner.after_campaign_run()

    assert calls == [10]
