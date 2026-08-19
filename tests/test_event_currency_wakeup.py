from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import module.webui.event_currency as currency
import module.webui.event_shop_priority as priority
from module.log_res.log_res import LogRes
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


def test_proven_dashboard_pt_increase_wakes_enabled_event_shop(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    previous_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    current_at = previous_at + timedelta(minutes=1)

    persist_current_pt_observation(
        instance=config.config_name,
        event_id=spec["id"],
        server="EN",
        source_revision=spec["provenance"]["revision"],
        value=50,
        observed_at=previous_at,
        source="dashboard_ocr",
        root=observation_root,
    )
    set_event_shop_priority(
        config.config_name,
        spec["id"],
        "11",
        0,
        root=priority_root,
    )

    result = currency.persist_event_currency_update(
        config,
        150,
        source="dashboard_ocr",
        observed_at=current_at,
        observation_root=observation_root,
        priority_root=priority_root,
    )

    assert result is not None
    assert result["current_pt"] == 150
    assert result["current_pt_status"] == "observed"
    assert config.task_calls == [("EventShop", False)]


def test_pt_decrease_does_not_wake_event_shop(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    previous_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    persist_current_pt_observation(
        instance=config.config_name,
        event_id=spec["id"],
        server="EN",
        source_revision=spec["provenance"]["revision"],
        value=200,
        observed_at=previous_at,
        source="dashboard_ocr",
        root=observation_root,
    )
    set_event_shop_priority(
        config.config_name,
        spec["id"],
        "11",
        0,
        root=priority_root,
    )

    result = currency.persist_event_currency_update(
        config,
        150,
        source="dashboard_ocr",
        observed_at=previous_at + timedelta(minutes=1),
        observation_root=observation_root,
        priority_root=priority_root,
    )

    assert result is not None
    assert result["current_pt"] == 150
    assert result["current_pt_status"] == "observed"
    assert config.task_calls == []


def test_stale_pt_increase_is_persisted_but_does_not_wake(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    previous_at = datetime.now(timezone.utc) - timedelta(hours=51)
    current_at = previous_at + timedelta(hours=1)

    persist_current_pt_observation(
        instance=config.config_name,
        event_id=spec["id"],
        server="EN",
        source_revision=spec["provenance"]["revision"],
        value=100,
        observed_at=previous_at,
        source="dashboard_ocr",
        root=observation_root,
    )
    set_event_shop_priority(
        config.config_name,
        spec["id"],
        "11",
        0,
        root=priority_root,
    )

    result = currency.persist_event_currency_update(
        config,
        150,
        source="dashboard_ocr",
        observed_at=current_at,
        observation_root=observation_root,
        priority_root=priority_root,
    )

    assert result is not None
    assert result["current_pt_status"] == "stale"
    assert config.task_calls == []


def test_event_shop_ocr_never_wakes_itself(monkeypatch, tmp_path):
    spec = _spec()
    _patch_current_event(monkeypatch, spec)
    config = FakeConfig()
    observation_root = tmp_path / "observation"
    priority_root = tmp_path / "priority"
    previous_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    persist_current_pt_observation(
        instance=config.config_name,
        event_id=spec["id"],
        server="EN",
        source_revision=spec["provenance"]["revision"],
        value=100,
        observed_at=previous_at,
        source="event_shop_ocr",
        root=observation_root,
    )
    set_event_shop_priority(
        config.config_name,
        spec["id"],
        "11",
        0,
        root=priority_root,
    )

    result = currency.persist_event_currency_update(
        config,
        150,
        source="event_shop_ocr",
        observed_at=previous_at + timedelta(minutes=1),
        observation_root=observation_root,
        priority_root=priority_root,
    )

    assert result is not None
    assert result["current_pt"] == 150
    assert result["current_pt_status"] == "observed"
    assert config.task_calls == []


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
