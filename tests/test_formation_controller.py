import numpy as np
import pytest

from module.formation.navigation import (
    FormationFleetController,
    FormationNavigationLayout,
)
from module.formation.scanner import FormationFleetOcrError


class _Device:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.clicks = []
        self.screenshot_calls = 0

    def click(self, button) -> None:
        self.clicks.append(button.name)

    def screenshot(self) -> None:
        self.screenshot_calls += 1


class _State:
    def __init__(self, owner, *, menu_by_iteration=None, info_by_iteration=None) -> None:
        self.owner = owner
        self.menu_by_iteration = menu_by_iteration or {}
        self.info_by_iteration = info_by_iteration or {}

    def fleet_menu_opened(self, frame) -> bool:
        return self.menu_by_iteration.get(self.owner._iteration, False)

    def info_opened(self, frame) -> bool:
        return self.info_by_iteration.get(self.owner._iteration, False)


class _FleetIndexOcr:
    def __init__(self, owner, values) -> None:
        self.owner = owner
        self.values = values

    def read(self, frame):
        return self.values.get(self.owner._iteration)


class _Controller(FormationFleetController):
    def __init__(self, iterations: int = 5) -> None:
        self.device = _Device()
        self.iterations = iterations
        self._iteration = -1
        self.__dict__["formation_navigation_layout"] = FormationNavigationLayout()

    def loop(self, *args, **kwargs):
        for index in range(self.iterations):
            self._iteration = index
            yield self.device.image

    def ui_page_appear(self, page, offset=(30, 30), interval=0):
        return True


def test_surface_fleet_selection_is_state_driven_and_verified() -> None:
    controller = _Controller(iterations=4)
    controller.__dict__["formation_state"] = _State(
        controller,
        menu_by_iteration={1: True},
    )
    controller.__dict__["formation_fleet_index_ocr"] = _FleetIndexOcr(
        controller,
        {0: 1, 2: 6},
    )

    controller.ensure_surface_fleet(6)

    assert controller.device.clicks == [
        "FORMATION_OPEN_FLEET_MENU",
        "FORMATION_SELECT_FLEET_6",
    ]
    assert controller._iteration == 2


def test_open_info_clicks_once_then_waits_for_selected_state() -> None:
    controller = _Controller(iterations=3)
    controller.__dict__["formation_state"] = _State(
        controller,
        info_by_iteration={1: True},
    )

    controller._open_info()

    assert controller.device.clicks == ["FORMATION_OPEN_INFO"]


def test_close_info_clicks_once_then_requires_formation_page() -> None:
    controller = _Controller(iterations=3)
    controller.__dict__["formation_state"] = _State(
        controller,
        info_by_iteration={0: True},
    )

    controller._close_info()

    assert controller.device.clicks == ["FORMATION_CLOSE_INFO"]


class _AlwaysInfo:
    def info_opened(self, frame):
        return True


class _FailingScanner:
    def scan(self, frame, *, fleet_index):
        raise FormationFleetOcrError("fixture")


def test_scan_restores_info_state_when_scanner_fails(monkeypatch) -> None:
    controller = _Controller(iterations=1)
    controller.__dict__["formation_state"] = _AlwaysInfo()
    controller.__dict__["formation_fleet_scanner"] = _FailingScanner()
    events = []

    monkeypatch.setattr(
        controller,
        "ensure_formation_page",
        lambda: events.append("ensure_page"),
    )
    monkeypatch.setattr(
        controller,
        "ensure_surface_fleet",
        lambda fleet_index: events.append(("ensure_fleet", fleet_index)),
    )
    monkeypatch.setattr(controller, "_open_info", lambda: events.append("open_info"))
    monkeypatch.setattr(controller, "_close_info", lambda: events.append("close_info"))

    with pytest.raises(FormationFleetOcrError, match="fixture"):
        controller.scan_surface_fleet(4)

    assert events == [
        "ensure_page",
        ("ensure_fleet", 4),
        "open_info",
        "close_info",
    ]
