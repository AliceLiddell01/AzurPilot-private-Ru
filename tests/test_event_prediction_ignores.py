from dataclasses import fields
import importlib

import pytest

from module.base.utils import node2location
from module.event_datamine.generator import generate_map_module
from module.event_datamine.model import MapSpec
from module.event_datamine.runtime_policy import (
    EventRuntimePolicyError,
    load_generated_runtime_policy,
    runtime_map_policies,
)
from tests.event_fixture_helpers import (
    current_generated_package_parts,
    production_artifact,
)

D2_MAP_ID = 2050025
SMOKE_ARCHIVE_SHA256 = "4c125df163063f29d6c36d153bf7f94ce32dc2275ee4dcf0b00908aed9b0ac17"


def _d2_policy():
    policy = load_generated_runtime_policy(current_generated_package_parts())
    assert policy is not None
    return runtime_map_policies(policy)[D2_MAP_ID]


def _d2_spec() -> MapSpec:
    raw = next(
        item
        for item in production_artifact()["event_spec"]["maps"]
        if item["id"] == D2_MAP_ID
    )
    values = {field.name: raw[field.name] for field in fields(MapSpec)}
    return MapSpec(**values)


def test_d2_prediction_ignores_are_bound_to_smoke_evidence():
    runtime = _d2_policy()

    assert [item.node for item in runtime.prediction_ignores] == ["D5", "F7"]
    assert [item.match_dict() for item in runtime.prediction_ignores] == [
        {"enemy_genre": "Enemy", "enemy_scale": 1},
        {"enemy_genre": "Enemy", "enemy_scale": 1},
    ]
    assert {
        item.evidence_sha256 for item in runtime.prediction_ignores
    } == {SMOKE_ARCHIVE_SHA256}


def test_generator_emits_prediction_ignores_from_runtime_policy():
    content = generate_map_module(_d2_spec(), runtime_policy=_d2_policy())

    assert "MAP.ignore_prediction('D5', **{'enemy_genre': 'Enemy', 'enemy_scale': 1})" in content
    assert "MAP.ignore_prediction('F7', **{'enemy_genre': 'Enemy', 'enemy_scale': 1})" in content


def test_production_d2_map_contains_generated_prediction_ignores():
    module = importlib.import_module("campaign.generated_event.en_51101.d2")
    runtime = _d2_policy()
    expected = [
        (node2location(item.node), item.match_dict())
        for item in runtime.prediction_ignores
    ]

    assert module.MAP._ignore_prediction == expected


def test_prediction_ignore_rejects_unsupported_match_key():
    from module.event_datamine.runtime_policy import runtime_map_policies

    data = {
        "runtime_maps": [
            {
                "map_id": 1,
                "chapter_name": "TEST",
                "source_path": "campaign/test.py",
                "prediction_ignores": [
                    {
                        "node": "A1",
                        "match": {"unknown_detector_flag": True},
                        "evidence_sha256": "1" * 64,
                    }
                ],
            }
        ]
    }

    with pytest.raises(EventRuntimePolicyError, match="неизвестные поля"):
        runtime_map_policies(data)
