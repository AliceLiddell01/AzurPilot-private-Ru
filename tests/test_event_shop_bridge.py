from module.shop_event.selector import parse_filter_amount
from module.webui.event_plan import empty_event_plan
from module.webui.event_shop_bridge import (
    build_event_shop_automation_plan,
    canonical_event_shop_filter_token,
)


def _plan_with_items(items):
    plan = empty_event_plan()
    plan["shop_items"] = items
    return plan


def test_filter_token_validation_uses_runtime_event_shop_grammar():
    assert canonical_event_shop_filter_token(" Cube ") == "cube"
    assert canonical_event_shop_filter_token("EquipSSR") == "equipssr"
    assert canonical_event_shop_filter_token("Plate General T3") == "plategeneralt3"
    assert canonical_event_shop_filter_token("ShipUR") == "shipur"
    assert canonical_event_shop_filter_token("PtUR") == "ptur"
    assert canonical_event_shop_filter_token("Cube:3") is None
    assert canonical_event_shop_filter_token("Cube > Oil") is None
    assert canonical_event_shop_filter_token("DefinitelyNotASelector") is None
    assert canonical_event_shop_filter_token("") is None


def test_unique_partial_quantity_compiles_to_runtime_amount_suffix():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {"name": "Cube", "price": 100, "stock": 5, "selected": 3, "filter": "Cube"},
                {"name": "Oil", "price": 450, "stock": 5, "selected": 5, "filter": "Oil"},
            ]
        )
    )

    assert compiled.safe is True
    assert compiled.tokens == ("cube:3", "oil")
    assert compiled.filter_text == "cube:3 > oil"
    assert parse_filter_amount(compiled.filter_text) == {"cube": 3}


def test_invalid_or_chained_selector_fails_closed_before_runtime_config():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {
                    "name": "Injected",
                    "price": 100,
                    "stock": 1,
                    "selected": 1,
                    "filter": "Cube > Oil",
                },
                {
                    "name": "Unknown",
                    "price": 100,
                    "stock": 1,
                    "selected": 1,
                    "filter": "NoSuchFilter",
                },
            ]
        )
    )

    assert compiled.safe is False
    assert compiled.invalid_items == ("Injected", "Unknown")
    assert compiled.filter_text == ""


def test_ur_prefilter_categories_are_not_claimed_as_visual_filter_support():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {"name": "UR Ship", "price": 10000, "stock": 1, "selected": 1, "filter": "ShipUR"},
                {"name": "UR Point", "price": 100, "stock": 10, "selected": 10, "filter": "PtUR"},
            ]
        )
    )

    assert compiled.safe is False
    assert compiled.invalid_items == ("UR Ship", "UR Point")
    assert compiled.filter_text == ""


def test_equivalent_case_and_spacing_share_one_canonical_conflict_bucket():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
                {"name": "Chip B", "price": 100, "stock": 5, "selected": 0, "filter": " cHiP "},
            ]
        )
    )

    assert compiled.safe is False
    assert compiled.conflicts == {"chip": ("Chip A", "Chip B")}


def test_shared_token_partial_quantity_is_rejected_because_runtime_limit_is_aggregate():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
                {"name": "Chip B", "price": 100, "stock": 5, "selected": 3, "filter": "chip"},
            ]
        )
    )

    assert compiled.safe is False
    assert compiled.conflicts == {"chip": ("Chip A", "Chip B")}


def test_shared_token_full_selection_compiles_once():
    compiled = build_event_shop_automation_plan(
        _plan_with_items(
            [
                {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
                {"name": "Chip B", "price": 100, "stock": 5, "selected": 5, "filter": "chip"},
            ]
        )
    )

    assert compiled.safe is True
    assert compiled.tokens == ("chip",)
    assert compiled.filter_text == "chip"
