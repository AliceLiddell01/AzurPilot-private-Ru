from __future__ import annotations

from dataclasses import dataclass

import module.base.timer as timer_module
from module.map.map_fleet_preparation import FleetOperator


@dataclass
class _Clock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 0.2) -> None:
        self.value += seconds


class _Device:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.screenshot_count = 0
        self.clicks: list[object] = []
        self.on_click = None

    def screenshot(self) -> None:
        self.screenshot_count += 1
        self.clock.advance()

    def click(self, button) -> None:
        self.clicks.append(button)
        if self.on_click is not None:
            self.on_click(button)


class _Main:
    def __init__(self, clock: _Clock) -> None:
        self.device = _Device(clock)
        self.popup_sequence: list[bool] = []

    def handle_popup_confirm(self, _name: str) -> bool:
        if self.popup_sequence:
            return self.popup_sequence.pop(0)
        return False


def _operator(main: _Main) -> FleetOperator:
    operator = FleetOperator.__new__(FleetOperator)
    operator.main = main
    operator._clear = "FLEET_2_CLEAR"
    return operator


def test_clear_is_idempotent_when_slot_already_empty(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(timer_module, "time", clock)

    main = _Main(clock)
    operator = _operator(main)
    monkeypatch.setattr(operator, "allow", lambda: False)
    monkeypatch.setattr(operator, "in_use", lambda: False)

    operator.clear(skip_first_screenshot=False)

    assert main.device.clicks == []
    assert main.device.screenshot_count >= 4


def test_clear_clicks_populated_slot_then_confirms_empty(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(timer_module, "time", clock)

    main = _Main(clock)
    operator = _operator(main)
    state = {"in_use": True}

    monkeypatch.setattr(operator, "allow", lambda: state["in_use"])
    monkeypatch.setattr(operator, "in_use", lambda: state["in_use"])

    def on_click(_button) -> None:
        state["in_use"] = False

    main.device.on_click = on_click

    operator.clear(skip_first_screenshot=False)

    assert main.device.clicks == ["FLEET_2_CLEAR"]
    assert state["in_use"] is False


def test_clear_does_not_accept_transient_empty_frame(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(timer_module, "time", clock)

    main = _Main(clock)
    operator = _operator(main)
    states = iter([False, False, True, False, False, False, False, False])

    def in_use() -> bool:
        return next(states, False)

    monkeypatch.setattr(operator, "allow", lambda: False)
    monkeypatch.setattr(operator, "in_use", in_use)

    operator.clear(skip_first_screenshot=False)

    assert main.device.clicks == []
    assert main.device.screenshot_count >= 7


def test_clear_popup_resets_empty_confirmation(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(timer_module, "time", clock)

    main = _Main(clock)
    main.popup_sequence = [True]
    operator = _operator(main)
    monkeypatch.setattr(operator, "allow", lambda: False)
    monkeypatch.setattr(operator, "in_use", lambda: False)

    operator.clear(skip_first_screenshot=False)

    assert main.device.clicks == []
    assert main.device.screenshot_count >= 5
