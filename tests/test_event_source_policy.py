from pathlib import Path

from module.event_datamine.artifact import load_builtin_artifact
from module.webui.event_plan import empty_event_plan, save_event_plan
from module.webui.event_shop_bridge import build_event_shop_automation_plan
from module.webui.event_source import (
    empty_event_user_state,
    event_plan_from_source,
    event_user_state_path,
    load_event_user_state,
    migrate_stage2_plan,
    save_event_user_state,
    user_state_from_plan,
)


def test_generated_facts_and_user_policy_are_separate_round_trip(tmp_path: Path):
    spec = load_builtin_artifact()["event_spec"]
    state = load_event_user_state(
        "ap", root=tmp_path / "state", legacy_root=tmp_path / "legacy"
    )
    plan = event_plan_from_source(spec, state)
    original_event = dict(plan["event"])
    plan["shop_items"][0]["selected"] = 1

    saved = user_state_from_plan(plan, state)
    save_event_user_state("ap", saved, root=tmp_path / "state")
    restored_state = load_event_user_state(
        "ap", root=tmp_path / "state", legacy_root=tmp_path / "legacy"
    )
    restored = event_plan_from_source(spec, restored_state)

    assert restored["event"] == original_event
    assert restored["shop_items"][0]["selected"] == 1
    assert set(restored_state) == {
        "schema_version",
        "source_event_id",
        "explicit_empty",
        "progress",
        "shop_selections",
        "recurring_status",
        "legacy_unverified",
    }


def test_stage2_migration_preserves_intent_without_activating_manual_facts(
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy"
    plan = empty_event_plan("EN")
    plan["event"].update({"name": "Manual old event", "farm_end": "2026-01-01"})
    plan["shop_items"] = [
        {
            "id": "3005",
            "name": "Selected",
            "price": 10,
            "stock": 2,
            "selected": 1,
            "filter": "",
        }
    ]
    plan["progress"].update({"current_pt": 900, "pt_mode": "manual"})
    save_event_plan("ap", plan, root=legacy_root)

    state = load_event_user_state(
        "ap", root=tmp_path / "state", legacy_root=legacy_root
    )
    projected = event_plan_from_source(load_builtin_artifact()["event_spec"], state)

    assert state["legacy_unverified"]["event"]["name"] == "Manual old event"
    assert state["legacy_unverified"]["manual_current_pt"] == 900
    assert state["shop_selections"] == {"3005": 1}
    assert projected["event"]["name"] == "A Rose on the High Tower"
    assert (
        next(item for item in projected["shop_items"] if item["id"] == "3005")[
            "selected"
        ]
        == 1
    )
    assert projected["progress"]["pt_mode"] == "auto"


def test_stage2_shop_selection_without_id_maps_only_by_unique_source_facts():
    old = empty_event_plan("EN")
    old["event"]["name"] = "Old manual snapshot"
    old["shop_items"] = [
        {
            "name": "Cognitive Chips",
            "price": 300,
            "stock": 10,
            "selected": 3,
            "filter": "Chip",
        }
    ]

    state = migrate_stage2_plan(old)
    projected = event_plan_from_source(load_builtin_artifact()["event_spec"], state)

    chips = next(item for item in projected["shop_items"] if item["id"] == "3009")
    assert chips["selected"] == 3
    assert state["shop_selections"] == {}


def test_source_shop_ids_feed_existing_bridge_without_weakening_special_cases():
    spec = load_builtin_artifact()["event_spec"]
    state = empty_event_user_state()
    state["shop_selections"] = {"3009": 3}
    safe = build_event_shop_automation_plan(event_plan_from_source(spec, state))
    assert safe.safe
    assert safe.tokens == ("chip:3",)

    state["shop_selections"] = {"3001": 1}
    special = build_event_shop_automation_plan(event_plan_from_source(spec, state))
    assert not special.safe
    assert special.invalid_items == ("Trafalgar",)


def test_explicit_manual_empty_is_preserved():
    old = empty_event_plan("EN")
    old["event"]["source"]["kind"] = "manual_empty"
    state = migrate_stage2_plan(old)
    projected = event_plan_from_source(load_builtin_artifact()["event_spec"], state)
    assert projected["event"]["source"]["kind"] == "manual_empty"
    assert projected["shop_items"] == []
    state["explicit_empty"] = False
    restored = event_plan_from_source(load_builtin_artifact()["event_spec"], state)
    assert restored["event"]["id"] == "en:5941"


def test_corrupt_user_state_is_preserved_before_safe_fallback(tmp_path: Path):
    root = tmp_path / "state"
    path = event_user_state_path("ap", root)
    path.parent.mkdir(parents=True)
    path.write_text('{"shop_selections":', encoding="utf-8")

    restored = load_event_user_state("ap", root=root, legacy_root=tmp_path / "legacy")

    assert restored["source_event_id"] == "en:5941"
    assert not path.exists()
    backups = list(root.glob(f"{path.name}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"shop_selections":'
