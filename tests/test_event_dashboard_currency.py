from datetime import datetime, timezone
from types import SimpleNamespace

import module.webui.app_dashboard as dashboard


def test_dashboard_inserts_current_balance_after_cumulative_pt():
    assert dashboard._dashboard_groups_with_event_balance(
        ["Oil", "Coin", "Gem", "Pt", "Cube"]
    ) == ["Oil", "Coin", "Gem", "Pt", "EventCurrencyBalance", "Cube"]
    assert dashboard._dashboard_group_label("Pt") == "Всего валюты события заработано"
    assert (
        dashboard._dashboard_group_label("EventCurrencyBalance")
        == "Текущий баланс валюты события"
    )


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
