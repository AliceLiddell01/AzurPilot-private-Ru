import ast
import json
from pathlib import Path

import pytest

from module.event_datamine.artifact import (
    artifact_digest,
    build_artifact,
    load_artifact,
    load_builtin_artifact,
    write_artifact,
)

ROOT = Path(__file__).resolve().parents[1]


def trusted_map_facts(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "MAP"
            and target.attr
            in {
                "shape",
                "map_data",
                "map_data_loop",
                "spawn_data",
                "spawn_data_loop",
                "portal_data",
                "land_based_data",
            }
        ):
            result[target.attr] = ast.literal_eval(node.value)
    return result


def matrix(value: str):
    return [line.split() for line in value.strip().splitlines()]


def test_artifact_serialization_is_deterministic_and_tamper_evident(tmp_path: Path):
    first = build_artifact({"id": "en:1", "values": {"b": 2, "a": 1}})
    second = build_artifact({"values": {"a": 1, "b": 2}, "id": "en:1"})
    assert first == second
    assert first["digest"] == artifact_digest(first)

    path = write_artifact(tmp_path / "event.json", first)
    assert load_artifact(path) == first
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["event_spec"]["id"] = "en:2"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="Digest"):
        load_artifact(path)


def test_artifact_normalizes_nested_mapping_keys_before_digest(tmp_path: Path):
    artifact = build_artifact({"id": "en:1", "nested": {1: {2: "value"}}})
    path = write_artifact(tmp_path / "normalized.json", artifact)

    restored = load_artifact(path)

    assert restored["event_spec"]["nested"] == {"1": {"2": "value"}}
    assert restored["digest"] == artifact["digest"]
    with pytest.raises(ValueError, match="Дублирующийся JSON key"):
        build_artifact({"id": "en:1", "nested": {1: "a", "1": "b"}})


def test_invalid_replacement_does_not_destroy_previous_artifact(tmp_path: Path):
    path = write_artifact(tmp_path / "event.json", build_artifact({"id": "en:1"}))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_artifact(
            path,
            {
                "artifact_schema_version": 1,
                "event_spec": {"id": "en:2"},
                "digest": "bad",
            },
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("role", "fixture", "роль"),
        ("metadata", [], "metadata"),
    ],
)
def test_artifact_read_validates_envelope_fields(tmp_path: Path, field, value, match):
    artifact = build_artifact({"id": "en:1"})
    artifact[field] = value
    artifact["digest"] = artifact_digest(artifact)
    path = tmp_path / "invalid-envelope.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        load_artifact(path)


def test_rose_tower_golden_is_source_derived_and_complete_except_declared_gaps():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]

    assert spec["id"] == "en:5941"
    assert spec["name"] == "A Rose on the High Tower"
    assert spec["farm_end"] == "2025-06-11 23:59:59"
    assert spec["shop_end"] == "2025-06-18 23:59:59"
    assert spec["provenance"] == {
        "provider": "AzurLaneLuaScripts",
        "repository": "AzurLaneTools/AzurLaneLuaScripts",
        "revision": "f44b48853d48b400b92738b1f1cf6fcdf1d69169",
        "server": "EN",
        "activity_id": 5941,
        "schema_version": 2,
    }
    assert len(spec["maps"]) == 15
    assert len(spec["shop_items"]) == 31
    assert len(spec["milestones"]) == 43
    assert [item["id"] for item in spec["currencies"]] == [498, 499]
    assert [item["threshold"] for item in spec["milestones"]] == sorted(
        item["threshold"] for item in spec["milestones"]
    )
    assert {item["row_id"] for item in spec["shop_items"]} == set(range(3001, 3032))
    assert 71136 not in {item["row_id"] for item in spec["shop_items"]}
    filters = {item["row_id"]: item["event_shop_filter"] for item in spec["shop_items"]}
    assert filters[3001] == ""
    assert filters[3004] == ""
    assert filters[3005] == "ShipSSR"
    assert filters[3007] == "EquipUR"
    assert filters[3009] == "Chip"
    assert filters[3029] == "Coin"
    assets = {item["row_id"]: item["asset"] for item in spec["shop_items"]}
    assert assets[3005] == {
        "kind": "ship",
        "game_id": "9707071",
        "source_path": "ship_skin/9707070",
        "resolved": True,
    }
    assert assets[3007]["source_path"] == "Equips/24400"
    assert assets[3009]["source_path"] == "Props/15008"
    assert not [item for item in spec["findings"] if item["severity"] == "error"]
    assert {item["code"] for item in spec["findings"]} <= {
        "asset_unresolved",
        "map_pt_amount_unavailable",
    }

    a1 = next(item for item in spec["maps"] if item["id"] == 1920001)
    assert a1["shape"] == "I8"
    assert a1["map_data"][0] == ["--", "++", "++", "--", "--", "--", "--", "--", "--"]
    assert a1["spawn_data"] == [
        {"battle": 0, "enemy": 2, "siren": 1},
        {"battle": 1, "enemy": 1},
        {"battle": 2, "enemy": 1},
        {"battle": 3, "boss": 1, "enemy": 1},
        {"battle": 4, "enemy": 1},
    ]


def test_rose_tower_map_semantics_match_all_trusted_existing_campaign_files():
    spec = load_builtin_artifact("rose_tower.json")["event_spec"]
    runtime_maps = [item for item in spec["maps"] if item["chapter_name"] != "EXTRA"]
    assert len(runtime_maps) == 13

    for item in runtime_maps:
        path = (
            ROOT
            / "campaign"
            / "event_20250520_cn"
            / f"{item['chapter_name'].lower()}.py"
        )
        facts = trusted_map_facts(path)
        assert item["shape"] == facts["shape"], path
        assert item["map_data"] == matrix(facts["map_data"]), path
        assert item["spawn_data"] == facts["spawn_data"], path
        expected_loop = (
            matrix(facts["map_data_loop"]) if "map_data_loop" in facts else None
        )
        assert item["map_data_loop"] == expected_loop, path
        assert item["spawn_data_loop"] == facts.get("spawn_data_loop"), path
