from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import campaign as campaign_package
from campaign import (
    _GeneratedEventAliasFinder,
    _GeneratedEventAliasLoader,
    _adapt_generated_campaign_ui,
)
from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT, load_artifact
from module.event_datamine.campaign_selector import (
    generated_campaign_package_parts,
    generated_stage_module,
    resolve_generated_campaign_module,
)
from module.event_datamine.supplemental import (
    EventSupplementalError,
    load_supplemental,
    require_int,
    supplemental_digest,
    validate_supplemental,
)
from module.webui.app import AlasGUI
from module.webui.app_event_general_presentation import EventGeneralPresentationMixin
from module.webui.app_event_general_v2 import EventGeneralV2Mixin
from module.webui.app_event_layout import EventLayoutMixin
import module.webui.app_event_layout as event_layout


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ARTIFACT = BUILTIN_ARTIFACT_ROOT / "production" / "en-51101.json"
ARGS_PATH = ROOT / "module" / "config" / "argument" / "args.json"
EVENT_GENERAL_CSS = ROOT / "assets" / "gui" / "css" / "event-general-v2-alas.css"
PINNED_NOW = datetime(2026, 8, 15, 12, 0, 0)


class _Presenter(EventGeneralPresentationMixin):
    @staticmethod
    def _fmt(value):
        return f"{int(value):,}".replace(",", " ")


class _MapPresenter(EventLayoutMixin):
    pass


@pytest.mark.parametrize("value", (True, False, 1.0, 1.5, -2.25))
def test_supplemental_integer_contract_rejects_bool_and_float(value):
    with pytest.raises(EventSupplementalError):
        require_int(value, "test.value")


@pytest.mark.parametrize(
    "value, expected", ((0, 0), (17, 17), ("42", 42), ("-3", -3))
)
def test_supplemental_integer_contract_keeps_exact_integers(value, expected):
    assert require_int(value, "test.value") == expected


@pytest.mark.parametrize(
    "field, value",
    (("base_points", True), ("base_points", 30.0), ("map_id", 2050001.5)),
)
def test_supplemental_validation_rejects_non_integer_map_fields(field, value):
    data = load_supplemental("en:51101")
    assert data is not None
    data["farm"]["maps"][0][field] = value
    data["digest"] = supplemental_digest(data)

    with pytest.raises(EventSupplementalError):
        validate_supplemental(data)


def _current_en_selector() -> str:
    args_data = json.loads(ARGS_PATH.read_text(encoding="utf-8"))
    options = args_data["Event"]["Campaign"]["Event"].get("option_en", [])
    selectors = [str(item) for item in options if str(item).startswith("event_")]
    assert selectors
    return selectors[-1]


def test_generated_campaign_package_is_derived_from_artifact_metadata():
    artifact = load_artifact(PRODUCTION_ARTIFACT)

    assert generated_campaign_package_parts(artifact) == ("en_51101",)
    assert generated_stage_module(artifact, "d3").endswith("/d3.py")
    assert generated_stage_module(artifact, "ht6").endswith("/d3.py")


def test_legacy_selector_resolves_to_current_generated_module():
    selector = _current_en_selector()
    target = resolve_generated_campaign_module(
        selector,
        "ht6",
        now=PINNED_NOW,
    )

    assert target == "campaign.generated_event.en_51101.d3"


def test_alias_loader_reuses_same_module_object(monkeypatch):
    fake_module = SimpleNamespace()
    calls: list[str] = []

    def fake_import(name):
        calls.append(name)
        return fake_module

    monkeypatch.setattr(campaign_package.importlib, "import_module", fake_import)
    monkeypatch.setattr(campaign_package, "_adapt_generated_campaign_ui", lambda module: None)
    loader = _GeneratedEventAliasLoader("campaign.generated_event.any.d3")
    spec = importlib.util.spec_from_loader("campaign.event_legacy.ht6", loader)

    assert loader.create_module(spec) is fake_module
    assert loader.create_module(spec) is fake_module
    assert calls == ["campaign.generated_event.any.d3", "campaign.generated_event.any.d3"]


