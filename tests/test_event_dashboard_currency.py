from datetime import datetime, timezone
from types import SimpleNamespace

import module.webui.app_dashboard as dashboard


def test_dashboard_inserts_current_balance_after_cumulative_pt():
    assert dashboard._dashboard_groups_with_event_balance(
        ["Oil", "Coin", "Gem", "Pt", "Cube"]
    ) == ["Oil", "Coin", "Gem", "Pt", "EventCurrencyBalance", "Cube"]


def test_dashboard_event_currency_labels_use_localization_keys(monkeypatch):
    seen = []

    def fake_t(key):
        seen.append(key)
        return f"<{key}>"

    monkeypatch.setattr(dashboard, "t", fake_t)

    assert dashboard._dashboard_group_label("Pt") == "<Gui.Dashboard.EventPtTotal>"
    assert (
        dashboard._dashboard_group_label("EventCurrencyBalance")
        == "<Gui.Dashboard.EventCurrencyBalance>"
    )
    assert seen == [
        "Gui.Dashboard.EventPtTotal",
        "Gui.Dashboard.EventCurrencyBalance",
    ]


def test_dashboard_current_balance_uses_only_observed_event_plan_value(monkeypatch):
    observed_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        dashboard,
        "load_current_event_plan",
        lambda instance, server: {
            "progress": {
                "current_pt": 1234,
                "status": "observed",
                "observed_at": observed_at,
            }
        },
    )
    config = SimpleNamespace(config_name="alas", SERVER="EN")

    group = dashboard._event_currency_balance_group(config)

    assert group["Value"] == 1234
    assert group["Record"].isoformat() == observed_at
    assert group["Color"] == "^00BFFF"


def test_dashboard_unknown_current_balance_never_displays_zero(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "load_current_event_plan",
        lambda instance, server: {
            "progress": {
                "current_pt": None,
                "status": "unavailable",
                "observed_at": "",
            }
        },
    )
    config = SimpleNamespace(config_name="alas", SERVER="EN")

    group = dashboard._event_currency_balance_group(config)

    assert group["Value"] is None
    assert group["Record"] is None


def test_dashboard_current_balance_cache_avoids_reloading_within_ttl(monkeypatch):
    gui = dashboard.DashboardMixin()
    gui.alas_config = SimpleNamespace(config_name="alas", SERVER="EN")
    calls = []
    ticks = iter((10.0, 11.0))
    expected = {"Value": 321, "Record": None, "Color": "^00BFFF"}

    monkeypatch.setattr(dashboard, "monotonic", lambda: next(ticks))

    def load_group(config):
        calls.append(config)
        return expected

    monkeypatch.setattr(dashboard, "_event_currency_balance_group", load_group)

    assert gui._event_currency_balance_group_cached() is expected
    assert gui._event_currency_balance_group_cached() is expected
    assert calls == [gui.alas_config]


def test_dashboard_current_balance_cache_reloads_after_ttl(monkeypatch):
    gui = dashboard.DashboardMixin()
    gui.alas_config = SimpleNamespace(config_name="alas", SERVER="EN")
    ticks = iter((10.0, 16.0))
    values = iter((100, 200))

    monkeypatch.setattr(dashboard, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        dashboard,
        "_event_currency_balance_group",
        lambda config: {
            "Value": next(values),
            "Record": None,
            "Color": "^00BFFF",
        },
    )

    assert gui._event_currency_balance_group_cached()["Value"] == 100
    assert gui._event_currency_balance_group_cached()["Value"] == 200


def test_dashboard_current_balance_loader_failure_is_fail_closed_and_cached(monkeypatch):
    gui = dashboard.DashboardMixin()
    gui.alas_config = SimpleNamespace(config_name="alas", SERVER="EN")
    calls = []
    warnings = []
    ticks = iter((20.0, 21.0))

    monkeypatch.setattr(dashboard, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(dashboard.logger, "warning", warnings.append)

    def fail(config):
        calls.append(config)
        raise OSError("повреждённое состояние")

    monkeypatch.setattr(dashboard, "_event_currency_balance_group", fail)

    first = gui._event_currency_balance_group_cached()
    second = gui._event_currency_balance_group_cached()

    assert first == {"Value": None, "Record": None, "Color": "^00BFFF"}
    assert second == first
    assert calls == [gui.alas_config]
    assert len(warnings) == 1
    assert "Не удалось получить текущий баланс валюты события" in warnings[0]
