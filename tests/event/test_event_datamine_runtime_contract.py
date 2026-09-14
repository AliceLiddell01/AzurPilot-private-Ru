from types import SimpleNamespace

import pytest

import module.webui.app_event_datamine as event_datamine
from module.webui.app_event_datamine import EventDatamineMixin


def test_event_plan_fails_closed_when_resolver_breaks_contract(monkeypatch):
    mixin = EventDatamineMixin()
    mixin.alas_name = "test-instance"
    mixin.alas_config = SimpleNamespace(read_file=lambda _name: {})

    monkeypatch.setattr(event_datamine, "is_demo_mode", lambda: False)
    monkeypatch.setattr(
        event_datamine,
        "resolve_current_event_artifact",
        lambda *, server, now: (None, None),
    )

    with pytest.raises(RuntimeError, match="Resolver текущего события"):
        mixin._event_plan()
