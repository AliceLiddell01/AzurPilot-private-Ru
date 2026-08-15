from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT, load_artifact
from module.event_datamine.campaign_selector import generated_campaign_selector
from module.event_datamine.supplemental import (
    EventSupplementalError,
    load_supplemental,
    require_int,
    supplemental_digest,
    validate_supplemental,
)
from module.webui.app import AlasGUI
from module.webui.app_event_acceptance import EventAcceptanceMixin
from module.webui.app_event_general_v2 import EventGeneralV2Mixin


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ARTIFACT = BUILTIN_ARTIFACT_ROOT / "production" / "en-51101.json"
ACCEPTANCE_CSS = ROOT / "assets" / "gui" / "css" / "event-general-v2-acceptance-alas.css"


class _Presenter(EventAcceptanceMixin):
    @staticmethod
    def _fmt(value):
        return f"{int(value):,}".replace(",", " ")


@pytest.mark.parametrize("value", (True, False, 1.0, 1.5, -2.25))
def test_supplemental_integer_contract_rejects_bool_and_float(value):
    with pytest.raises(EventSupplementalError):
        require_int(value, "test.value")


@pytest.mark.parametrize("value, expected", ((0, 0), (17, 17), ("42", 42), ("-3", -3)))
def test_supplemental_integer_contract_keeps_exact_integers(value, expected):
    assert require_int(value, "test.value") == expected


@pytest.mark.parametrize("field, value", (("base_points", True), ("base_points", 30.0), ("map_id", 2050001.5)))
def test_supplemental_validation_rejects_non_integer_map_fields(field, value):
    data = load_supplemental("en:51101")
    assert data is not None
    data["farm"]["maps"][0][field] = value
    data["digest"] = supplemental_digest(data)

    with pytest.raises(EventSupplementalError):
        validate_supplemental(data)


def test_generated_campaign_selector_comes_from_artifact_metadata_and_imports():
    artifact = load_artifact(PRODUCTION_ARTIFACT)
    selector = generated_campaign_selector(artifact)

    assert selector == "event_generated.en_51101"
    module = importlib.import_module(f"campaign.{selector}.d3")
    assert hasattr(module, "MAP")
    assert hasattr(module, "Campaign")


def test_campaign_selector_rejects_mixed_generated_packages():
    artifact = {
        "metadata": {
            "generated_maps": [
                {"source_status": "verified", "module": "first/a1.py"},
                {"source_status": "verified", "module": "second/a2.py"},
            ]
        }
    }
    with pytest.raises(ValueError):
        generated_campaign_selector(artifact)


def test_acceptance_mixin_precedes_legacy_event_general_renderer():
    mro = AlasGUI.__mro__
    assert mro.index(EventAcceptanceMixin) < mro.index(EventGeneralV2Mixin)
    assert AlasGUI._render_event_sources_v2 is EventAcceptanceMixin._render_event_sources_v2
    assert AlasGUI._render_event_stages_v2 is EventAcceptanceMixin._render_event_stages_v2
    assert AlasGUI._render_event_general_v2 is EventAcceptanceMixin._render_event_general_v2


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


def test_identical_technical_extra_variants_collapse_only_in_user_facing_projection():
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
            {**common, "id": "2050051"},
            {**common, "id": "2050052"},
        ]
    }

    stages = presenter._user_facing_stages(plan)

    assert len(stages) == 1
    assert stages[0]["name"] == "EXTRA"
    assert stages[0]["variant_ids"] == ["2050051", "2050052"]
    assert len(plan["stages"]) == 2


def test_acceptance_layout_moves_sources_and_stages_outside_top_split():
    source = inspect.getsource(EventAcceptanceMixin._render_event_general_v2)
    top_row = source.index('.style("--event-general-v2-layout--")')
    sources = source.index('put_scope("group_EventSources")')
    stages = source.index('put_scope("group_EventStages")')
    main = source.index('with use_scope("group_EventMainColumn")')

    assert top_row < sources < main
    assert top_row < stages < main
    assert 'put_scope("group_EventOverview")' in source


def test_special_stage_css_caps_sparse_card_width_and_flattens_main_column():
    css = ACCEPTANCE_CSS.read_text(encoding="utf-8")

    assert ".event-map-group-special .event-source-grid-v2" in css
    assert "minmax(230px, 320px)" in css
    assert "justify-content: start" in css
    assert "#pywebio-scope-group_EventMainColumn" in css
    assert "background: transparent !important" in css
