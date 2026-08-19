from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

import module.event.base as event_base_module
import module.event.campaign_abcd as campaign_abcd_module
import module.event_datamine.campaign_selector as selector_module
import module.event_datamine.registry as registry_module
from module.event.base import EventBase, EventStage
from module.event.campaign_abcd import CampaignABCD
from module.event.campaign_sp import CampaignSP
from module.event_datamine.campaign_selector import (
    EventCampaignSelectorError,
    resolve_generated_campaign_module,
    resolve_generated_campaign_modules,
)


def _args(selector: str):
    return {
        "Event": {
            "Campaign": {
                "Event": {
                    "option_en": [selector],
                }
            }
        }
    }


class _Registry:
    def __init__(self, artifact):
        self.artifact = artifact
        self.calls = []

    def resolve_current(self, server, now, *, supplemental=True):
        self.calls.append((server, now, supplemental))
        return self.artifact


def test_current_generated_catalog_blocks_legacy_fallback_for_unknown_stage(monkeypatch):
    selector = "event_current"
    now = datetime(2026, 8, 19, 12, 0, 0)
    artifact = object()
    registry = _Registry(artifact)

    monkeypatch.setattr(
        registry_module,
        "load_event_artifact_registry",
        lambda _root: registry,
    )
    monkeypatch.setattr(
        selector_module,
        "_verified_generated_modules",
        lambda current: {
            "a1": "en_current/a1.py",
            "sp": "en_current/sp.py",
        }
        if current is artifact
        else {},
    )

    catalog = resolve_generated_campaign_modules(
        selector,
        now=now,
        args_data=_args(selector),
    )

    assert catalog == {
        "a1": "campaign.generated_event.en_current.a1",
        "sp": "campaign.generated_event.en_current.sp",
    }
    assert resolve_generated_campaign_module(
        selector,
        "T1",
        now=now,
        args_data=_args(selector),
    ) == "campaign.generated_event.en_current.a1"

    with pytest.raises(
        EventCampaignSelectorError,
        match="не содержит проверенный этап",
    ):
        resolve_generated_campaign_module(
            selector,
            "C1",
            now=now,
            args_data=_args(selector),
        )

    assert resolve_generated_campaign_module(
        selector,
        "C1",
        now=now,
        args_data=_args(selector),
        strict=False,
    ) is None
    assert registry.calls
    assert all(call[0] == "EN" for call in registry.calls)
    assert all(call[2] is False for call in registry.calls)


def test_event_base_prefers_generated_catalog_over_legacy_directory(monkeypatch):
    runner = EventBase.__new__(EventBase)
    runner.config = SimpleNamespace(Campaign_Event="event_current")
    catalog = {
        "a1": "campaign.generated_event.en_current.a1",
        "b1": "campaign.generated_event.en_current.b1",
        "sp": "campaign.generated_event.en_current.sp",
    }

    monkeypatch.setattr(event_base_module, "current_time", lambda: object())
    monkeypatch.setattr(
        event_base_module,
        "resolve_generated_campaign_modules",
        lambda selector, *, now: catalog if selector == "event_current" else None,
    )

    def reject_legacy_directory(_path):
        raise AssertionError(
            "Current generated event не должен сканировать legacy-directory"
        )

    monkeypatch.setattr(event_base_module.os, "listdir", reject_legacy_directory)

    stages = runner.available_stages()
    assert [str(stage) for stage in stages] == ["a1", "b1", "sp"]

    def reject_legacy_normalization(*_args, **_kwargs):
        raise AssertionError(
            "Current generated stages не должны проходить legacy-нормализацию"
        )

    runner.handle_stage_name = reject_legacy_normalization
    assert runner.convert_stages("A1") == "a1"
    assert runner.convert_stages("D1") == "d1"


def test_event_base_keeps_legacy_directory_fallback(monkeypatch):
    runner = EventBase.__new__(EventBase)
    runner.config = SimpleNamespace(Campaign_Event="event_historical")

    monkeypatch.setattr(event_base_module, "current_time", lambda: object())
    monkeypatch.setattr(
        event_base_module,
        "resolve_generated_campaign_modules",
        lambda selector, *, now: None,
    )
    monkeypatch.setattr(
        event_base_module.os,
        "listdir",
        lambda _path: ["t1.py", "sp.py", "notes.txt"],
    )
    runner.handle_stage_name = lambda name, folder: (
        f"legacy-{str(name).lower()}",
        folder,
    )

    assert [str(stage) for stage in runner.available_stages()] == [
        "t1",
        "sp",
        "unknown",
    ]
    assert runner.convert_stages("A1") == "legacy-a1"


def test_campaign_abcd_runs_generated_stages_from_catalog(monkeypatch):
    runner = CampaignABCD.__new__(CampaignABCD)
    calls = []
    delays = []

    class _Filter:
        filter = []

        def load(self, value):
            self.filter = [[value]]

        def apply(self, stages):
            return stages

    stage_filter = _Filter()
    monkeypatch.setattr(campaign_abcd_module, "STAGE_FILTER", stage_filter)
    monkeypatch.setattr(
        campaign_abcd_module,
        "get_server_last_update",
        lambda _value: datetime(2026, 8, 19, 0, 0, 0),
    )

    runner.config = SimpleNamespace(
        Campaign_Event="event_current",
        EventDaily_StageFilter="b1 > d1",
        EventDaily_LastStage=0,
        Scheduler_NextRun=datetime(2026, 8, 20, 0, 0, 0),
        Scheduler_ServerUpdate="00:00",
        Scheduler_Enable=True,
        multi_set=lambda: nullcontext(),
        task_delay=lambda **kwargs: delays.append(kwargs),
        task_stop=lambda *_args, **_kwargs: None,
        task_switched=lambda: False,
    )
    runner.available_stages = lambda: [
        EventStage("b1.py"),
        EventStage("d1.py"),
    ]
    runner.convert_stages = lambda value: value

    def fake_run(self, *, name, folder, total):
        calls.append((name, folder, total))
        self.run_count = 1

    monkeypatch.setattr(EventBase, "run", fake_run)

    runner.run()

    assert calls == [
        ("b1", "event_current", 1),
        ("d1", "event_current", 1),
    ]
    assert delays[-1] == {"server_update": True}


def test_campaign_sp_uses_generated_catalog_without_physical_sp_file(monkeypatch):
    runner = CampaignSP.__new__(CampaignSP)
    calls = []
    delays = []

    runner.config = SimpleNamespace(
        Campaign_Event="event_current",
        Campaign_Name="sp",
        Scheduler_Enable=True,
        task_delay=lambda **kwargs: delays.append(kwargs),
        task_stop=lambda *_args, **_kwargs: None,
    )
    runner.available_stages = lambda: [EventStage("sp.py")]
    runner.convert_stages = lambda value: value

    def fake_run(self, *, name, folder, total):
        calls.append((name, folder, total))
        self.run_count = 1

    monkeypatch.setattr(EventBase, "run", fake_run)

    runner.run()

    assert calls == [("sp", "event_current", 1)]
    assert delays == [{"server_update": True}]
