from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import campaign as campaign_package
from campaign import (
    _GeneratedEventAliasFinder,
    _GeneratedEventAliasLoader,
    _adapt_generated_campaign_ui,
)
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
from tests.event_fixture_helpers import (
    ROOT,
    artifact_active_time,
    current_fixture_identity,
    production_artifact,
)

ARGS_PATH = ROOT / "module" / "config" / "argument" / "args.json"
EVENT_GENERAL_CSS = ROOT / "assets" / "gui" / "css" / "event-general-v2-alas.css"


class _Presenter(EventGeneralPresentationMixin):
    @staticmethod
    def _fmt(value):
        return f"{int(value):,}".replace(",", " ")


class _MapPresenter(EventLayoutMixin):
    pass


def test_supplemental_integer_contract_rejects_non_integer_values():
    for value in (True, False, 1.0, 1.5, -2.25):
        with pytest.raises(EventSupplementalError):
            require_int(value, "test.value")


def test_supplemental_integer_contract_keeps_exact_integers():
    for value, expected in ((0, 0), (17, 17), ("42", 42), ("-3", -3)):
        assert require_int(value, "test.value") == expected


def test_supplemental_validation_rejects_non_integer_map_fields():
    artifact = production_artifact()
    source = load_supplemental(artifact["event_spec"]["id"])
    assert source is not None

    for field, value in (
        ("base_points", True),
        ("base_points", 30.0),
        ("map_id", 1.5),
    ):
        data = json.loads(json.dumps(source))
        data["farm"]["maps"][0][field] = value
        data["digest"] = supplemental_digest(data)
        with pytest.raises(EventSupplementalError):
            validate_supplemental(data)


def _current_selector() -> str:
    _, server, *_ = current_fixture_identity()
    args_data = json.loads(ARGS_PATH.read_text(encoding="utf-8"))
    event_arg = args_data["Event"]["Campaign"]["Event"]
    options = event_arg.get(f"option_{server.lower()}", [])
    selectors = [str(item) for item in options if str(item).startswith("event_")]
    assert selectors
    return selectors[-1]


def test_generated_campaign_modules_are_derived_from_artifact_metadata():
    artifact = production_artifact()
    package_parts = generated_campaign_package_parts(artifact)
    assert package_parts

    generated = [
        item
        for item in artifact["metadata"]["generated_maps"]
        if item.get("source_status") == "verified" and item.get("module")
    ]
    assert generated
    for item in generated:
        stage = PurePosixPath(item["module"]).stem
        assert generated_stage_module(artifact, stage) == item["module"]
        assert PurePosixPath(item["module"]).parent.parts == package_parts


def test_legacy_selector_resolves_to_generated_module_from_current_artifact():
    artifact = production_artifact()
    selector = _current_selector()
    compatibility_stage = "t1"
    expected = generated_stage_module(artifact, compatibility_stage)
    target = resolve_generated_campaign_module(
        selector,
        compatibility_stage,
        now=artifact_active_time(artifact),
    )

    assert target == "campaign.generated_event." + ".".join(
        PurePosixPath(expected).with_suffix("").parts
    )


def test_alias_loader_delegates_each_create_module_to_importlib(monkeypatch):
    modules = [SimpleNamespace(), SimpleNamespace()]
    calls: list[str] = []

    def fake_import(name):
        calls.append(name)
        return modules[len(calls) - 1]

    monkeypatch.setattr(campaign_package.importlib, "import_module", fake_import)
    monkeypatch.setattr(campaign_package, "_adapt_generated_campaign_ui", lambda module: None)
    loader = _GeneratedEventAliasLoader("campaign.generated_event.fixture.stage")
    spec = importlib.util.spec_from_loader("campaign.event_fixture.t1", loader)

    assert loader.create_module(spec) is modules[0]
    assert loader.create_module(spec) is modules[1]
    assert calls == [
        "campaign.generated_event.fixture.stage",
        "campaign.generated_event.fixture.stage",
    ]


