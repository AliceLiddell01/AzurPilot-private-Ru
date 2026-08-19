from types import SimpleNamespace

from dev_tools.event_datamine_build import _map_status


def _spec(*, map_data=(('++',),), map_data_loop=(), unknown_grid_types=(), unknown_effects=()):
    return SimpleNamespace(
        map_data=map_data,
        map_data_loop=map_data_loop,
        unknown_grid_types=unknown_grid_types,
        unknown_effects=unknown_effects,
    )


def test_map_status_marks_topology_hole_partial():
    assert _map_status(_spec(map_data=(("++", "??"),))) == "partial"


def test_map_status_keeps_unknown_source_semantics_unsupported():
    assert _map_status(_spec(unknown_grid_types=(99,))) == "unsupported"


def test_map_status_marks_complete_known_topology_verified():
    assert _map_status(_spec()) == "verified"
