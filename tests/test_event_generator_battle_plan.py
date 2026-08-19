from dataclasses import replace
from types import SimpleNamespace

import pytest

from module.event_datamine.generator import generate_map_module
from module.event_datamine.runtime_policy import SirenRecognitionPolicy
from module.event_datamine.runtime_semantics import (
    BattlePlanPolicy,
    SirenFilterStepPolicy,
)
from tests.event_runtime_policy_helpers import runtime_policy


def _map_spec():
    return SimpleNamespace(
        id=1,
        chapter_name="T",
        shape="A1",
        portals=(),
        map_data=(("--",),),
        map_data_loop=None,
        land_based=(),
        spawn_data=(
            {"battle": 0, "siren": 1},
            {"battle": 3, "boss": 1},
        ),
        spawn_data_loop=None,
        has_story=False,
        has_fleet_step=False,
        has_ambush=False,
        has_mystery=False,
        movable_enemy_turns=(),
        star_requirements=(1, 2, 3),
        boss_refresh=3,
        unknown_grid_types=(),
        unknown_effects=(),
    )


def test_generator_rejects_duplicate_battle_numbers_before_python_generation():
    policy = runtime_policy(
        siren=SirenRecognitionPolicy(("RuntimeTemplate",), False),
    )
    policy = replace(
        policy,
        battle_plan=BattlePlanPolicy(
            enemy_filter="1L > 1M > 1E > 1C",
            siren_filter_steps=(
                SirenFilterStepPolicy(battle=1, preserve=0),
                SirenFilterStepPolicy(battle=1, preserve=1),
            ),
        ),
    )

    with pytest.raises(
        ValueError,
        match="содержит повторный battle_1",
    ):
        generate_map_module(_map_spec(), runtime_policy=policy)
