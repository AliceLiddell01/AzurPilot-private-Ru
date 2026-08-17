from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from module.event_datamine.artifact import canonical_json, validate_artifact
from module.event_datamine.registry import EventArtifactRegistry
from module.event_datamine.supplemental import (
    EventSupplementalError,
    enrich_event_plan_with_supplemental,
    load_supplemental,
    resolve_supplemental_artifact,
    resolve_supplemental_event_spec,
    supplemental_digest,
)
from module.webui.event_source import empty_event_user_state, event_plan_from_source
from tests.event_fixture_helpers import (
    artifact_active_time,
    current_fixture_identity,
    production_artifact,
    write_split_supplemental,
)

PARTIAL_CODES = {
    "asset_unresolved",
    "map_pt_amount_unavailable",
    "pt_source_kind_unknown",
    "source_name_unlocalized",
}


def _artifact() -> dict:
    return production_artifact()


def _source(spec: dict, identity: str) -> dict:
    return next(item for item in spec["pt_sources"] if item["id"] == identity)


def _farm_map(spec: dict, map_id: int) -> dict:
    return next(item for item in spec["farm"]["maps"] if item["map_id"] == map_id)


def test_builtin_supplemental_is_self_signed_and_bound_to_source_revision() -> None:
    artifact = _artifact()
    event_id = artifact["event_spec"]["id"]
    supplemental = load_supplemental(event_id)
    assert supplemental is not None
    assert supplemental["event_id"] == event_id
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
    assert all(item["severity"] == "info" for item in conflicts)
    for item in conflicts:
        evidence = item.get("evidence", {})
        assert "page_note_value" in evidence
        assert "shop_banner_value" in evidence
        assert evidence["page_note_value"] != evidence["shop_banner_value"]


def test_current_registry_resolution_uses_valid_composite_artifact_but_get_stays_raw() -> None:
    event_id, server, *_ = current_fixture_identity()
    registry = EventArtifactRegistry()
    raw = registry.get(event_id)
    now = artifact_active_time(raw)
    current = registry.resolve_current(server, now)
    raw_current = registry.resolve_current(server, now, supplemental=False)

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
    event_id = artifact["event_spec"]["id"]
    supplemental = load_supplemental(event_id)
    assert supplemental is not None
    supplemental["base_contract"]["event_name"] = "stale name"
    supplemental["digest"] = supplemental_digest(supplemental)
    write_split_supplemental(tmp_path, supplemental)

    resolved, resolution = resolve_supplemental_artifact(
        artifact,
        supplemental_root=tmp_path,
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


def test_task_taxonomy_is_derived_from_signed_supplemental_data() -> None:
    artifact = _artifact()
    supplemental = load_supplemental(artifact["event_spec"]["id"])
    assert supplemental is not None
    resolved, _ = resolve_supplemental_event_spec(artifact)

    for expected in supplemental["task_classification"]:
        source = _source(resolved, f"task:{expected['task_id']}")
        assert source["kind"] == expected["kind"]
        assert source["points"] == expected["expected_points"]
        assert source["classification_source"] == "supplemental"
        if "scope" in expected:
            assert source["scope"] == expected["scope"]


def test_farm_projection_preserves_signed_supplemental_facts() -> None:
    artifact = _artifact()
    supplemental = load_supplemental(artifact["event_spec"]["id"])
    assert supplemental is not None
    resolved, _ = resolve_supplemental_event_spec(artifact)

    for expected in supplemental["farm"]["maps"]:
        actual = _farm_map(resolved, expected["map_id"])
        assert actual == expected

    assert resolved["farm"]["rules"] == supplemental["farm"]["rules"]
    assert resolved["farm"]["mechanics"] == supplemental["farm"]["mechanics"]


def test_shop_projection_and_verification_are_derived_from_signed_supplemental_data() -> None:
    artifact = _artifact()
    supplemental = load_supplemental(artifact["event_spec"]["id"])
    assert supplemental is not None
    resolved, _ = resolve_supplemental_event_spec(artifact)

    rows = {item["row_id"]: item for item in resolved["shop_items"]}
    for expected in supplemental["shop_overrides"]:
        actual = rows[expected["row_id"]]
        assert actual["item_type"] == expected["expected_item_type"]
        assert actual["item_id"] == expected["expected_item_id"]
        assert actual["price"] == expected["expected_price"]
        assert actual["stock"] == expected["expected_stock"]
        assert actual["name"] == expected["name"]
        assert actual["name_source"] == "supplemental"

    assert resolved["supplemental_verification"] == supplemental["verification"]


def test_plan_projection_uses_static_farm_facts_without_faking_runtime_observation() -> None:
    artifact = _artifact()
    resolved, _ = resolve_supplemental_event_spec(artifact)
    state = empty_event_user_state()
    plan = event_plan_from_source(resolved, state, {})
    plan = enrich_event_plan_with_supplemental(plan, resolved)

    stages = {stage["name"]: stage for stage in plan["stages"]}
    for expected in resolved["farm"]["maps"]:
        stage = stages.get(expected["chapter_name"])
        if stage is None:
            continue
        assert stage["grants_event_pt"] is expected["grants_event_pt"]
        if expected.get("base_points") is not None:
            assert stage["points"] == expected["base_points"]
            assert stage["points_source"] == "supplemental"
        oil = expected.get("oil")
        if isinstance(oil, dict) and oil.get("per_run") is not None:
            assert stage["oil"] == oil["per_run"]
            assert stage["oil_source"] == "supplemental"
        assert stage["observation_status"] == "unavailable"

    assert plan["supplemental"]["digest"] == resolved["supplemental"]["digest"]


def test_tampered_supplemental_is_rejected(tmp_path: Path) -> None:
    artifact = _artifact()
    event_id = artifact["event_spec"]["id"]
    source = load_supplemental(event_id)
    assert source is not None
    event_root = write_split_supplemental(tmp_path, source)
    part = event_root / source["map_parts"][0]
    rows = json.loads(part.read_text(encoding="utf-8"))
    rows[0]["base_points"] = 999
    part.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(EventSupplementalError, match="Digest"):
        load_supplemental(event_id, root=tmp_path)


def test_stale_supplemental_is_rejected_when_base_contract_changes() -> None:
    artifact = _artifact()
    changed = copy.deepcopy(artifact)
    changed["event_spec"]["name"] = "Changed upstream event name"

    with pytest.raises(EventSupplementalError, match="base_contract"):
        resolve_supplemental_event_spec(changed)


def test_missing_supplemental_keeps_sharecfg_only_contract(tmp_path: Path) -> None:
    artifact = _artifact()
    resolved, resolution = resolve_supplemental_event_spec(
        artifact,
        supplemental_root=tmp_path,
    )

    assert resolved["source_status"] == artifact["event_spec"]["source_status"]
    assert resolved["provenance"]["revision"] == artifact["event_spec"]["provenance"][
        "revision"
    ]
    assert resolution["kind"] == "sharecfg_only"
    assert resolution["supplemental_digest"] == ""
