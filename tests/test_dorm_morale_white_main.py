import numpy as np

from module.dorm.morale_controller import DormMoraleController
from module.ui.page import page_main_white


class _Device:
    def __init__(self):
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.clicks = []
        self.screenshots = 0

    def screenshot(self):
        self.screenshots += 1

    def click(self, button):
        self.clicks.append(button.name)


class _WhiteMainController(DormMoraleController):
    def ui_page_appear(self, page, offset=(20, 20)):
        return page is page_main_white


def test_close_train_accepts_white_main_as_already_safe():
    controller = object.__new__(_WhiteMainController)
    controller.device = _Device()

    frame = controller.close_train()

    assert frame is controller.device.image
    assert controller.device.screenshots == 1
    assert controller.device.clicks == []
