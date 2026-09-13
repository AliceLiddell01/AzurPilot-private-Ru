from __future__ import annotations

import numpy as np
import pytest

from module.dock_inventory.traversal import (
    DockInventoryTraversal,
    DockInventoryTraversalError,
)


class _Device:
    def __init__(self) -> None:
        self.image = np.zeros((8, 8, 3), dtype=np.uint8)


class _Runtime:
    def __init__(self) -> None:
        self.device = _Device()
        self.capture_calls = 0

    def capture_stable_dock_frame(self) -> np.ndarray:
        self.capture_calls += 1
        if self.capture_calls == 2:
            raise DockInventoryTraversalError("stable frame failed")
        return np.full((8, 8, 3), self.capture_calls, dtype=np.uint8)


class _Scroll:
    edge_threshold = 0.05

    def __init__(self) -> None:
        self.current = 0.0
        self.next_page_calls = 0

    def appear(self, _main) -> bool:
        return True

    def cal_position(self, _main) -> float:
        return self.current

    def set_top(self, _main, **_kwargs) -> None:
        self.current = 0.0

    def next_page(self, _main, **_kwargs) -> None:
        self.next_page_calls += 1
        self.current = 1.0


def test_dpad_stable_frame_failure_is_not_masked_by_scroll_fallback() -> None:
    runtime = _Runtime()
    scroll = _Scroll()

    def send(keycode: str) -> None:
        assert keycode == DockInventoryTraversal.DPAD_DOWN
        scroll.current = 0.2

    traversal = DockInventoryTraversal(
        runtime,
        scroll=scroll,
        keyevent_sender=send,
    )

    with pytest.raises(DockInventoryTraversalError, match="stable frame failed"):
        traversal.traverse(lambda _viewport: None)

    assert scroll.next_page_calls == 0
