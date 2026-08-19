from __future__ import annotations

import re
from copy import deepcopy

from module.event_datamine.runtime_policy import (
    load_generated_runtime_policy,
    runtime_map_policies,
)
from tests.event_fixture_helpers import ROOT, load_upstream_runtime_audits

SHA40 = re.compile(r"[0-9a-f]{40}")


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
            "siren_filter_steps": deepcopy(raw["battle_plan"]["siren_filter_steps"]),
        },
    }
    if "stage_entry" in raw:
        result["stage_entry"] = deepcopy(raw["stage_entry"])
    return result


def _package_parts(audit: dict) -> tuple[str, ...]:
    parts = tuple(str(audit["generated_package"]).split("."))
    assert all(parts)
    return parts


def test_runtime_policies_match_all_pinned_upstream_semantic_audits():
    expected_classification = {
        "source_facts",
        "generic_defaults",
        "validated_runtime_evidence",
        "obsolete_upstream_workaround",
        "irrelevant_implementation_detail",
    }

    for audit in load_upstream_runtime_audits():
        package_parts = _package_parts(audit)
        policy = load_generated_runtime_policy(package_parts)

        assert policy is not None
        assert audit["audit_schema_version"] == 1
        assert audit["event_id"] == policy["event_id"]
        assert audit["generated_package"] == policy["generated_package"]
        assert audit["campaign_ui_layout"] == policy["campaign_ui"]["layout"]
        assert audit["upstream"]["repository"] == policy["map_evidence"]["repository"]
        assert audit["upstream"]["revision"] == policy["map_evidence"]["revision"]
        assert set(audit["classification"]) == expected_classification

        raw_maps = audit["maps"]
        assert raw_maps
        assert len({item["map_id"] for item in raw_maps}) == len(raw_maps)
        assert len({item["chapter_name"] for item in raw_maps}) == len(raw_maps)
        for item in raw_maps:
            assert SHA40.fullmatch(item["upstream_blob_sha"])

        actual = runtime_map_policies(policy)
        assert set(actual) == {item["map_id"] for item in raw_maps}
        for raw in raw_maps:
            assert _actual_runtime_map(actual[raw["map_id"]]) == _expected_runtime_map(
                audit, raw
            )


def test_generated_maps_do_not_reintroduce_pinned_upstream_workarounds():
    forbidden = (
        "emotion_qz",
        "haorenlichade_m_",
        "ConfigBase",
        "SelectedGrids",
        "RoadGrids",
        "from module.logger import logger",
        "STAGE_INCREASE_AB",
    )

    for audit in load_upstream_runtime_audits():
        generated = ROOT / "campaign" / "generated_event"
        generated = generated.joinpath(*_package_parts(audit))
        modules = sorted(
            path for path in generated.glob("*.py") if path.name != "__init__.py"
        )
        assert modules
        for path in modules:
            content = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in content, (
                    f"{path.name}: найден устаревший токен {token!r}"
                )
