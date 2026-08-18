from __future__ import annotations

import copy
from pathlib import Path

import pytest

from module.event_datamine.artifact import validate_artifact
from module.event_datamine.supplemental import (
    load_supplemental,
    resolve_supplemental_artifact,
)
from tests.event_fixture_helpers import production_artifact, write_split_supplemental


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
    artifact = production_artifact()
    event_id = artifact["event_spec"]["id"]
    source = load_supplemental(event_id)
    assert source is not None

    supplemental = copy.deepcopy(source)
    _corrupt_supplemental(supplemental, case)
    case_root = tmp_path / case
    write_split_supplemental(case_root, supplemental)

    resolved, resolution = resolve_supplemental_artifact(
        artifact,
        supplemental_root=case_root,
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
