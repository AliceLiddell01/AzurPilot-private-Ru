from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from campaign import _apply_generated_campaign_ui_policy
from module.event_datamine.runtime_policy import (
    load_generated_runtime_policy,
    runtime_map_policies,
)

ROOT = Path(__file__).resolve().parents[1]
AUDIT = (
    ROOT
    / "tests"
    / "fixtures"
    / "event_datamine"
    / "upstream_runtime_semantics"
    / "en-51101-857deff.json"
)
GENERATED = ROOT / "campaign" / "generated_event" / "en_51101"
SHA40 = re.compile(r"[0-9a-f]{40}")


def _load_audit() -> dict:
    return json.loads(AUDIT.read_text(encoding="utf-8"))


def _line_policy(value) -> dict:
    result = {
        "height": list(value.height),
        "prominence": value.prominence,
        "distance": value.distance,
    }
    if value.width is not None:
        result["width"] = list(value.width)
    if value.wlen is not None:
        result["wlen"] = value.wlen
    return result


def _detector_policy(value) -> dict:
    result = {
        "internal_lines": _line_policy(value.internal_lines),
        "edge_lines": _line_policy(value.edge_lines),
        "swipe": {
            "adb": list(value.swipe.adb),
            "minitouch": list(value.swipe.minitouch),
            "maatouch": list(value.swipe.maatouch),
        },
    }
    if value.walk_use_current_fleet is not None:
        result["walk_use_current_fleet"] = value.walk_use_current_fleet
    if value.ensure_edge_insight_corner is not None:
        result["ensure_edge_insight_corner"] = value.ensure_edge_insight_corner
    return result


def _actual_runtime_map(value) -> dict:
    result = {
        "map_id": value.map_id,
        "chapter_name": value.chapter_name,
        "source_path": value.source_path,
        "siren_recognition": {
            "templates": list(value.siren_recognition.templates),
            "boss_icon_small": value.siren_recognition.boss_icon_small,
        },
        "boss_clear": {"strategy": value.boss_clear.strategy},
        "camera_calibration": {
            "camera_data": list(value.camera_calibration.camera_data),
            "spawn_points": list(value.camera_calibration.spawn_points),
        },
        "detector_calibration": _detector_policy(value.detector_calibration),
        "battle_plan": {
            "enemy_filter": value.battle_plan.enemy_filter,
            "siren_filter_steps": [
                {"battle": step.battle, "preserve": step.preserve}
                for step in value.battle_plan.siren_filter_steps
            ],
        },
    }
    if value.stage_entry is not None:
        stage_entry = {}
        if value.stage_entry.one_time is not None:
            stage_entry["one_time"] = value.stage_entry.one_time
        if value.stage_entry.has_mode_switch is not None:
            stage_entry["has_mode_switch"] = value.stage_entry.has_mode_switch
        result["stage_entry"] = stage_entry
    return result


def _expected_runtime_map(audit: dict, raw: dict) -> dict:
    profile = deepcopy(audit["profiles"][raw["profile"]])
    result = {
        "map_id": raw["map_id"],
        "chapter_name": raw["chapter_name"],
        "source_path": raw["source_path"],
        **profile,
        "boss_clear": deepcopy(raw["boss_clear"]),
        "camera_calibration": deepcopy(raw["camera_calibration"]),
        "battle_plan": {
            "enemy_filter": audit["enemy_filter"],
            "siren_filter_steps": deepcopy(
                raw["battle_plan"]["siren_filter_steps"]
            ),
        },
    }
    if "stage_entry" in raw:
        result["stage_entry"] = deepcopy(raw["stage_entry"])
    return result


def test_current_runtime_policy_matches_pinned_upstream_semantics_for_all_maps():
    audit = _load_audit()
    policy = load_generated_runtime_policy(("en_51101",))

    assert policy is not None
    assert audit["audit_schema_version"] == 1
    assert audit["event_id"] == policy["event_id"] == "en:51101"
    assert audit["generated_package"] == policy["generated_package"] == "en_51101"
    assert audit["campaign_ui_layout"] == policy["campaign_ui"]["layout"]
    assert audit["upstream"]["repository"] == policy["map_evidence"]["repository"]
    assert audit["upstream"]["revision"] == policy["map_evidence"]["revision"]

    expected_classification = {
        "source_facts",
        "generic_defaults",
        "validated_runtime_evidence",
        "obsolete_upstream_workaround",
        "irrelevant_implementation_detail",
    }
    assert set(audit["classification"]) == expected_classification

    raw_maps = audit["maps"]
    assert len(raw_maps) == 13
    assert len({item["map_id"] for item in raw_maps}) == 13
    assert len({item["chapter_name"] for item in raw_maps}) == 13
    for item in raw_maps:
        assert SHA40.fullmatch(item["upstream_blob_sha"])

    actual = runtime_map_policies(policy)
    assert set(actual) == {item["map_id"] for item in raw_maps}

    for raw in raw_maps:
        assert _actual_runtime_map(actual[raw["map_id"]]) == _expected_runtime_map(
            audit, raw
        )


def test_generated_maps_do_not_reintroduce_upstream_generator_workarounds():
    forbidden = (
        "emotion_qz",
        "haorenlichade_m_",
        "ConfigBase",
        "SelectedGrids",
        "RoadGrids",
        "from module.logger import logger",
        "STAGE_INCREASE_AB",
    )
    modules = sorted(
        path for path in GENERATED.glob("*.py") if path.name != "__init__.py"
    )

    assert len(modules) == 13
    for path in modules:
        content = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in content, f"{path.name}: найден устаревший токен {token!r}"


def test_20241219_layout_preserves_explicit_sp_mode_switch_override():
    class Config:
        MAP_CHAPTER_SWITCH_20241219 = False
        MAP_CHAPTER_SWITCH_20241219_SP = False
        MAP_CHAPTER_SWITCH_20241219_SPEX = False
        MAP_CHAPTER_SWITCH_20260326 = False
        MAP_HAS_MODE_SWITCH = False

    module = SimpleNamespace(Config=Config)

    _apply_generated_campaign_ui_policy(module, "20241219")

    assert module.Config.MAP_CHAPTER_SWITCH_20241219 is True
    assert module.Config.STAGE_ENTRANCE == ["half", "20240725"]
    assert module.Config.MAP_HAS_MODE_SWITCH is False
