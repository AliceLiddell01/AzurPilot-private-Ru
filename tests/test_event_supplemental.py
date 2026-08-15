from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    canonical_json,
    load_artifact,
    validate_artifact,
)
from module.event_datamine.supplemental import (
    EventSupplementalError,
    enrich_event_plan_with_supplemental,
    load_supplemental,
    resolve_supplemental_artifact,
    resolve_supplemental_event_spec,
    supplemental_digest,
)
from module.event_datamine.registry import EventArtifactRegistry
from module.webui.event_source import empty_event_user_state, event_plan_from_source


PRODUCTION_ARTIFACT = BUILTIN_ARTIFACT_ROOT / "production" / "en-51101.json"
PARTIAL_CODES = {
    "asset_unresolved",
    "map_pt_amount_unavailable",
    "pt_source_kind_unknown",
    "source_name_unlocalized",
}


def _artifact() -> dict:
    return load_artifact(PRODUCTION_ARTIFACT)


def _source(spec: dict, identity: str) -> dict:
    return next(item for item in spec["pt_sources"] if item["id"] == identity)


def _farm_map(spec: dict, map_id: int) -> dict:
    return next(item for item in spec["farm"]["maps"] if item["map_id"] == map_id)


def _write_split_supplemental(root: Path, data: dict) -> None:
    event_root = root / "en-51101"
    event_root.mkdir(parents=True, exist_ok=True)
    prepared = copy.deepcopy(data)
    maps = prepared["farm"].pop("maps")
    prepared["digest"] = supplemental_digest(data)
    parts = list(prepared["map_parts"])
    chunks = (maps[:6], maps[6:12], maps[12:])
    assert len(parts) == len(chunks)
    (event_root / "manifest.json").write_text(
        json.dumps(prepared, ensure_ascii=False), encoding="utf-8"
    )
    for name, rows in zip(parts, chunks):
        (event_root / name).write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )


def test_builtin_supplemental_is_self_signed_and_bound_to_source_revision() -> None:
    artifact = _artifact()
    supplemental = load_supplemental(artifact["event_spec"]["id"])
    assert supplemental is not None
    assert supplemental["digest"] == supplemental_digest(supplemental)
    assert (
        supplemental["base_contract"]["source_revision"]
        == artifact["event_spec"]["provenance"]["revision"]
    )
    assert supplemental["base_contract"]["map_count"] == len(
        artifact["event_spec"]["maps"]
    )
    assert supplemental["base_contract"]["shop_count"] == len(
        artifact["event_spec"]["shop_items"]
    )
    assert supplemental["base_contract"]["milestone_count"] == len(
        artifact["event_spec"]["milestones"]
    )


def test_supplemental_resolution_keeps_base_artifact_immutable_and_closes_partial_gaps() -> None:
    artifact = _artifact()
    before = canonical_json(artifact)
    assert artifact["event_spec"]["source_status"] == "partial"

    resolved, resolution = resolve_supplemental_event_spec(artifact)

    assert canonical_json(artifact) == before
    assert artifact["event_spec"]["source_status"] == "partial"
    assert resolved["source_status"] == "verified"
    assert resolution["base_source_status"] == "partial"
    assert resolution["resolved_source_status"] == "verified"
    assert resolved["provenance"]["base_revision"] == resolution["base_revision"]
    assert resolved["provenance"]["revision"] == resolution["composite_revision"]
    assert resolved["provenance"]["revision"] != resolution["base_revision"]
    assert resolved["provenance"]["supplemental_digest"] == resolution[
        "supplemental_digest"
    ]

    remaining = {
        item["code"]
        for item in resolved["findings"]
        if item.get("severity") in {"warning", "error"}
    }
    assert not (remaining & PARTIAL_CODES)
    conflicts = [
        item
        for item in resolved["findings"]
        if item["code"] == "supplemental_source_conflict"
    ]
    assert conflicts
    assert conflicts[0]["severity"] == "info"
    assert conflicts[0]["evidence"]["page_note_value"] == 138550
    assert conflicts[0]["evidence"]["shop_banner_value"] == 138750


