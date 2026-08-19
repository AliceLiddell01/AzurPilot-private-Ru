import pytest

import module.campaign.run as campaign_run_module
from module.campaign.run import CampaignRun
from module.event_datamine.campaign_selector import EventCampaignSelectorError
from module.exception import RequestHumanTakeover


def test_invalid_generated_selector_stops_before_legacy_fallback(monkeypatch):
    runner = CampaignRun.__new__(CampaignRun)
    logged = []

    def fail_resolver(_selector, _stage):
        raise EventCampaignSelectorError("повреждённый selector binding")

    def reject_legacy(*_args, **_kwargs):
        raise AssertionError(
            "Невалидный generated selector не должен проваливаться в legacy routing"
        )

    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        fail_resolver,
    )
    monkeypatch.setattr(
        campaign_run_module.logger,
        "error_context",
        lambda **kwargs: logged.append(kwargs),
    )
    runner.handle_stage_name = reject_legacy

    with pytest.raises(RequestHumanTakeover) as error:
        runner.run("A1", folder="event_fixture", total=-1)

    assert isinstance(error.value.__cause__, EventCampaignSelectorError)
    assert logged
    assert "legacy-карту запрещён" in logged[0]["impact"]
