from dataclasses import dataclass

import pytest

from module.exception import MapDetectionError
from module.map.camera import Camera
from module.map.map_grids import SelectedGrids


@dataclass(frozen=True)
class _Grid:
    location: tuple[int, int]

    def __str__(self):
        x, y = self.location
        return f"grid({x},{y})"


class _MapStub:
    def __init__(self, camera_points, update_results):
        self.camera_data = SelectedGrids(camera_points)
        self._update_results = iter(update_results)
        self.update_cameras = []
        self.missing_predict_called = False

    def reset_fleet(self):
        pass

    def missing_is_none(self, *args):
        return False

    def update(self, *, grids, camera, mode):
        self.update_cameras.append(camera)
        return next(self._update_results)

    def missing_predict(self, *args):
        self.missing_predict_called = True

    def show(self):
        pass


def _camera(camera_points, update_results, recovery_records):
    instance = Camera.__new__(Camera)
    instance.camera = camera_points[0].location
    instance.map = _MapStub(camera_points, update_results)
    instance.view = object()
    instance.focus_to = lambda grid: setattr(instance, "camera", grid.location)
    instance.focus_to_grid_center = lambda _tolerance: False
    records = iter(recovery_records)
    instance.ensure_edge_insight = lambda **_kwargs: next(records)
    return instance


def test_full_scan_defers_view_when_edge_recovery_cannot_move():
    first = _Grid((0, 0))
    second = _Grid((5, 0))
    camera = _camera(
        [first, second],
        update_results=[False, True, True],
        recovery_records=[[(0, 0)]],
    )

    camera.full_scan()

    assert camera.map.update_cameras == [(0, 0), (5, 0), (0, 0)]
    assert camera.map.missing_predict_called is True


def test_full_scan_raises_after_repeated_stationary_failure():
    point = _Grid((0, 0))
    camera = _camera(
        [point],
        update_results=[False, False],
        recovery_records=[[(0, 0)], [(0, 0)]],
    )

    with pytest.raises(
        MapDetectionError,
        match="Повторное сканирование точки",
    ):
        camera.full_scan()

    assert camera.map.update_cameras == [(0, 0), (0, 0)]


def test_full_scan_retries_same_view_after_real_camera_movement():
    point = _Grid((0, 0))
    camera = _camera(
        [point],
        update_results=[False, True],
        recovery_records=[[(1, 0), (0, 0)]],
    )

    camera.full_scan()

    assert camera.map.update_cameras == [(0, 0), (0, 0)]
    assert camera.map.missing_predict_called is True