def test_current_registry_resolution_uses_valid_composite_artifact_but_get_stays_raw() -> None:
    registry = EventArtifactRegistry()
    raw = registry.get("en:51101")
    current = registry.resolve_current("EN", datetime(2026, 8, 15, 12, 0, 0))
    raw_current = registry.resolve_current(
        "EN", datetime(2026, 8, 15, 12, 0, 0), supplemental=False
    )

    assert current is not None
    assert raw_current is not None
    assert raw["event_spec"]["source_status"] == "partial"
    assert raw_current["digest"] == raw["digest"]
    assert current["event_spec"]["source_status"] == "verified"
    assert current["event_spec"]["provenance"]["revision"] != raw["event_spec"][
        "provenance"
    ]["revision"]
    assert validate_artifact(current) == current


def test_invalid_supplemental_falls_back_to_valid_raw_partial_artifact(tmp_path: Path) -> None:
    artifact = _artifact()
    supplemental = load_supplemental("en:51101")
    assert supplemental is not None
    supplemental["base_contract"]["event_name"] = "stale name"
    supplemental["digest"] = supplemental_digest(supplemental)
    _write_split_supplemental(tmp_path, supplemental)

    resolved, resolution = resolve_supplemental_artifact(
        artifact, supplemental_root=tmp_path
    )

    assert resolution["kind"] == "supplemental_rejected"
    assert resolved["event_spec"]["source_status"] == "partial"
    assert resolved["event_spec"]["provenance"]["revision"] == artifact[
        "event_spec"
    ]["provenance"]["revision"]
    assert any(
        item.get("code") == "supplemental_rejected"
        for item in resolved["event_spec"]["findings"]
    )
    assert validate_artifact(resolved) == resolved


def test_task_taxonomy_is_data_driven_and_fully_classified() -> None:
    resolved, _ = resolve_supplemental_event_spec(_artifact())

    expected = {
        27371: ("daily", 300),
        27372: ("daily", 300),
        27373: ("daily", 150),
        27374: ("one_time", 200),
        27375: ("one_time", 400),
        27376: ("one_time", 600),
        27377: ("one_time", 400),
        27378: ("one_time", 600),
        27379: ("one_time", 800),
        27388: ("one_time", 500),
        27389: ("one_time", 1500),
        27390: ("one_time", 3000),
    }
    for task_id, (kind, points) in expected.items():
        source = _source(resolved, f"task:{task_id}")
        assert source["kind"] == kind
        assert source["points"] == points
        assert source["classification_source"] == "supplemental"

    assert _source(resolved, "task:27373")["scope"] == "non_event_hard_mode"


def test_map_pt_sources_cover_base_daily_sp_and_explicit_no_pt() -> None:
    resolved, _ = resolve_supplemental_event_spec(_artifact())

    a1 = _source(resolved, "map:2050001")
    a1_daily = _source(resolved, "map-daily-first-clear:2050001")
    assert a1["points"] == 30
    assert a1_daily["points"] == 90
    assert a1_daily["base_points"] == 30
    assert a1_daily["bonus_points"] == 60
    assert a1_daily["multiplier"] == 3
    assert a1_daily["includes_base_points"] is True

    assert _source(resolved, "map:2050026")["points"] == 180
    sp = _source(resolved, "map-daily-first-clear:2050041")
    assert sp["points"] == 800
    assert sp["daily_limit"] == 1
    assert sp["recurring"] is True

    ids = {item["id"] for item in resolved["pt_sources"]}
    assert "map:2050051" not in ids
    assert "map:2050052" not in ids
    assert "map-daily-first-clear:2050051" not in ids
    assert "map-daily-first-clear:2050052" not in ids


def test_farm_metadata_preserves_map_details_and_efficiency_inputs() -> None:
    resolved, _ = resolve_supplemental_event_spec(_artifact())

    d1 = _farm_map(resolved, 2050024)
    d2 = _farm_map(resolved, 2050025)
    d3 = _farm_map(resolved, 2050026)
    sp = _farm_map(resolved, 2050041)
    ex = _farm_map(resolved, 2050051)

    assert (d1["base_points"], d1["oil"]["per_run"]) == (120, 194)
    assert (d2["base_points"], d2["oil"]["per_run"]) == (150, 245)
    assert (d3["base_points"], d3["oil"]["per_run"]) == (180, 267)
    assert d3["coins"]["map_plus_clear_range"] == [1175, 1400]
    assert d3["boss_only_ship_drops"] == ["Collett"]
    assert d3["boss_level"] == 105
    assert d3["required_battles"] == 6
    assert d3["stat_restrictions"]["average_level_gt"] == 100

    assert sp["base_points"] == 800
    assert sp["daily_limit"] == 1
    assert sp["unlock_requires"] == ["D3"]
    assert sp["boss_level"] == 110
    assert ex["grants_event_pt"] is False
    assert ex["score_counts_toward_ranking"] is False
    assert ex["map_drop_families"] == []

    rules = resolved["farm"]["rules"]
    assert rules["normal_first_clear_daily_multiplier"] == 3
    assert rules["high_efficiency_multiplier"] == 2
    assert rules["oil_per_run_includes_sortie_start_cost"] == 10
    assert rules["submarine_oil_applies_only_when_called_into_battle"] is True


