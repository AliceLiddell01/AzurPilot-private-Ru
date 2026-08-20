import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import module.webui.event_currency as currency
import module.webui.event_shop_priority as priority
from module.log_res.log_res import LogRes
from module.webui.event_observation import (
    dashboard_pt_observation,
    event_observation_path,
)
from module.webui.event_observation_update import persist_current_pt_observation
from module.webui.event_shop_priority import set_event_shop_priority


class FakeConfig:
    config_name = "test-instance"
    SERVER = "EN"

    def __init__(self, *, enabled=True):
        self.enabled = enabled
        self.task_calls = []

    def is_task_enabled(self, task):
        assert task == "EventShop"
        return self.enabled

    def task_call(self, task, force_call=True):
        self.task_calls.append((task, force_call))
        return True


def _spec():
    return {
        "id": "event-test",
        "server": "EN",
        "provenance": {"revision": "c" * 40},
        "currencies": [{"id": 1, "runtime_token": "pt"}],
        "shop_items": [
            {
                "row_id": 11,
                "stock": 1,
                "price": 100,
                "currency_id": 1,
                "amount": 1,
                "event_shop_filter": "Chip",
            }
        ],
    }


def _patch_current_event(monkeypatch, spec):
    registry = SimpleNamespace(
        resolve_current=lambda server, now: {"event_spec": spec}
    )
    monkeypatch.setattr(currency, "load_event_artifact_registry", lambda: registry)
    monkeypatch.setattr(priority, "_current_spec", lambda config: spec)
    monkeypatch.setattr(
        priority,
        "_selected_targets",
        lambda config, event_id: {"11": 1},
    )


def _seed_shop_balance(config, spec, root, value, observed_at):
    return persist_current_pt_observation(
        instance=config.config_name,
        event_id=spec["id"],
        server="EN",
        source_revision=spec["provenance"]["revision"],
        value=value,
        observed_at=observed_at,
        source="event_shop_ocr",
        root=root,
    )


def _record_total(
    config,
    value,
    observed_at,
    observation_root,
    priority_root,
):
    return currency.persist_event_currency_update(
        config,
        value,
        source="dashboard_ocr",
        observed_at=observed_at,
        observation_root=observation_root,
        priority_root=priority_root,
    )


def _enable_target(config, spec, priority_root):
    set_event_shop_priority(
        config.config_name,
        spec["id"],
        "11",
        0,
        root=priority_root,
    )


