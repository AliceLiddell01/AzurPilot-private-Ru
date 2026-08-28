import time
from types import SimpleNamespace

import pytest

from module.application.morale_bootstrap import CampaignMoraleBootstrapError
from module.application.morale_rescan import MoraleRescanPolicy
from module.campaign.run import CampaignRun


def _config(*, runs=10, minutes=60, include_policy=True):
    storage = {}
    if include_policy:
        storage["MoraleRescan"] = {
            "Runs": runs,
            "Minutes": minutes,
        }
    return SimpleNamespace(Storage_Storage=storage)


def test_default_rescan_policy_is_ten_runs_or_sixty_minutes():
    policy = MoraleRescanPolicy.from_config(_config(include_policy=False))

    assert policy.runs == 10
    assert policy.minutes == 60
    assert policy.due(completed_runs=9, elapsed_seconds=3599) == (False, None)
    assert policy.due(completed_runs=10, elapsed_seconds=10) == (True, "runs")
    assert policy.due(completed_runs=3, elapsed_seconds=3600) == (True, "time")


def test_custom_rescan_policy_uses_task_storage_values():
    policy = MoraleRescanPolicy.from_config(_config(runs=3, minutes=7))

    assert policy.runs == 3
    assert policy.minutes == 7
    assert policy.due(completed_runs=3, elapsed_seconds=0) == (True, "runs")
    assert policy.due(completed_runs=1, elapsed_seconds=420) == (True, "time")


def test_zero_disables_each_periodic_trigger():
    policy = MoraleRescanPolicy.from_config(_config(runs=0, minutes=0))

    assert policy.due(completed_runs=100, elapsed_seconds=100000) == (False, None)


@pytest.mark.parametrize(
    ("config", "message"),
    (
        (_config(runs=-1), "Storage.Storage.MoraleRescan.Runs"),
        (_config(minutes="abc"), "Storage.Storage.MoraleRescan.Minutes"),
        (
            SimpleNamespace(Storage_Storage={"MoraleRescan": "broken"}),
            "Storage.Storage.MoraleRescan",
        ),
    ),
)
def test_invalid_rescan_policy_fails_closed(config, message):
    with pytest.raises(ValueError, match=message):
        MoraleRescanPolicy.from_config(config)


def _runner(run_count, *, runs=10, minutes=60):
    runner = object.__new__(CampaignRun)
    runner.run_count = run_count
    runner.config = _config(runs=runs, minutes=minutes)
    runner.campaign = SimpleNamespace(
        emotion=SimpleNamespace(
            is_calculate=False,
            log_working_fleets=lambda _source: None,
        )
    )
    return runner


def test_campaign_hook_invokes_periodic_callback_only_when_config_policy_is_due():
    calls = []
    runner = _runner(2, runs=3, minutes=0)
    runner.morale_campaign_clear_callback = calls.append
    runner._morale_rescan_last_at = time.monotonic()

    assert runner.after_campaign_run() is True
    assert calls == []

    runner.run_count = 3
    assert runner.after_campaign_run() is True
    assert calls == [3]


def test_campaign_hook_time_trigger_uses_same_safe_boundary():
    calls = []
    runner = _runner(1, runs=0, minutes=1)
    runner.morale_campaign_clear_callback = calls.append
    runner._morale_rescan_last_at = time.monotonic() - 61

    assert runner.after_campaign_run() is True

    assert calls == [1]


def test_periodic_bootstrap_failure_stops_campaign_loop_without_escaping():
    runner = _runner(3, runs=3, minutes=0)
    runner._morale_rescan_last_at = time.monotonic()

    def fail(_completed_runs):
        raise CampaignMoraleBootstrapError(
            "target_lookup_failed",
            "synthetic periodic evidence failure",
        )

    runner.morale_campaign_clear_callback = fail

    assert runner.after_campaign_run() is False
