from types import SimpleNamespace

from module.map.assets import MAP_PREPARATION
from module.map.map_operation import MapOperation


class ResetTracker:
    def __init__(self):
        self.count = 0

    def reset(self):
        self.count += 1


def make_operation(*, one_time: bool) -> MapOperation:
    operation = MapOperation.__new__(MapOperation)
    operation.config = SimpleNamespace(
        MAP_IS_ONE_TIME_STAGE=one_time,
        MAP_HAS_CLEAR_PERCENTAGE=True,
    )
    operation.map_clear_percentage_prev = 0.5
    operation.map_clear_percentage_timer = ResetTracker()
    return operation


def test_one_time_stage_uses_color_fallback_after_template_miss(monkeypatch):
    operation = make_operation(one_time=True)
    offsets = []

    def appear(button, offset=0, interval=0):
        assert button is MAP_PREPARATION
        assert interval == 0
        offsets.append(offset)
        return offset == 0

    monkeypatch.setattr(operation, "appear", appear)

    assert operation.handle_map_preparation() is True
    assert offsets == [(20, 20), 0]
    assert operation.map_clear_percentage_timer.count == 0


def test_normal_stage_keeps_strict_template_detection(monkeypatch):
    operation = make_operation(one_time=False)
    offsets = []

    def appear(button, offset=0, interval=0):
        assert button is MAP_PREPARATION
        assert interval == 0
        offsets.append(offset)
        return False

    monkeypatch.setattr(operation, "appear", appear)

    assert operation.handle_map_preparation() is False
    assert offsets == [(20, 20)]
    assert operation.map_clear_percentage_prev == -1
    assert operation.map_clear_percentage_timer.count == 1
