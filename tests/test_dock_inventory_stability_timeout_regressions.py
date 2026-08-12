from __future__ import annotations

import numpy as np
import pytest

from module.dock_inventory.navigation import (
    DockInventoryNavigationError,
    DockInventoryNavigator,
)


class _SequenceDevice:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.shared = np.zeros((8, 8, 3), dtype=np.uint8)
        self.image = self.shared
        self.screenshot_calls = 0

    def screenshot(self) -> None:
        self.screenshot_calls += 1
        self.shared.fill(self.values.pop(0))
        self.image = self.shared


class _HashGenerator:
    def scan(self, image, cached=False, output=False):
        return [int(image[0, 0, 0])]


class _ManualTimer:
    now = 0.0

    def __init__(self, limit: float, count: int = 0) -> None:
        self.limit = float(limit)
        self.count = count
        self._access = 0

    def start(self):
        return self

    def current_time(self) -> float:
        return type(self).now

    def reached(self) -> bool:
        self._access += 1
        return self._access > self.count and self.current_time() > self.limit


class _TimedSequenceDevice(_SequenceDevice):
    def __init__(self, values: list[int], times: list[float]) -> None:
        super().__init__(values)
        self.times = list(times)

    def screenshot(self) -> None:
        super().screenshot()
        _ManualTimer.now = self.times.pop(0)


def test_fast_capture_can_stabilize_after_more_than_twelve_frames(monkeypatch) -> None:
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _SequenceDevice(list(range(1, 15)) + [14])

    frame = navigator.capture_stable_dock_frame()

    assert navigator.device.screenshot_calls == 15
    assert int(frame[0, 0, 0]) == 14
    assert navigator._dock_stability_failure_frame is None
    assert navigator._dock_stability_failure_hashes == ()


def test_matching_pair_before_wall_clock_deadline_is_accepted(monkeypatch) -> None:
    import module.dock_inventory.navigation as navigation
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    monkeypatch.setattr(navigation, "Timer", _ManualTimer)
    _ManualTimer.now = 0.0
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _TimedSequenceDevice([7, 7], [0.5, 1.0])

    frame = navigator.capture_stable_dock_frame()

    assert navigator.device.screenshot_calls == 2
    assert int(frame[0, 0, 0]) == 7
    assert navigator._dock_stability_failure_frame is None
    assert navigator._dock_stability_failure_hashes == ()


def test_matching_pair_after_wall_clock_deadline_is_rejected(monkeypatch) -> None:
    import module.dock_inventory.navigation as navigation
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    monkeypatch.setattr(navigation, "Timer", _ManualTimer)
    _ManualTimer.now = 0.0
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _TimedSequenceDevice([7, 7], [0.5, 3.1])

    with pytest.raises(
        DockInventoryNavigationError,
        match=r"captures=2, elapsed=3\.100s, timeout=3\.0s",
    ):
        navigator.capture_stable_dock_frame()

    assert navigator.device.screenshot_calls == 2
    failure_frame = navigator._dock_stability_failure_frame
    assert isinstance(failure_frame, np.ndarray)
    assert int(failure_frame[0, 0, 0]) == 7
    assert navigator._dock_stability_failure_hashes == ("7",)


def test_real_timeout_preserves_detached_failure_evidence(monkeypatch) -> None:
    import module.dock_inventory.navigation as navigation
    import module.retire.scanner as retire_scanner

    monkeypatch.setattr(retire_scanner, "HashGenerator", _HashGenerator)
    monkeypatch.setattr(navigation, "Timer", _ManualTimer)
    _ManualTimer.now = 0.0
    navigator = object.__new__(DockInventoryNavigator)
    navigator.device = _TimedSequenceDevice([1, 2], [0.5, 3.1])

    with pytest.raises(
        DockInventoryNavigationError,
        match=r"captures=2, elapsed=3\.100s, timeout=3\.0s",
    ):
        navigator.capture_stable_dock_frame()

    assert navigator.device.screenshot_calls == 2
    failure_frame = navigator._dock_stability_failure_frame
    assert isinstance(failure_frame, np.ndarray)
    assert int(failure_frame[0, 0, 0]) == 2
    assert navigator._dock_stability_failure_hashes == ("2",)

    navigator.device.shared.fill(99)
    assert int(failure_frame[0, 0, 0]) == 2