def test_alias_finder_is_lazy_and_does_not_resolve_unrelated_imports(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_resolve(selector, stage, *, now):
        calls.append((selector, stage))
        return "campaign.generated_event.any.d3"

    monkeypatch.setattr(
        campaign_package,
        "resolve_generated_campaign_module",
        fake_resolve,
    )
    finder = _GeneratedEventAliasFinder(now_factory=lambda: PINNED_NOW)

    assert finder.find_spec("campaign.generated_event.any") is None
    assert calls == []
    spec = finder.find_spec("campaign.event_legacy.ht6")
    assert spec is not None
    assert isinstance(spec.loader, _GeneratedEventAliasLoader)
    assert spec.loader.target == "campaign.generated_event.any.d3"
    assert calls == [("event_legacy", "ht6")]


def test_generated_campaign_ui_adapter_uses_map_name_without_replacing_class():
    calls: list[tuple[str, str, bool]] = []

    class FakeCampaign:
        MAP = SimpleNamespace(name="D3")

        def ensure_campaign_ui(self, name, mode="normal", skip_first_screenshot=True):
            calls.append((name, mode, skip_first_screenshot))
            return "ok"

    fake_module = SimpleNamespace(
        Campaign=FakeCampaign,
        MAP=FakeCampaign.MAP,
    )
    original_class = fake_module.Campaign

    _adapt_generated_campaign_ui(fake_module)
    _adapt_generated_campaign_ui(fake_module)
    result = fake_module.Campaign().ensure_campaign_ui(
        "ht6", mode="hard", skip_first_screenshot=False
    )

    assert fake_module.Campaign is original_class
    assert result == "ok"
    assert calls == [("d3", "hard", False)]


def test_canonical_general_presentation_precedes_v2_dispatch():
    mro = AlasGUI.__mro__
    assert mro.index(EventGeneralPresentationMixin) < mro.index(EventGeneralV2Mixin)
    assert mro.index(EventGeneralV2Mixin) < mro.index(EventLayoutMixin)
    assert (
        AlasGUI._render_event_sources_v2
        is EventGeneralPresentationMixin._render_event_sources_v2
    )
    assert (
        AlasGUI._render_event_stages_v2
        is EventGeneralPresentationMixin._render_event_stages_v2
    )
    assert (
        AlasGUI._render_event_general_v2
        is EventGeneralPresentationMixin._render_event_general_v2
    )


def test_event_map_name_is_session_local_and_legacy_selector_is_hidden():
    presenter = _MapPresenter()
    presenter.ALAS_ARGS = {
        "Event": {
            "Campaign": {
                "Event": {
                    "type": "select",
                    "value": "event_legacy",
                    "option": ["event_legacy"],
                    "option_en": ["event_legacy"],
                }
            }
        }
    }
    presenter._current_event_name = lambda config: "Current Event"
    config = {
        "Alas": {"Emulator": {"PackageName": "com.YoStarEN.AzurLane"}},
        "Event": {"Campaign": {"Event": "event_legacy"}},
    }

    task_args, returned_config, event_name = presenter._prepare_event_map_args(
        "Event", config
    )

    assert returned_config is config
    assert event_name == "Current Event"
    assert task_args["Campaign"]["Event"]["display"] == "hide"
    assert task_args["Campaign"]["Event"]["value"] == "event_legacy"
    assert config["Event"]["Campaign"]["Event"] == "event_legacy"


def test_advanced_groups_render_directly_inside_collapse_without_dom_reparent(monkeypatch):
    presenter = _MapPresenter()
    scopes: list[str] = []
    rendered: list[tuple[str, str]] = []

    class _Output:
        def style(self, value):
            return self

    @contextmanager
    def fake_scope(name, clear=False):
        scopes.append(name)
        try:
            yield
        finally:
            scopes.pop()

    monkeypatch.setattr(event_layout, "use_scope", fake_scope)
    monkeypatch.setattr(event_layout, "put_html", lambda value: value)
    monkeypatch.setattr(event_layout, "put_scope", lambda name: name)
    monkeypatch.setattr(event_layout, "put_collapse", lambda *args, **kwargs: _Output())
    monkeypatch.setattr(
        event_layout,
        "run_js",
        lambda *_args, **_kwargs: pytest.fail("DOM reparent больше не допускается"),
    )
    presenter._render_named_group = (
        lambda task, name, group_map, config, navigator=True: rendered.append(
            (name, scopes[-1])
        )
        or 1
    )

    presenter._render_advanced(
        task="Event",
        title="Расширенные настройки карты",
        description="Редкие параметры",
        names=("Submarine", "HpControl"),
        group_map={"Submarine": object(), "HpControl": object()},
        config={},
    )

    assert len(rendered) == 2
    assert rendered[0][0] == "Submarine"
    assert rendered[1][0] == "HpControl"
    assert rendered[0][1].startswith("event_advanced_event_")
    assert rendered[1][1] == rendered[0][1]


def test_pt_sources_for_same_map_are_presented_as_one_card():
    presenter = _Presenter()
    plan = {
        "stages": [
            {"id": "2050001", "name": "A1", "title": "Idol and Detective"}
        ]
    }
    sources = [
        {
            "id": "map:2050001",
            "kind": "repeatable_map_clear",
            "name": "A1",
            "points": 30,
            "source_ids": [2050001],
        },
        {
            "id": "map-daily-first-clear:2050001",
            "kind": "daily_first_clear",
            "name": "A1",
            "points": 90,
            "source_ids": [2050001],
            "multiplier": 3,
        },
    ]

    cards = presenter._combined_map_pt_sources(plan, sources)

    assert len(cards) == 1
    assert cards[0]["name"] == "A1"
    assert cards[0]["title"] == "Idol and Detective"
    assert [item["points"] for item in cards[0]["sources"]] == [30, 90]
    rendered = presenter._render_source_card(cards[0])
    assert "Обычное прохождение" in rendered
    assert "Первое прохождение дня" in rendered
    assert "×3" in rendered


def test_verified_coin_range_and_stage_title_are_used_for_farm_presentation():
    presenter = _Presenter()
    stage = {
        "name": "D3",
        "title": "Rain Upon Flowery Seas",
        "points": 180,
        "oil": 267,
        "coins": {"map_plus_clear_range": [1175, 1400]},
        "required_battles": 6,
        "clear_rewards": [["Wisdom Cube", 2], ["Coins", 1500]],
        "three_star_rewards": [["T3 Battleship Retrofit Blueprint", 1]],
    }

    assert presenter._format_coin_income(stage) == "1 175–1 400"
    rendered = presenter._render_farm_card(stage, 131790)
    assert "D3" in rendered
    assert "Rain Upon Flowery Seas" in rendered
    assert "Доход за проход" in rendered
    assert "1 175–1 400" in rendered
    assert "Затраты" in rendered
    assert "267" in rendered
    assert "Награда за первое прохождение" in rendered
    assert "Награда за 3★" in rendered


def test_observed_coin_value_prevents_technical_stage_collapse():
    presenter = _Presenter()
    common = {
        "name": "EXTRA",
        "title": "Pandora's Wish",
        "mode": "special",
        "points": None,
        "oil": None,
        "coins": None,
        "clear_rewards": [["Cognitive Chips", 500]],
        "three_star_rewards": [],
        "required_battles": 0,
        "daily_limit": None,
        "grants_event_pt": False,
    }
    plan = {
        "stages": [
            {**common, "id": "2050051", "coin": 10},
            {**common, "id": "2050052", "coin": 20},
        ]
    }

    stages = presenter._user_facing_stages(plan)

    assert len(stages) == 2


def test_identical_technical_extra_variants_collapse_only_in_user_facing_projection():
    presenter = _Presenter()
    common = {
        "name": "EXTRA",
        "title": "Pandora's Wish",
        "mode": "special",
        "points": None,
        "oil": None,
        "coin": None,
        "coins": None,
        "clear_rewards": [["Cognitive Chips", 500]],
        "three_star_rewards": [],
        "required_battles": 0,
        "daily_limit": None,
        "grants_event_pt": False,
    }
    plan = {
        "stages": [
            {**common, "id": "2050051"},
            {**common, "id": "2050052"},
        ]
    }

    stages = presenter._user_facing_stages(plan)

    assert len(stages) == 1
    assert stages[0]["name"] == "EXTRA"
    assert stages[0]["variant_ids"] == ["2050051", "2050052"]
    assert len(plan["stages"]) == 2


def test_event_general_scope_layout_contract():
    top, main = EventGeneralPresentationMixin._event_general_scope_layout()

    assert top == ("group_EventMainColumn", "group_EventSideColumn")
    assert main == ("group_EventSources", "group_EventStages")


def test_canonical_css_caps_sparse_cards_and_has_no_main_column_surface():
    css = EVENT_GENERAL_CSS.read_text(encoding="utf-8")

    assert ".event-map-group-special .event-source-grid-v2" in css
    assert "minmax(230px, 320px)" in css
    assert "justify-content: start" in css
    assert "#pywebio-scope-group_EventMainColumn" in css
    assert "background: transparent !important" in css
    assert ".event-map-current-event" in css
    assert "event-general-v2-polish" not in css
    assert "event-general-v2-acceptance" not in css
