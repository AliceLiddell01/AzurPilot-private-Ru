from types import SimpleNamespace

import pytest

import module.map.camera as camera_module
from module.exception import MapDetectionError
from module.map.camera import Camera


def _edge_camera(*, corner="", shape=(8, 8)):
    view = SimpleNamespace(
        left_edge=False,
        right_edge=False,
        lower_edge=True,
        upper_edge=True,
    )
    swipes = []
    camera = SimpleNamespace(
        config=SimpleNamespace(MAP_ENSURE_EDGE_INSIGHT_CORNER=corner),
        map=SimpleNamespace(shape=shape),
        view=view,
    )

    def map_swipe(vector):
        normalized = tuple(int(value) for value in vector)
        swipes.append(normalized)
        return True

    camera.map_swipe = map_swipe
    return camera, swipes


def test_random_edge_direction_reverses_after_axis_budget(monkeypatch):
    monkeypatch.setattr(camera_module, "random_direction", lambda _: (-1, 1))
    camera, swipes = _edge_camera()

    def map_swipe(vector):
        normalized = tuple(int(value) for value in vector)
        swipes.append(normalized)
        if normalized == (3, 0):
            camera.view.right_edge = True
        return True

    camera.map_swipe = map_swipe

    Camera.ensure_edge_insight(camera)

    assert [vector for vector in swipes if vector != (0, 0)] == [
        (-3, 0),
        (-3, 0),
        (-3, 0),
        (3, 0),
    ]


def test_fixed_edge_direction_fails_without_reversal(monkeypatch):
    monkeypatch.setattr(camera_module, "random_direction", lambda _: (-1, 1))
    camera, swipes = _edge_camera(corner="left")

    with pytest.raises(MapDetectionError, match="оси X"):
        Camera.ensure_edge_insight(camera)

    assert swipes == [(-3, 0), (-3, 0), (-3, 0)]


def test_random_edge_direction_fails_after_both_sides(monkeypatch):
    monkeypatch.setattr(camera_module, "random_direction", lambda _: (-1, 1))
    camera, swipes = _edge_camera()

    with pytest.raises(MapDetectionError, match="оси X"):
        Camera.ensure_edge_insight(camera)

    assert swipes == [
        (-3, 0),
        (-3, 0),
        (-3, 0),
        (3, 0),
        (3, 0),
        (3, 0),
    ]
