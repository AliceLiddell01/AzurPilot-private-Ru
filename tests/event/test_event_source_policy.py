from pathlib import Path

from module.event_datamine.artifact import load_builtin_artifact
from module.webui.event_plan import empty_event_plan, save_event_plan
from module.webui.event_shop_bridge import build_event_shop_automation_plan
from module.webui.event_source import (
    EVENT_USER_STATE_SCHEMA_VERSION,
    empty_event_user_state,
    event_plan_from_source,
    event_user_state_path,
    load_event_user_state,
    migrate_legacy_event_plan,
    normalize_event_user_state,
    save_event_user_state,
    user_state_from_plan,
)


def test_generated_facts_and_user_policy_are_separate_round_trip(tmp_path: Path):
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
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
        "shop_selections",
    }
    assert restored_state["schema_version"] == EVENT_USER_STATE_SCHEMA_VERSION == 3
    assert restored_state["source_event_id"] == spec["id"]


def test_legacy_migration_preserves_only_source_bound_user_intent(tmp_path: Path):
    legacy_root = tmp_path / "legacy"
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    plan = empty_event_plan("EN")
    plan["event"].update(
        {
            "id": spec["id"],
            "name": "Manual old event",
            "farm_end": "2026-01-01",
        }
    )
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
    legacy_path = next(legacy_root.glob("*.json"))

    state = load_event_user_state(
        "ap", root=tmp_path / "state", legacy_root=legacy_root
    )
    projected = event_plan_from_source(spec, state)

    assert state == {
        "schema_version": 3,
        "source_event_id": spec["id"],
        "explicit_empty": False,
        "shop_selections": {"3005": 1},
    }
    assert projected["event"]["name"] == "A Rose on the High Tower"
    assert (
        next(item for item in projected["shop_items"] if item["id"] == "3005")[
            "selected"
        ]
        == 1
    )
    assert projected["progress"]["current_pt"] is None
    assert projected["progress"]["status"] == "unavailable"
    assert not legacy_path.exists()


def test_legacy_selection_without_source_identity_is_discarded_fail_closed():
    old = empty_event_plan("EN")
    old["event"]["name"] = "Old manual snapshot"
    old["shop_items"] = [
        {
            "id": "3009",
            "name": "Cognitive Chips",
            "price": 300,
            "stock": 10,
            "selected": 3,
            "filter": "Chip",
        }
    ]

    state = migrate_legacy_event_plan(old)
    projected = event_plan_from_source(
        load_builtin_artifact("rose_tower.json")["event_spec"], state
    )

    chips = next(item for item in projected["shop_items"] if item["id"] == "3009")
    assert chips["selected"] == 0
    assert state["source_event_id"] == ""
    assert state["shop_selections"] == {}


def test_legacy_shop_selection_without_row_id_is_discarded_fail_closed():
    old = empty_event_plan("EN")
    old["event"]["id"] = "en:5941"
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

    state = migrate_legacy_event_plan(old)
    projected = event_plan_from_source(
        load_builtin_artifact("rose_tower.json")["event_spec"], state
    )

    chips = next(item for item in projected["shop_items"] if item["id"] == "3009")
    assert chips["selected"] == 0
    assert state["shop_selections"] == {}


def test_source_shop_ids_feed_existing_bridge_without_weakening_special_cases():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    state = empty_event_user_state()
    state["source_event_id"] = spec["id"]
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
    state = migrate_legacy_event_plan(old)
    projected = event_plan_from_source(
        load_builtin_artifact("rose_tower.json")["event_spec"], state
    )
    assert projected["event"]["source"]["kind"] == "manual_empty"
    assert projected["shop_items"] == []
    state["explicit_empty"] = False
    restored = event_plan_from_source(
        load_builtin_artifact("rose_tower.json")["event_spec"], state
    )
    assert restored["event"]["id"] == "en:5941"


def test_corrupt_user_state_is_preserved_before_safe_fallback(tmp_path: Path):
    root = tmp_path / "state"
    path = event_user_state_path("ap", root)
    path.parent.mkdir(parents=True)
    path.write_text('{"shop_selections":', encoding="utf-8")

    restored = load_event_user_state("ap", root=root, legacy_root=tmp_path / "legacy")

    assert restored["source_event_id"] == ""
    assert not path.exists()
    backups = list(root.glob(f"{path.name}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"shop_selections":'


def test_old_state_is_rewritten_without_legacy_payload(tmp_path: Path):
    root = tmp_path / "state"
    path = event_user_state_path("ap", root)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema_version":2,"source_event_id":"en:old",'
        '"explicit_empty":false,"shop_selections":{"3009":2},'
        '"legacy_unverified":{"event":{"name":"old"}},'
        '"legacy_debug_evidence":{"manual_current_pt":900},'
        '"progress":{"current_pt":900},"recurring_status":{"x":true}}',
        encoding="utf-8",
    )

    restored = load_event_user_state(
        "ap", root=root, legacy_root=tmp_path / "legacy"
    )

    assert restored == {
        "schema_version": 3,
        "source_event_id": "en:old",
        "explicit_empty": False,
        "shop_selections": {"3009": 2},
    }
    persisted = path.read_text(encoding="utf-8")
    assert "legacy_" not in persisted
    assert '"progress"' not in persisted
    assert '"recurring_status"' not in persisted


def test_old_state_without_source_identity_discards_selections_on_rewrite(tmp_path: Path):
    root = tmp_path / "state"
    path = event_user_state_path("ap", root)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema_version":2,"source_event_id":"",'
        '"explicit_empty":false,"shop_selections":{"3009":2}}',
        encoding="utf-8",
    )

    restored = load_event_user_state(
        "ap", root=root, legacy_root=tmp_path / "legacy"
    )

    assert restored == {
        "schema_version": 3,
        "source_event_id": "",
        "explicit_empty": False,
        "shop_selections": {},
    }
    assert '"3009"' not in path.read_text(encoding="utf-8")


def test_policy_from_another_event_does_not_leak_into_new_source():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    state = empty_event_user_state()
    state["source_event_id"] = "en:previous"
    state["shop_selections"] = {"3009": 3}

    projected = event_plan_from_source(spec, state)

    assert all(item["selected"] == 0 for item in projected["shop_items"])
    assert projected["daily"] == []
    assert projected["extra"] == []
    assert all(
        item["observation_status"] == "unavailable" for item in projected["pt_sources"]
    )


def test_policy_without_event_identity_does_not_apply_shop_selections():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    projected = event_plan_from_source(
        spec,
        {
            "source_event_id": "",
            "shop_selections": {"3009": 3},
        },
    )

    chips = next(item for item in projected["shop_items"] if item["id"] == "3009")
    assert chips["selected"] == 0


def test_malformed_policy_quantities_and_old_manual_fields_are_ignored():
    state = normalize_event_user_state(
        {
            "source_event_id": "en:test",
            "shop_selections": {
                "valid": "2",
                "negative": -3,
                "text": "many",
                "mapping": {"value": 4},
            },
            "progress": {"current_pt": 900},
            "recurring_status": {"valid": {"skip": True}, "invalid": "yes"},
        }
    )

    assert state == {
        "schema_version": 3,
        "source_event_id": "en:test",
        "explicit_empty": False,
        "shop_selections": {"valid": 2, "negative": 0},
    }
