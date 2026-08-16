from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from module.event_datamine.artifact import load_builtin_artifact
from module.webui.event_observation import load_event_observation
from module.webui.event_shop_observation import (
    invalidate_event_shop_observation,
    persist_event_shop_observation,
    reconcile_event_shop,
)
from module.webui.event_source import empty_event_user_state, event_plan_from_source


@dataclass
class RuntimeItem:
    group: str
    sub_genre: str | None
    tier: str | None
    price: int
    total_count: int
    count: int
    cost: str
    amount: int = 1


def test_unique_exact_match_derives_purchased_without_changing_desired_policy(tmp_path):
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    catalog = next(item for item in spec["shop_items"] if item["row_id"] == 3009)
    runtime = RuntimeItem(
        "chip",
        None,
        None,
        catalog["price"],
        catalog["stock"],
        catalog["stock"] - 2,
        "pt",
        catalog["amount"],
    )

    rows, findings = reconcile_event_shop(spec, [runtime])

    assert findings == []
    assert rows[0]["status"] == "matched"
    assert rows[0]["row_id"] == 3009
    assert rows[0]["purchased"] == 2
    assert "selected" not in rows[0]

    persist_event_shop_observation(
        instance="ap",
        spec=spec,
        runtime_items=[runtime],
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        root=tmp_path,
    )
    stored = load_event_observation(
        "ap",
        spec["id"],
        spec["server"],
        spec["provenance"]["revision"],
        root=tmp_path,
    )
    assert stored["shop_items"][0]["purchased"] == 2


def test_invalid_counter_and_ambiguous_catalog_fail_closed():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    invalid = RuntimeItem("chip", None, None, 300, 10, 11, "pt")
    ambiguous = RuntimeItem("box", None, "t4", 300, 4, 2, "pt")

    rows, findings = reconcile_event_shop(spec, [invalid, ambiguous])

    assert rows[0]["status"] == "invalid_counter"
    assert rows[0]["purchased"] is None
    assert rows[1]["status"] == "ambiguous"
    assert rows[1]["row_id"] is None
    assert {item["code"] for item in findings} == {
        "shop_counter_invalid",
        "shop_match_ambiguous",
    }


def test_consistent_duplicate_runtime_claim_keeps_one_canonical_observation():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    catalog = next(item for item in spec["shop_items"] if item["row_id"] == 3009)
    item = RuntimeItem(
        "chip",
        None,
        None,
        catalog["price"],
        catalog["stock"],
        8,
        "pt",
        catalog["amount"],
    )

    rows, findings = reconcile_event_shop(spec, [item, item])

    assert findings == []
    assert [row["status"] for row in rows] == ["matched", "duplicate"]
    assert rows[0]["row_id"] == 3009
    assert rows[1]["row_id"] is None
    assert rows[1]["duplicate_of_runtime_index"] == 0
    assert rows[1]["duplicate_of_row_id"] == 3009


def test_conflicting_duplicate_runtime_claims_fail_closed():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    catalog = next(item for item in spec["shop_items"] if item["row_id"] == 3009)
    first = RuntimeItem(
        "chip",
        None,
        None,
        catalog["price"],
        catalog["stock"],
        8,
        "pt",
        catalog["amount"],
    )
    second = RuntimeItem(
        "chip",
        None,
        None,
        catalog["price"],
        catalog["stock"],
        7,
        "pt",
        catalog["amount"],
    )

    rows, findings = reconcile_event_shop(spec, [first, second])

    assert [row["status"] for row in rows] == ["ambiguous", "ambiguous"]
    assert all(row["row_id"] is None for row in rows)
    assert findings[-1]["code"] == "shop_runtime_duplicate_conflict"


def test_purchase_invalidation_removes_freshness(tmp_path):
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    persist_event_shop_observation(
        instance="ap", spec=spec, runtime_items=[], root=tmp_path
    )
    invalidate_event_shop_observation(
        instance="ap",
        event_id=spec["id"],
        server=spec["server"],
        source_revision=spec["provenance"]["revision"],
        root=tmp_path,
    )

    stored = load_event_observation(
        "ap",
        spec["id"],
        spec["server"],
        spec["provenance"]["revision"],
        root=tmp_path,
    )
    assert stored["shop_observed_at"] == ""
    assert (
        stored["findings"][-1]["code"] == "shop_observation_invalidated_after_purchase"
    )


def test_desired_quantity_never_changes_observed_purchase_count():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    catalog = next(item for item in spec["shop_items"] if item["row_id"] == 3009)
    runtime = RuntimeItem(
        "chip",
        None,
        None,
        catalog["price"],
        catalog["stock"],
        8,
        "pt",
        catalog["amount"],
    )
    rows, _ = reconcile_event_shop(spec, [runtime])
    observed_at = datetime.now(timezone.utc).isoformat()
    observation = {
        "schema_version": 1,
        "event_id": spec["id"],
        "server": spec["server"],
        "instance": "ap",
        "observed_at": observed_at,
        "source": "event_shop_scanner",
        "shop_observed_at": observed_at,
        "shop_items": rows,
    }
    state = empty_event_user_state()
    state["shop_selections"] = {"3009": 7}

    plan = event_plan_from_source(spec, state, observation)
    item = next(item for item in plan["shop_items"] if item["id"] == "3009")

    assert item["selected"] == 7
    assert item["purchased"] == 2


def test_runtime_invalidates_snapshot_before_attempting_purchase():
    source = (
        Path(__file__).resolve().parents[1] / "module/shop_event/shop_event.py"
    ).read_text(encoding="utf-8")
    method = source[
        source.index("    def event_shop_buy_item(") : source.index(
            "    def get_current_pts("
        )
    ]

    assert method.index("invalidate_event_shop_observation(") < method.index(
        "return super().event_shop_buy_item"
    )
