from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from module.dock_inventory.navigation import DockInventoryNavigator


class _Device:
    def __init__(self) -> None:
        self.image = np.zeros((4, 4, 3), dtype=np.uint8)
        self.captures = 0

    def screenshot(self) -> None:
        self.captures += 1
        value = min(self.captures, 13)
        self.image = np.full((4, 4, 3), value, dtype=np.uint8)


class _HashGenerator:
    def scan(self, frame, *, cached: bool, output: bool):
        assert cached is False
        assert output is False
        return int(frame[0, 0, 0])


def test_stable_frame_budget_can_outlive_old_twelve_capture_cap(monkeypatch) -> None:
    import module.retire.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "HashGenerator", _HashGenerator)
    device = _Device()
    navigator = SimpleNamespace(
        device=device,
        DOCK_STABILITY_TIMEOUT=1000.0,
        DOCK_STABILITY_MIN_CAPTURES=2,
        DOCK_STABILITY_MAX_CAPTURES=DockInventoryNavigator.DOCK_STABILITY_MAX_CAPTURES,
    )

    frame = DockInventoryNavigator.capture_stable_dock_frame(navigator)

    assert device.captures == 14
    assert int(frame[0, 0, 0]) == 13
    assert np.array_equal(device.image, frame)
