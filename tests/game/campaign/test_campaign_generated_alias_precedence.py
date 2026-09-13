from types import SimpleNamespace

import campaign


def test_generated_alias_never_overrides_real_legacy_module(monkeypatch):
    real_spec = SimpleNamespace(name="legacy")
    resolve_calls = []

    monkeypatch.setattr(
        campaign.importlib.machinery.PathFinder,
        "find_spec",
        lambda fullname, path=None: real_spec,
    )
    monkeypatch.setattr(
        campaign,
        "resolve_generated_campaign_module",
        lambda *args, **kwargs: resolve_calls.append((args, kwargs)),
    )

    finder = campaign._GeneratedEventAliasFinder()
    result = finder.find_spec(
        "campaign.event_stale.b1",
        path=["/legacy/campaign/event_stale"],
    )

    assert result is None
    assert resolve_calls == []


def test_generated_alias_resolves_only_when_legacy_module_is_absent(monkeypatch):
    monkeypatch.setattr(
        campaign.importlib.machinery.PathFinder,
        "find_spec",
        lambda fullname, path=None: None,
    )
    monkeypatch.setattr(
        campaign,
        "resolve_generated_campaign_module",
        lambda *args, **kwargs: "campaign.generated_event.en_current.b1",
    )
    monkeypatch.setattr(
        campaign,
        "generated_campaign_ui_layout",
        lambda resolved: "legacy",
    )

    finder = campaign._GeneratedEventAliasFinder()
    result = finder.find_spec(
        "campaign.event_stale.b1",
        path=["/missing/campaign/event_stale"],
    )

    assert result is not None
    assert result.loader.target == "campaign.generated_event.en_current.b1"


def test_generated_alias_creates_missing_intermediate_selector_package(monkeypatch):
    resolve_calls = []
    monkeypatch.setattr(
        campaign.importlib.machinery.PathFinder,
        "find_spec",
        lambda fullname, path=None: None,
    )
    monkeypatch.setattr(
        campaign,
        "resolve_generated_campaign_module",
        lambda *args, **kwargs: resolve_calls.append((args, kwargs)),
    )

    finder = campaign._GeneratedEventAliasFinder()
    result = finder.find_spec("campaign.event_stale", path=["/campaign"])

    assert result is not None
    assert result.submodule_search_locations == []
    assert isinstance(result.loader, campaign._GeneratedEventAliasPackageLoader)
    assert resolve_calls == []


def test_generated_alias_never_overrides_real_legacy_selector_package(monkeypatch):
    real_spec = SimpleNamespace(name="legacy-package")
    monkeypatch.setattr(
        campaign.importlib.machinery.PathFinder,
        "find_spec",
        lambda fullname, path=None: real_spec,
    )

    finder = campaign._GeneratedEventAliasFinder()

    assert finder.find_spec("campaign.event_stale", path=["/campaign"]) is None