def test_shop_name_resource_display_and_cross_source_totals_are_verified() -> None:
    resolved, _ = resolve_supplemental_event_spec(_artifact())

    row = next(item for item in resolved["shop_items"] if item["row_id"] == 4133)
    assert row["item_id"] == 30387
    assert row["name"] == "Gear Skin Box (Seaside Speedstars)"
    assert row["name_source"] == "supplemental"

    coins = next(item for item in resolved["shop_items"] if item["row_id"] == 4154)
    oil = next(item for item in resolved["shop_items"] if item["row_id"] == 4155)
    assert coins["asset"]["display_resolved"] is True
    assert coins["asset"]["display_name"] == "Coins"
    assert oil["asset"]["display_resolved"] is True
    assert oil["asset"]["display_name"] == "Oil"

    verification = resolved["supplemental_verification"]["shop"]
    assert verification["row_count"] == 26
    assert verification["full_buyout_cost"] == 138550
    assert verification["one_featured_copy_buyout_cost"] == 106550


def test_plan_projection_uses_static_farm_facts_without_faking_runtime_observation() -> None:
    resolved, _ = resolve_supplemental_event_spec(_artifact())
    state = empty_event_user_state()
    plan = event_plan_from_source(resolved, state, {})
    plan = enrich_event_plan_with_supplemental(plan, resolved)

    stages = {stage["name"]: stage for stage in plan["stages"]}
    assert stages["A1"]["points"] == 30
    assert stages["A1"]["points_source"] == "supplemental"
    assert stages["D3"]["points"] == 180
    assert stages["D3"]["oil"] == 267
    assert stages["D3"]["oil_source"] == "supplemental"
    assert stages["D3"]["observation_status"] == "unavailable"
    assert stages["EXTRA"]["grants_event_pt"] is False

    sources = {item["id"]: item for item in plan["pt_sources"]}
    assert sources["map-daily-first-clear:2050001"]["multiplier"] == 3
    assert sources["task:27373"]["scope"] == "non_event_hard_mode"
    assert len(plan["missions"]) == 10
    assert plan["supplemental"]["digest"] == resolved["supplemental"]["digest"]


def test_tampered_supplemental_is_rejected(tmp_path: Path) -> None:
    source = load_supplemental("en:51101")
    assert source is not None
    _write_split_supplemental(tmp_path, source)
    part = tmp_path / "en-51101" / source["map_parts"][0]
    rows = json.loads(part.read_text(encoding="utf-8"))
    rows[0]["base_points"] = 999
    part.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(EventSupplementalError, match="Digest"):
        load_supplemental("en:51101", root=tmp_path)


def test_stale_supplemental_is_rejected_when_base_contract_changes() -> None:
    artifact = _artifact()
    changed = copy.deepcopy(artifact)
    changed["event_spec"]["name"] = "Changed upstream event name"

    with pytest.raises(EventSupplementalError, match="base_contract"):
        resolve_supplemental_event_spec(changed)


def test_missing_supplemental_keeps_sharecfg_only_contract(tmp_path: Path) -> None:
    artifact = _artifact()
    resolved, resolution = resolve_supplemental_event_spec(
        artifact, supplemental_root=tmp_path
    )

    assert resolved["source_status"] == artifact["event_spec"]["source_status"]
    assert resolved["provenance"]["revision"] == artifact["event_spec"]["provenance"][
        "revision"
    ]
    assert resolution["kind"] == "sharecfg_only"
    assert resolution["supplemental_digest"] == ""
