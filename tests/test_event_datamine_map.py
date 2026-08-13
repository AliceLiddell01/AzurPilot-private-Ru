from module.event_datamine.generator import generate_map_module
from module.event_datamine.map_compiler import MapCompiler


def chapter(grid_type=0):
    return {
        "id": 1001,
        "chapter_name": "A1",
        "name": "Fixture",
        "grids": {
            0: {0: 0, 1: 0, 2: True, 3: 1},
            1: {0: 0, 1: 1, 2: True, 3: grid_type},
            2: {0: 1, 1: 0, 2: True, 3: 8},
            3: {0: 1, 1: 1, 2: True, 3: 6},
        },
        "boss_refresh": 1,
        "enemy_refresh": {0: 1, 1: 0},
        "elite_refresh": {},
        "ai_refresh": {},
        "box_refresh": {},
        "ai_expedition_list": {},
        "land_based": {},
        "story_refresh_boss": {},
        "is_limit_move": 0,
        "is_ambush": 0,
        "is_air_attack": 0,
        "star_require_1": 1,
        "star_require_2": 2,
        "star_require_3": 3,
    }


def compiler(row, event_list=None, templates=None):
    return MapCompiler({1001: row}, {}, event_list or {}, templates or {}, {})


def test_unknown_grid_is_explicit_and_blocks_generation():
    spec, findings = compiler(chapter(999)).compile(1001)

    assert spec is not None
    assert spec.unknown_grid_types == (999,)
    assert any(
        item.code == "unknown_grid" and item.severity == "error" for item in findings
    )
    try:
        generate_map_module(spec)
    except ValueError as exc:
        assert "не eligible" in str(exc)
    else:
        raise AssertionError("unknown grid был сгенерирован как production map")


def test_portal_effect_is_decoded_instead_of_silently_ignored():
    event_list = {1001: {"event_list": {0: 381}, "event_list_loop": {}}}
    templates = {
        381: {
            "address": {0: 4, 1: 4},
            "effect": {0: {0: "jump", 1: 0, 2: 4}, 1: {0: "jumpsub", 1: 0, 2: 4}},
        }
    }

    row = chapter()
    row["grids"] = {
        y * 5 + x: {
            0: y,
            1: x,
            2: True,
            3: 1 if (x, y) == (0, 0) else 8 if (x, y) == (4, 4) else 0,
        }
        for y in range(5)
        for x in range(5)
    }
    spec, findings = compiler(row, event_list, templates).compile(1001)

    assert not findings
    assert spec is not None
    assert [(item.source, item.target) for item in spec.portals] == [("E5", "E1")]
    assert "MAP.portal_data = [('E5', 'E1')]" in generate_map_module(spec)
    assert "STAR_REQUIRE_1 = 1" in generate_map_module(spec)


def test_unknown_event_effect_is_blocking_diagnostic():
    event_list = {1001: {"event_list": {0: 99}}}
    templates = {99: {"address": {0: 0, 1: 0}, "effect": {0: {0: "new_mechanic"}}}}

    spec, findings = compiler(chapter(), event_list, templates).compile(1001)

    assert spec is not None
    assert spec.unknown_effects == ("new_mechanic",)
    assert any(item.code == "unknown_effect" for item in findings)


def test_topology_hole_is_blocking_instead_of_becoming_question_marks():
    row = chapter()
    del row["grids"][3]

    spec, findings = compiler(row).compile(1001)

    assert spec is not None
    assert any(
        item.code == "map_topology_hole" and item.severity == "error"
        for item in findings
    )


def test_supported_land_based_data_is_preserved_in_generated_map():
    row = chapter()
    row["land_based"] = {0: {0: 1, 1: 0, 2: 1}}

    spec, findings = compiler(row).compile(1001)

    assert not findings
    assert spec is not None
    assert spec.land_based == (("A2", "up"),)
    generated = generate_map_module(spec)
    assert "MAP.land_based_data = [('A2', 'up')]" in generated
    assert "MAP_HAS_LAND_BASED = True" in generated
