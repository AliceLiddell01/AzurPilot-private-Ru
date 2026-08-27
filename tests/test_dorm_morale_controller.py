from datetime import UTC, datetime, timedelta

import numpy as np

from module.dorm.morale_controller import DormManageStateDetector, DormMoraleController
from module.dorm.morale_model import (
    DormFloor,
    DormFloorSnapshot,
    DormMoraleScanStatus,
)


def _frame(floor):
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    area = (145, 90, 330, 120) if floor is DormFloor.FLOOR_1 else (360, 90, 545, 120)
    x1, y1, x2, y2 = area
    frame[y1:y2, x1:x2] = 255
    return frame


class _Device:
    def __init__(self, frames):
        self.frames = list(frames)
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.clicks = []
        self.clicked_buttons = []

    def screenshot(self):
        self.image = self.frames.pop(0)

    def click(self, button):
        self.clicks.append(button.name)
        self.clicked_buttons.append(button)


class _Scanner:
    def __init__(self, fail_floor=None):
        self.calls = []
        self.fail_floor = fail_floor

    def scan(self, _frame, *, floor):
        self.calls.append(floor)
        if floor is self.fail_floor:
            raise RuntimeError("синтетическая ошибка OCR")
        return DormFloorSnapshot(floor, (), "a" * 64)


class _Controller(DormMoraleController):
    def ui_ensure(self, page):
        self.ensured = page

    def ui_page_appear(self, page, offset=(20, 20)):
        return self.dorm_page_visible

    def ui_additional(self, get_ship=False):
        return False


def _controller(frames, *, fail_floor=None, dorm_page_visible=False):
    controller = object.__new__(_Controller)
    controller.device = _Device(frames)
    controller._scanner = _Scanner(fail_floor)
    controller.dorm_page_visible = dorm_page_visible
    values = iter(
        datetime(2026, 8, 27, 10, tzinfo=UTC) + timedelta(seconds=index)
        for index in range(20)
    )
    controller._clock = lambda: next(values)
    from uuid import uuid4

    controller._id_factory = uuid4
    return controller


def test_state_detector_distinguishes_selected_floor_and_unknown():
    detector = DormManageStateDetector()
    assert detector.selected_floor(_frame(DormFloor.FLOOR_1)) is DormFloor.FLOOR_1
    assert detector.selected_floor(_frame(DormFloor.FLOOR_2)) is DormFloor.FLOOR_2
    large = np.repeat(np.repeat(_frame(DormFloor.FLOOR_1), 3, axis=0), 3, axis=1)
    assert detector.selected_floor(large) is DormFloor.FLOOR_1
    assert detector.selected_floor(np.zeros((720, 1280, 3), dtype=np.uint8)) is None


def test_state_detector_uses_rgb_channel_contract():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    x1, y1, x2, y2 = (145, 90, 330, 120)
    frame[y1:y2, x1:x2] = (255, 255, 0)

    assert DormManageStateDetector().selected_floor(frame) is DormFloor.FLOOR_1


def test_controller_opens_train_once_and_waits_for_confirmed_floor():
    unknown = np.zeros((720, 1280, 3), dtype=np.uint8)
    controller = _controller(
        (unknown, unknown.copy(), _frame(DormFloor.FLOOR_1)),
        dorm_page_visible=True,
    )

    frame = controller._open_manage()

    assert controller.dorm_manage_state.selected_floor(frame) is DormFloor.FLOOR_1
    assert controller.device.clicks == ["DORM_MORALE_TRAIN"]
    assert len(controller.device.clicked_buttons) == 1
    assert (
        controller.device.clicked_buttons[0].button
        == controller.dorm_manage_layout.train_entry_button
    )


def test_controller_switches_one_action_per_screenshot_and_scans_both_floors():
    controller = _controller(
        (
            _frame(DormFloor.FLOOR_2),
            _frame(DormFloor.FLOOR_1),
            _frame(DormFloor.FLOOR_1),
            _frame(DormFloor.FLOOR_2),
            _frame(DormFloor.FLOOR_2),
        )
    )
    result = controller.scan_both_floors(source="test:controller")
    assert result.status is DormMoraleScanStatus.SUCCEEDED
    assert controller._scanner.calls == [DormFloor.FLOOR_1, DormFloor.FLOOR_2]
    assert controller.device.clicks == ["DORM_MORALE_1F", "DORM_MORALE_2F"]


def test_controller_second_floor_failure_is_partial_not_outside_evidence():
    controller = _controller(
        (
            _frame(DormFloor.FLOOR_1),
            _frame(DormFloor.FLOOR_1),
            _frame(DormFloor.FLOOR_2),
            _frame(DormFloor.FLOOR_2),
        ),
        fail_floor=DormFloor.FLOOR_2,
    )
    result = controller.scan_both_floors(source="test:controller")
    assert result.status is DormMoraleScanStatus.PARTIAL
    assert not result.complete
    assert controller.device.clicks == ["DORM_MORALE_2F"]


def test_controller_unexpected_state_returns_failed_without_blind_clicks():
    controller = _controller((np.zeros((720, 1280, 3), dtype=np.uint8),))
    result = controller.scan_both_floors(source="test:controller")
    assert result.status is DormMoraleScanStatus.FAILED
    assert controller.device.clicks == []