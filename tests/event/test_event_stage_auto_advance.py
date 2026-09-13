from types import SimpleNamespace

import pytest

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


def test_generated_event_advances_through_verified_catalog_without_legacy_files(
    monkeypatch,
):
    runner = _runner(stage="A1")
    catalog = {
        "a1": "campaign.generated_event.synthetic.a1",
        "a2": "campaign.generated_event.synthetic.a2",
        "a3": "campaign.generated_event.synthetic.a3",
        "b1": "campaign.generated_event.synthetic.b1",
    }

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda selector: catalog if selector == "event_current" else None,
    )
    monkeypatch.setattr(
        fast_forward_module,
        "map_files",
        lambda _selector: (_ for _ in ()).throw(
            AssertionError("Generated Event не должен читать legacy-каталог")
        ),
    )

    runner.handle_map_stop()
    assert runner.config.Campaign_Name == "A2"
    assert runner.config.Scheduler_Enable is True

    runner.config.Campaign_Name = "A3"
    runner.handle_map_stop()
    assert runner.config.Campaign_Name == "B1"
    assert runner.config.Scheduler_Enable is True


def test_generated_event_does_not_fall_back_to_legacy_for_missing_next_stage(
    monkeypatch,
):
    runner = _runner(stage="A1")

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda _selector: {
            "a1": "campaign.generated_event.synthetic.a1",
        },
    )
    monkeypatch.setattr(
        fast_forward_module,
        "map_files",
        lambda _selector: (_ for _ in ()).throw(
            AssertionError("Generated Event не должен проваливаться в legacy-каталог")
        ),
    )

    runner.handle_map_stop()

    assert runner.config.Campaign_Name == "A1"
    assert runner.config.Scheduler_Enable is False


def test_legacy_event_keeps_physical_stage_catalog_fallback(monkeypatch):
    runner = _runner(stage="A1")
    runner.config.Campaign_Event = "event_legacy"

    monkeypatch.setattr(
        fast_forward_module,
        "resolve_generated_campaign_modules",
        lambda _selector: None,
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

    def fail_resolver(_selector):
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