def test_first_cumulative_total_never_becomes_shop_balance(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    _enable_target(config, spec, priority_root)

    result = _record_total(
        config,
        7190,
        datetime.now(timezone.utc),
        observation_root,
        priority_root,
    )

    assert result is not None
    assert result["event_pt_total"] == 7190
    assert result["event_pt_total_source"] == "dashboard_ocr"
    assert result["current_pt"] is None
    assert config.task_calls == []


def test_cumulative_delta_wakes_only_when_estimated_balance_crosses_threshold(
    monkeypatch, tmp_path
):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    _seed_shop_balance(config, spec, observation_root, 10, started_at)
    _enable_target(config, spec, priority_root)

    baseline = _record_total(
        config,
        7190,
        started_at + timedelta(minutes=1),
        observation_root,
        priority_root,
    )
    below = _record_total(
        config,
        7240,
        started_at + timedelta(minutes=2),
        observation_root,
        priority_root,
    )
    crossed = _record_total(
        config,
        7290,
        started_at + timedelta(minutes=3),
        observation_root,
        priority_root,
    )
    above = _record_total(
        config,
        7300,
        started_at + timedelta(minutes=4),
        observation_root,
        priority_root,
    )

    assert baseline["current_pt"] == 10
    assert below["current_pt"] == 60
    assert crossed["current_pt"] == 110
    assert crossed["current_pt_source"] == "event_pt_delta"
    assert above["current_pt"] == 120
    assert config.task_calls == [("EventShop", False)]


def test_event_shop_ocr_resyncs_balance_and_resets_delta_anchor(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    _seed_shop_balance(config, spec, observation_root, 10, started_at)
    _record_total(
        config,
        7190,
        started_at + timedelta(minutes=1),
        observation_root,
        priority_root,
    )
    before_resync = _record_total(
        config,
        7240,
        started_at + timedelta(minutes=2),
        observation_root,
        priority_root,
    )
    assert before_resync["current_pt"] == 60

    resynced = currency.persist_event_currency_update(
        config,
        20,
        source="event_shop_ocr",
        observed_at=started_at + timedelta(minutes=3),
        observation_root=observation_root,
        priority_root=priority_root,
    )
    first_total = _record_total(
        config,
        7300,
        started_at + timedelta(minutes=4),
        observation_root,
        priority_root,
    )
    next_total = _record_total(
        config,
        7350,
        started_at + timedelta(minutes=5),
        observation_root,
        priority_root,
    )

    assert resynced is not None
    assert resynced["current_pt"] == 20
    assert resynced["event_pt_total"] is None
    assert first_total["current_pt"] == 20
    assert first_total["event_pt_total"] == 7300
    assert next_total["current_pt"] == 70
    assert config.task_calls == []


def test_decreasing_cumulative_total_is_ignored_without_corrupting_balance(
    monkeypatch, tmp_path
):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    _seed_shop_balance(config, spec, observation_root, 10, started_at)
    _record_total(
        config,
        7190,
        started_at + timedelta(minutes=1),
        observation_root,
        priority_root,
    )
    increased = _record_total(
        config,
        7240,
        started_at + timedelta(minutes=2),
        observation_root,
        priority_root,
    )
    decreased = _record_total(
        config,
        7000,
        started_at + timedelta(minutes=3),
        observation_root,
        priority_root,
    )
    resumed = _record_total(
        config,
        7250,
        started_at + timedelta(minutes=4),
        observation_root,
        priority_root,
    )

    assert increased["current_pt"] == 60
    assert decreased["event_pt_total"] == 7240
    assert decreased["current_pt"] == 60
    assert resumed["event_pt_total"] == 7250
    assert resumed["current_pt"] == 70
    assert config.task_calls == []


def test_event_spec_without_pt_shop_currency_never_derives_wallet(
    monkeypatch, tmp_path
):
    spec = _spec()
    spec["currencies"] = [{"id": 1, "runtime_token": "urpt"}]
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    _seed_shop_balance(config, spec, observation_root, 10, started_at)
    _enable_target(config, spec, priority_root)
    _record_total(
        config,
        7190,
        started_at + timedelta(minutes=1),
        observation_root,
        priority_root,
    )
    result = _record_total(
        config,
        7290,
        started_at + timedelta(minutes=2),
        observation_root,
        priority_root,
    )

    assert result["event_pt_total"] == 7290
    assert result["current_pt"] == 10
    assert result["current_pt_source"] == "event_shop_ocr"
    assert config.task_calls == []


def test_stale_cumulative_total_does_not_derive_wallet(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(hours=52)

    _seed_shop_balance(config, spec, observation_root, 50, started_at)
    _enable_target(config, spec, priority_root)
    _record_total(
        config,
        100,
        started_at + timedelta(minutes=1),
        observation_root,
        priority_root,
    )
    result = _record_total(
        config,
        200,
        started_at + timedelta(minutes=2),
        observation_root,
        priority_root,
    )

    assert result["event_pt_total_status"] == "stale"
    assert result["current_pt"] == 50
    assert config.task_calls == []


def test_event_shop_ocr_never_wakes_itself(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _enable_target(config, spec, priority_root)

    first = currency.persist_event_currency_update(
        config,
        50,
        source="event_shop_ocr",
        observed_at=started_at,
        observation_root=observation_root,
        priority_root=priority_root,
    )
    second = currency.persist_event_currency_update(
        config,
        150,
        source="event_shop_ocr",
        observed_at=started_at + timedelta(minutes=1),
        observation_root=observation_root,
        priority_root=priority_root,
    )

    assert first["current_pt"] == 50
    assert second["current_pt"] == 150
    assert config.task_calls == []


def test_schema_v2_polluted_dashboard_balance_is_invalidated(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    revision = spec["provenance"]["revision"]
    path = event_observation_path(
        config.config_name,
        spec["id"],
        "EN",
        observation_root,
        source_revision=revision,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "event_id": spec["id"],
                "server": "EN",
                "instance": config.config_name,
                "source_revision": revision,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": "dashboard_ocr",
                "current_pt": 7190,
                "current_pt_source": "dashboard_ocr",
                "current_pt_observed_at": datetime.now(timezone.utc).isoformat(),
                "current_pt_status": "observed",
            }
        ),
        encoding="utf-8",
    )
    _enable_target(config, spec, priority_root)

    result = _record_total(
        config,
        7250,
        datetime.now(timezone.utc) + timedelta(seconds=1),
        observation_root,
        priority_root,
    )

    assert result["schema_version"] == 3
    assert result["event_pt_total"] == 7250
    assert result["current_pt"] is None
    assert not any(
        item.get("code") == "observation_schema_unsupported"
        for item in result["findings"]
    )
    assert config.task_calls == []


def test_dashboard_observation_keeps_total_separate_from_current_balance():
    now = datetime.now(timezone.utc)
    result = dashboard_pt_observation(
        instance="test-instance",
        event_id="event-test",
        server="EN",
        value=7190,
        recorded_at=now.isoformat(),
        source_revision="c" * 40,
        now=now,
    )

    assert result["event_pt_total"] == 7190
    assert result["event_pt_total_status"] == "observed"
    assert result["current_pt"] is None
    assert result["current_pt_status"] == "unavailable"


def test_current_balance_rejects_dashboard_as_absolute_source(tmp_path):
    with pytest.raises(ValueError, match="накопительным счётчиком"):
        persist_current_pt_observation(
            instance="test-instance",
            event_id="event-test",
            server="EN",
            source_revision="c" * 40,
            value=7190,
            observed_at=datetime.now(timezone.utc),
            source="dashboard_ocr",
            root=tmp_path / "observation",
        )


def test_log_res_pt_change_feeds_event_currency_bridge(monkeypatch):
    calls = []
    config = SimpleNamespace(
        config_name="test-instance",
        task=SimpleNamespace(command="Campaign"),
        data={"Dashboard": {"Pt": {"Value": 100, "Record": datetime(2020, 1, 1)}}},
        modified={},
    )
    monkeypatch.setattr(LogRes, "groups", {"Pt": {}})
    monkeypatch.setattr(LogRes, "_record_all_resource_snapshot", lambda self, overrides=None: None)
    monkeypatch.setattr(
        currency,
        "persist_event_currency_update",
        lambda cfg, value, *, source: calls.append((cfg, value, source)),
    )

    LogRes(config).Pt = 150

    assert config.modified["Dashboard.Pt.Value"] == 150
    assert "Dashboard.Pt.Record" in config.modified
    assert calls == [(config, 150, "dashboard_ocr")]


def test_log_res_event_shop_task_does_not_feed_dashboard_bridge(monkeypatch):
    calls = []
    config = SimpleNamespace(
        config_name="test-instance",
        task=SimpleNamespace(command="EventShop"),
        data={"Dashboard": {"Pt": {"Value": 100, "Record": datetime(2020, 1, 1)}}},
        modified={},
    )
    monkeypatch.setattr(LogRes, "groups", {"Pt": {}})
    monkeypatch.setattr(LogRes, "_record_all_resource_snapshot", lambda self, overrides=None: None)
    monkeypatch.setattr(
        currency,
        "persist_event_currency_update",
        lambda cfg, value, *, source: calls.append((cfg, value, source)),
    )

    LogRes(config).Pt = 150

    assert config.modified["Dashboard.Pt.Value"] == 150
    assert "Dashboard.Pt.Record" in config.modified
    assert calls == []
