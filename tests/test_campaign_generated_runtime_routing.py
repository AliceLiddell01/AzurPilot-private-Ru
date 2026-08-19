import json
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

import module.campaign.run as campaign_run_module
from module.campaign.run import CampaignRun
from module.event_datamine.campaign_selector import resolve_generated_campaign_module
from module.exception import RequestHumanTakeover
from tests.event_fixture_helpers import (
    ROOT,
    artifact_active_time,
    current_fixture_identity,
    production_artifact,
)


class _RuntimeConfig:
    StopCondition_RunCount = 0

    def __init__(self):
        self.modified = []

    def override(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _MergeConfig:
    def merge(self, _other):
        return self


def _current_selector() -> str:
    _, server, *_ = current_fixture_identity()
    args_data = json.loads(
        (ROOT / "module" / "config" / "argument" / "args.json").read_text(
            encoding="utf-8"
        )
    )
    event_arg = args_data["Event"]["Campaign"]["Event"]
    selectors = [
        str(item)
        for item in event_arg.get(f"option_{server.lower()}", [])
        if str(item).startswith("event_")
    ]
    assert selectors
    return selectors[-1]


def _verified_current_stages() -> list[tuple[str, str]]:
    artifact = production_artifact()
    stages = []
    for item in artifact["metadata"]["generated_maps"]:
        if item.get("source_status") != "verified" or not item.get("module"):
            continue
        module = str(item["module"])
        stages.append((PurePosixPath(module).stem.lower(), module))
    assert stages
    return stages


def _fake_campaign_module():
    return SimpleNamespace(
        Config=lambda: object(),
        Campaign=lambda config, device: SimpleNamespace(
            config=config,
            device=device,
        ),
    )


def test_run_resolves_generated_stage_before_legacy_normalization(monkeypatch):
    target = "campaign.generated_event.en_current.a1"
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _RuntimeConfig()
    runner.device = object()
    captured = {}

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: (
            target
            if (selector, stage) == ("event_legacy_selector", "a1")
            else None
        ),
    )

    def reject_legacy_normalization(*_args, **_kwargs):
        raise AssertionError(
            "Текущий generated-этап не должен проходить legacy-нормализацию"
        )

    runner.handle_stage_name = reject_legacy_normalization

    def fake_load(name, folder="campaign_main"):
        captured.update(name=name, folder=folder)
        runner.campaign = SimpleNamespace(ensure_auto_search_exit=lambda: None)
        return True

    runner.load_campaign = fake_load

    # Отрицательный total завершает цикл сразу после production-routing и load.
    runner.run("A1", folder="event_legacy_selector", total=-1)

    assert captured == {
        "name": "a1",
        "folder": "event_legacy_selector",
    }
    assert runner.config.Campaign_Name == "a1"
    assert runner.config.Campaign_Event == "event_legacy_selector"


def test_run_resolves_generated_stage_after_dynamic_folder_routing(monkeypatch):
    target = "campaign.generated_event.en_current.a1"
    routing_time = object()
    resolver_calls = []
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _RuntimeConfig()
    runner.device = object()
    captured = {}

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: routing_time)

    def resolve(selector, stage, *, now):
        resolver_calls.append((selector, stage, now))
        if (selector, stage) == ("event_routed_selector", "t1"):
            return target
        return None

    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        resolve,
    )
    runner.handle_stage_name = lambda name, folder, mode: (
        "t1",
        "event_routed_selector",
    )

    def fake_load(name, folder="campaign_main"):
        captured.update(name=name, folder=folder)
        runner.campaign = SimpleNamespace(ensure_auto_search_exit=lambda: None)
        return True

    runner.load_campaign = fake_load

    runner.run("A1", folder="campaign_main", total=-1)

    assert resolver_calls == [
        ("campaign_main", "a1", routing_time),
        ("event_routed_selector", "t1", routing_time),
    ]
    assert captured == {
        "name": "a1",
        "folder": "event_routed_selector",
    }
    assert runner.config.Campaign_Name == "a1"
    assert runner.config.Campaign_Event == "event_routed_selector"


def test_every_verified_current_stage_uses_generated_runtime_routing(monkeypatch):
    artifact = production_artifact()
    now = artifact_active_time(artifact)
    selector = _current_selector()
    stages = _verified_current_stages()

    for stage, module in stages:
        target = resolve_generated_campaign_module(selector, stage, now=now)
        assert target == "campaign.generated_event." + ".".join(
            PurePosixPath(module).with_suffix("").parts
        )

    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _RuntimeConfig()
    runner.device = object()
    captured = []

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: now)

    def reject_legacy_normalization(*_args, **_kwargs):
        raise AssertionError(
            "Ни одна verified current generated-карта не должна проходить "
            "legacy-нормализацию"
        )

    runner.handle_stage_name = reject_legacy_normalization

    def fake_load(name, folder="campaign_main"):
        captured.append((name, folder))
        runner.campaign = SimpleNamespace(ensure_auto_search_exit=lambda: None)
        return True

    runner.load_campaign = fake_load

    for stage, _module in stages:
        runner.run(stage.upper(), folder=selector, total=-1)
        assert runner.config.Campaign_Name == stage
        assert runner.config.Campaign_Event == selector

    assert captured == [(stage, selector) for stage, _module in stages]


