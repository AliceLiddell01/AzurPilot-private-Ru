from dataclasses import replace
from types import SimpleNamespace

import pytest

from module.event_datamine.generator import generate_map_module
from module.event_datamine.runtime_policy import EventRuntimePolicyError
from module.event_datamine.runtime_semantics import (
    BattlePlanPolicy,
    SirenFilterStepPolicy,
    parse_battle_plan,
    parse_camera_calibration,
    parse_detector_calibration,
)
from tests.event_runtime_policy_helpers import runtime_policy


def _map(*, boss_refresh: int = 0):
    return SimpleNamespace(
        id=1,
        chapter_name="T",
        shape="A1",
        portals=(),
        map_data=(("--",),),
        map_data_loop=None,
        land_based=(),
        spawn_data=({"battle": boss_refresh, "boss": 1},),
        spawn_data_loop=None,
        has_story=False,
        has_fleet_step=False,
        has_ambush=False,
        has_mystery=False,
        movable_enemy_turns=(),
        star_requirements=(1, 2, 3),
        boss_refresh=boss_refresh,
        unknown_grid_types=(),
        unknown_effects=(),
    )


def test_camera_policy_rejects_arbitrary_fields():
    with pytest.raises(EventRuntimePolicyError, match="неизвестные поля"):
        parse_camera_calibration(
            {
                "camera_data": ["A1"],
                "spawn_points": ["A1"],
                "python": "self.do_anything()",
            },
            map_id=1,
            error_type=EventRuntimePolicyError,
        )


def test_detector_policy_rejects_arbitrary_config_names():
    with pytest.raises(EventRuntimePolicyError, match="неизвестные поля"):
        parse_detector_calibration(
            {
                "internal_lines": {
                    "height": [80, 238],
                    "width": [0.9, 10],
                    "prominence": 10,
                    "distance": 35,
                },
                "edge_lines": {
                    "height": [238, 255],
                    "prominence": 10,
                    "distance": 50,
                    "wlen": 1000,
                },
                "swipe": {
                    "adb": [1.0, 1.1],
                    "minitouch": [1.0, 1.1],
                    "maatouch": [1.0, 1.1],
                },
                "ARBITRARY_CONFIG": True,
            },
            map_id=1,
            error_type=EventRuntimePolicyError,
        )


def test_battle_plan_rejects_python_like_filter_payload():
    with pytest.raises(EventRuntimePolicyError, match="enemy_filter"):
        parse_battle_plan(
            {
                "enemy_filter": "__import__('os').system('x')",
                "siren_filter_steps": [{"battle": 0, "preserve": 0}],
            },
            map_id=1,
            error_type=EventRuntimePolicyError,
        )


def test_generator_rejects_runtime_camera_outside_map_shape():
    policy = runtime_policy(camera_data=("B1",), spawn_points=("B1",))

    with pytest.raises(ValueError, match="вне shape"):
        generate_map_module(_map(), runtime_policy=policy)


def test_generator_rejects_battle_plan_collision_with_boss():
    policy = runtime_policy()
    policy = replace(
        policy,
        battle_plan=BattlePlanPolicy(
            enemy_filter="1L > 1M",
            siren_filter_steps=(SirenFilterStepPolicy(battle=0, preserve=0),),
        ),
    )

    with pytest.raises(ValueError, match="конфликтует с boss battle_0"):
        generate_map_module(_map(boss_refresh=0), runtime_policy=policy)
