from types import SimpleNamespace

import pytest

import module.event_datamine.campaign_selector as campaign_selector_module
import module.handler.fast_forward as fast_forward_module
from module.event_datamine.campaign_selector import EventCampaignSelectorError
from module.exception import RequestHumanTakeover
from module.handler.fast_forward import FastForwardHandler


def _runner(*, stage="A1"):
    runner = FastForwardHandler.__new__(FastForwardHandler)
    runner.config = SimpleNamespace(
        Campaign_Event="event_current",
        Campaign_Name=stage,
        Scheduler_Enable=True,
        STAGE_INCREASE_AB=True,
        STAGE_INCREASE_CUSTOM="",
        StopCondition_StageIncrease=True,
    )
    return runner


def _forbid_legacy_catalog(monkeypatch):
    monkeypatch.setattr(
        fast_forward_module,
        "map_files",
        lambda _selector: (_ for _ in ()).throw(
            AssertionError("Generated Event не должен читать legacy-каталог")
        ),
    )


def test_generated_event_advances_by_verified_catalog_order(monkeypatch):
    runner = _runner(stage="A3")
    catalog = {
        "a1": "campaign.generated_event.synthetic.a1",
        "a2": "campaign.generated_event.synthetic.a2",
        "a3": "campaign.generated_event.synthetic.a3",
        "b1": "campaign.generated_event.synthetic.b1",
        "b2": "campaign.generated_event.synthetic.b2",
        "b3": "campaign.generated_event.synthetic.b3",
        "c1": "campaign.generated_event.synthetic.c1",
        "c2": "campaign.generated_event.synthetic.c2",
        "c3": "campaign.generated_event.synthetic.c3",
        "d1": "campaign.generated_event.synthetic.d1",
    }
    calls = []

    def resolver(selector, *, auto_advance_only=False):
        calls.append((selector, auto_advance_only))
        return catalog if selector == "event_current" else None

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        resolver,
    )
    _forbid_legacy_catalog(monkeypatch)

    runner.handle_map_stop()
    assert runner.config.Campaign_Name == "B1"
    assert runner.config.Scheduler_Enable is True

    runner.config.Campaign_Name = "B3"
    runner.handle_map_stop()
    assert runner.config.Campaign_Name == "C1"
    assert runner.config.Scheduler_Enable is True

    runner.config.Campaign_Name = "C3"
    runner.handle_map_stop()
    assert runner.config.Campaign_Name == "D1"
    assert runner.config.Scheduler_Enable is True

    assert calls == [
        ("event_current", False),
        ("event_current", True),
        ("event_current", False),
        ("event_current", True),
        ("event_current", False),
        ("event_current", True),
    ]


def test_generated_event_progression_does_not_depend_on_legacy_stage_names(
    monkeypatch,
):
    runner = _runner(stage="ALPHA")
    catalog = {
        "alpha": "campaign.generated_event.synthetic.alpha",
        "omega": "campaign.generated_event.synthetic.omega",
    }

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda selector, *, auto_advance_only=False: (
            catalog if selector == "event_current" else None
        ),
    )
    _forbid_legacy_catalog(monkeypatch)

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "OMEGA"
    assert runner.config.Scheduler_Enable is True


def test_generated_event_does_not_skip_special_stage_between_regular_stages(
    monkeypatch,
):
    runner = _runner(stage="ALPHA")
    full_catalog = {
        "alpha": "campaign.generated_event.synthetic.alpha",
        "special": "campaign.generated_event.synthetic.special",
        "omega": "campaign.generated_event.synthetic.omega",
    }
    auto_advance_catalog = {
        "alpha": "campaign.generated_event.synthetic.alpha",
        "omega": "campaign.generated_event.synthetic.omega",
    }
    calls = []

    def resolver(selector, *, auto_advance_only=False):
        calls.append((selector, auto_advance_only))
        if selector != "event_current":
            return None
        return auto_advance_catalog if auto_advance_only else full_catalog

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        resolver,
    )
    _forbid_legacy_catalog(monkeypatch)

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "ALPHA"
    assert runner.config.Scheduler_Enable is False
    assert calls == [
        ("event_current", False),
        ("event_current", True),
    ]


def test_generated_event_stops_before_stage_excluded_at_end_of_catalog(monkeypatch):
    runner = _runner(stage="D3")
    full_catalog = {
        "d3": "campaign.generated_event.synthetic.d3",
        "sp": "campaign.generated_event.synthetic.sp",
    }
    auto_advance_catalog = {
        "d3": "campaign.generated_event.synthetic.d3",
    }

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda selector, *, auto_advance_only=False: (
            auto_advance_catalog
            if selector == "event_current" and auto_advance_only
            else full_catalog if selector == "event_current" else None
        ),
    )
    _forbid_legacy_catalog(monkeypatch)

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "D3"
    assert runner.config.Scheduler_Enable is False


def test_generated_event_custom_sequence_keeps_explicit_user_priority(monkeypatch):
    runner = _runner(stage="A1")
    runner.config.STAGE_INCREASE_CUSTOM = "A1 > SP"
    full_catalog = {
        "a1": "campaign.generated_event.synthetic.a1",
        "a2": "campaign.generated_event.synthetic.a2",
        "sp": "campaign.generated_event.synthetic.sp",
    }

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda selector, *, auto_advance_only=False: (
            full_catalog if selector == "event_current" else None
        ),
    )
    _forbid_legacy_catalog(monkeypatch)

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "SP"
    assert runner.config.Scheduler_Enable is True


def test_legacy_event_keeps_physical_stage_catalog_fallback(monkeypatch):
    runner = _runner(stage="A1")
    runner.config.Campaign_Event = "event_legacy"

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda _selector, *, auto_advance_only=False: None,
    )
    monkeypatch.setattr(
        fast_forward_module,
        "map_files",
        lambda selector: ["a1", "a2"] if selector == "event_legacy" else [],
    )

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "A2"
    assert runner.config.Scheduler_Enable is True


def test_corrupt_generated_catalog_stops_stage_advance_fail_closed(monkeypatch):
    runner = _runner(stage="A1")
    diagnostics = []

    def fail_resolver(_selector, *, auto_advance_only=False):
        raise EventCampaignSelectorError("повреждённая привязка")

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        fail_resolver,
    )
    monkeypatch.setattr(
        fast_forward_module,
        "map_files",
        lambda _selector: (_ for _ in ()).throw(
            AssertionError("Повреждённый generated binding не должен использовать legacy")
        ),
    )
    monkeypatch.setattr(
        fast_forward_module.logger,
        "error_context",
        lambda **kwargs: diagnostics.append(kwargs),
    )

    with pytest.raises(RequestHumanTakeover) as error:
        runner.handle_map_stop()

    assert isinstance(error.value.__cause__, EventCampaignSelectorError)
    assert diagnostics
    assert "legacy-каталог запрещён" in diagnostics[0]["impact"]


def test_auto_advance_requires_explicit_non_one_time_stage_policy():
    regular = SimpleNamespace(
        stage_entry=SimpleNamespace(one_time=False),
    )
    one_time = SimpleNamespace(
        stage_entry=SimpleNamespace(one_time=True),
    )
    unknown = SimpleNamespace(
        stage_entry=SimpleNamespace(one_time=None),
    )
    missing = SimpleNamespace(stage_entry=None)

    assert campaign_selector_module._map_allows_auto_advance(regular) is True
    assert campaign_selector_module._map_allows_auto_advance(one_time) is False
    assert campaign_selector_module._map_allows_auto_advance(unknown) is False
    assert campaign_selector_module._map_allows_auto_advance(missing) is False