def test_alias_finder_is_lazy_and_does_not_resolve_unrelated_imports(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_resolve(selector, stage, *, now):
        calls.append((selector, stage))
        return "campaign.generated_event.fixture.stage"

    monkeypatch.setattr(
        campaign_package,
        "resolve_generated_campaign_module",
        fake_resolve,
    )
    finder = _GeneratedEventAliasFinder(now_factory=lambda: artifact_active_time())

    assert finder.find_spec("campaign.generated_event.fixture") is None
    assert calls == []
    spec = finder.find_spec("campaign.event_fixture.t1")
    assert spec is not None
    assert isinstance(spec.loader, _GeneratedEventAliasLoader)
    assert spec.loader.target == "campaign.generated_event.fixture.stage"
    assert calls == [("event_fixture", "t1")]


def test_generated_campaign_ui_adapter_uses_map_name_without_replacing_class():
    calls: list[tuple[str, str, bool]] = []

    class FakeCampaign:
        MAP = SimpleNamespace(name="T1")

        def ensure_campaign_ui(self, name, mode="normal", skip_first_screenshot=True):
            calls.append((name, mode, skip_first_screenshot))
            return "ok"

    fake_module = SimpleNamespace(Campaign=FakeCampaign, MAP=FakeCampaign.MAP)
    original_class = fake_module.Campaign

    _adapt_generated_campaign_ui(fake_module)
    _adapt_generated_campaign_ui(fake_module)
    result = fake_module.Campaign().ensure_campaign_ui(
        "legacy-stage",
        mode="hard",
        skip_first_screenshot=False,
    )

    assert fake_module.Campaign is original_class
    assert result == "ok"
    assert calls == [("t1", "hard", False)]


def test_canonical_general_presentation_precedes_v2_dispatch():
    mro = AlasGUI.__mro__
    assert mro.index(EventGeneralPresentationMixin) < mro.index(EventGeneralV2Mixin)
    assert mro.index(EventGeneralV2Mixin) < mro.index(EventLayoutMixin)
    assert AlasGUI._render_event_sources_v2 is EventGeneralPresentationMixin._render_event_sources_v2
    assert AlasGUI._render_event_stages_v2 is EventGeneralPresentationMixin._render_event_stages_v2
    assert AlasGUI._render_event_general_v2 is EventGeneralPresentationMixin._render_event_general_v2


def test_event_map_name_is_session_local_and_legacy_selector_is_hidden():
    presenter = _MapPresenter()
    presenter.ALAS_ARGS = {
        "Event": {
            "Campaign": {
                "Event": {
                    "type": "select",
                    "value": "event_fixture",
                    "option": ["event_fixture"],
                    "option_en": ["event_fixture"],
                }
            }
        }
    }
    presenter._current_event_name = lambda config: "Текущее событие"
    config = {
        "Alas": {"Emulator": {"PackageName": "com.YoStarEN.AzurLane"}},
        "Event": {"Campaign": {"Event": "event_fixture"}},
    }

    task_args, returned_config, event_name = presenter._prepare_event_map_args(
        "Event",
        config,
    )

    assert returned_config is config
    assert event_name == "Текущее событие"
    assert task_args["Campaign"]["Event"]["display"] == "hide"
    assert task_args["Campaign"]["Event"]["value"] == "event_fixture"
    assert config["Event"]["Campaign"]["Event"] == "event_fixture"


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
    map_id = 101
    plan = {"stages": [{"id": str(map_id), "name": "T1", "title": "Тестовая карта"}]}
    sources = [
        {
            "id": f"map:{map_id}",
            "kind": "repeatable_map_clear",
            "name": "T1",
            "points": 30,
            "source_ids": [map_id],
        },
        {
            "id": f"map-daily-first-clear:{map_id}",
            "kind": "daily_first_clear",
            "name": "T1",
            "points": 90,
            "source_ids": [map_id],
            "multiplier": 3,
        },
    ]

    cards = presenter._combined_map_pt_sources(plan, sources)

    assert len(cards) == 1
    assert cards[0]["name"] == "T1"
    assert cards[0]["title"] == "Тестовая карта"
    assert [item["points"] for item in cards[0]["sources"]] == [30, 90]
    rendered = presenter._render_source_card(cards[0])
    assert "Обычное прохождение" in rendered
    assert "Первое прохождение дня" in rendered
    assert "×3" in rendered


def test_verified_coin_range_and_stage_title_are_used_for_farm_presentation():
    presenter = _Presenter()
    stage = {
        "name": "T3",
        "title": "Тестовая карта",
        "points": 180,
        "oil": 267,
        "coins": {"map_plus_clear_range": [1175, 1400]},
        "required_battles": 6,
        "clear_rewards": [["Wisdom Cube", 2], ["Coins", 1500]],
        "three_star_rewards": [["T3 Battleship Retrofit Blueprint", 1]],
    }

    assert presenter._format_coin_income(stage) == "1 175–1 400"
    rendered = presenter._render_farm_card(stage, 131790)
    assert "T3" in rendered
    assert "Тестовая карта" in rendered
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
        "title": "Техническая карта",
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
            {**common, "id": "9001", "coin": 10},
            {**common, "id": "9002", "coin": 20},
        ]
    }

    stages = presenter._user_facing_stages(plan)

    assert len(stages) == 2


def test_identical_technical_extra_variants_collapse_only_in_user_facing_projection():
    presenter = _Presenter()
    common = {
        "name": "EXTRA",
        "title": "Техническая карта",
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
            {**common, "id": "9001"},
            {**common, "id": "9002"},
        ]
    }

    stages = presenter._user_facing_stages(plan)

    assert len(stages) == 1
    assert stages[0]["name"] == "EXTRA"
    assert stages[0]["variant_ids"] == ["9001", "9002"]
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