def test_load_campaign_imports_resolved_generated_module_directly(monkeypatch):
    target = "campaign.generated_event.en_current.sp"
    fake_module = _fake_campaign_module()
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _MergeConfig()
    runner.device = object()
    imports = []
    adaptations = []

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: target,
    )
    monkeypatch.setattr(
        campaign_run_module.importlib,
        "import_module",
        lambda name, package=None: imports.append((name, package)) or fake_module,
    )
    monkeypatch.setattr(
        campaign_run_module,
        "generated_campaign_ui_layout",
        lambda module_name: "20241219",
    )
    monkeypatch.setattr(
        campaign_run_module,
        "_adapt_generated_campaign_ui",
        lambda module, layout: adaptations.append((module, layout)),
    )

    assert runner.load_campaign("sp", folder="event_legacy_selector") is True

    assert imports == [(target, None)]
    assert adaptations == [(fake_module, "20241219")]
    assert runner.name == "sp"
    assert runner.stage == "sp"


def test_load_campaign_reloads_when_generated_source_changes(monkeypatch):
    first_target = "campaign.generated_event.en_first.a1"
    second_target = "campaign.generated_event.en_second.a1"
    current_target = {"value": first_target}
    fake_module = _fake_campaign_module()
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _MergeConfig()
    runner.device = object()
    imports = []

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: current_target["value"],
    )
    monkeypatch.setattr(
        campaign_run_module.importlib,
        "import_module",
        lambda name, package=None: imports.append((name, package)) or fake_module,
    )
    monkeypatch.setattr(
        campaign_run_module,
        "generated_campaign_ui_layout",
        lambda module_name: "20241219",
    )
    monkeypatch.setattr(
        campaign_run_module,
        "_adapt_generated_campaign_ui",
        lambda module, layout: None,
    )

    assert runner.load_campaign("a1", folder="event_selector") is True
    assert runner.load_campaign("a1", folder="event_selector") is False

    current_target["value"] = second_target
    assert runner.load_campaign("a1", folder="event_selector") is True

    assert imports == [
        (first_target, None),
        (second_target, None),
    ]


def test_load_campaign_keeps_legacy_import_as_fallback(monkeypatch):
    fake_module = _fake_campaign_module()
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _MergeConfig()
    runner.device = object()
    imports = []

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: None,
    )
    monkeypatch.setattr(
        campaign_run_module.importlib,
        "import_module",
        lambda name, package=None: imports.append((name, package)) or fake_module,
    )

    assert runner.load_campaign("a1", folder="event_historical") is True
    assert imports == [(".a1", "campaign.event_historical")]


def test_load_campaign_preserves_missing_map_takeover(monkeypatch):
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _MergeConfig()
    runner.device = object()

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: None,
    )

    def missing_map(_name, _package=None):
        raise ModuleNotFoundError(
            "No module named 'campaign.event_historical.a1'",
            name="campaign.event_historical.a1",
        )

    monkeypatch.setattr(
        campaign_run_module.importlib,
        "import_module",
        missing_map,
    )
    monkeypatch.setattr(campaign_run_module.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(campaign_run_module, "map_files", lambda _folder: [])

    with pytest.raises(RequestHumanTakeover):
        runner.load_campaign("a1", folder="event_historical")


def test_load_campaign_does_not_mask_internal_module_error(monkeypatch):
    runner = CampaignRun.__new__(CampaignRun)
    runner.config = _MergeConfig()
    runner.device = object()

    monkeypatch.setattr(campaign_run_module, "current_time", lambda: object())
    monkeypatch.setattr(
        campaign_run_module,
        "resolve_generated_campaign_module",
        lambda selector, stage, *, now: None,
    )

    def missing_dependency(_name, _package=None):
        raise ModuleNotFoundError(
            "No module named 'module.missing_dependency'",
            name="module.missing_dependency",
        )

    monkeypatch.setattr(
        campaign_run_module.importlib,
        "import_module",
        missing_dependency,
    )

    with pytest.raises(ModuleNotFoundError) as error:
        runner.load_campaign("a1", folder="event_historical")

    assert error.value.name == "module.missing_dependency"
