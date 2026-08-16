from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    load_artifact,
    validate_artifact,
)
from module.event_datamine.supplemental import (
    load_supplemental,
    resolve_supplemental_artifact,
    supplemental_digest,
)


PRODUCTION_ARTIFACT = BUILTIN_ARTIFACT_ROOT / "production" / "en-51101.json"


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
    for name, rows in zip(parts, chunks, strict=True):
        (event_root / name).write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )


def _corrupt_supplemental(data: dict, case: str) -> None:
    if case == "schema_version":
        data["supplemental_schema_version"] = "broken"
    elif case == "task_expected_points":
        data["task_classification"][0]["expected_points"] = "broken"
    elif case == "farm_base_points":
        data["farm"]["maps"][0]["base_points"] = "broken"
    elif case == "milestone_threshold":
        data["verification"]["milestones"]["thresholds"][0] = "broken"
    elif case == "base_map_count":
        data["base_contract"]["map_count"] = "broken"
    elif case == "resource_identity":
        data["resource_display_assets"][0].pop("resource_id")
    else:
        raise AssertionError(f"Неизвестный test case: {case}")


@pytest.mark.parametrize(
    "case",
    (
        "schema_version",
        "task_expected_points",
        "farm_base_points",
        "milestone_threshold",
        "base_map_count",
        "resource_identity",
    ),
)
def test_malformed_supplemental_falls_back_to_raw_artifact(
    tmp_path: Path, case: str
) -> None:
    artifact = load_artifact(PRODUCTION_ARTIFACT)
    supplemental = load_supplemental("en:51101")
    assert supplemental is not None
    _corrupt_supplemental(supplemental, case)
    _write_split_supplemental(tmp_path, supplemental)

    resolved, resolution = resolve_supplemental_artifact(
        artifact, supplemental_root=tmp_path
    )

    assert resolution["kind"] == "supplemental_rejected"
    assert resolution["error"]
    assert resolved["event_spec"]["source_status"] == artifact["event_spec"][
        "source_status"
    ]
    assert resolved["event_spec"]["provenance"]["revision"] == artifact[
        "event_spec"
    ]["provenance"]["revision"]
    assert any(
        item.get("code") == "supplemental_rejected"
        for item in resolved["event_spec"]["findings"]
    )
    assert validate_artifact(resolved) == resolved
