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
        self.swipes: list[tuple[tuple[int, int], tuple[int, int], object, str]] = []

    def swipe(self, p1, p2, *, duration, name):
        self.swipes.append((p1, p2, duration, name))


class _Runtime:
    def __init__(self) -> None:
        self.device = _Device()
        self.capture_calls = 0

    def capture_stable_dock_frame(self) -> np.ndarray:
        self.capture_calls += 1
        return np.full((8, 8, 3), self.capture_calls, dtype=np.uint8)


class _Scroll:
    edge_threshold = 0.05

    def __init__(self, *, moves: list[float] | None = None) -> None:
        self.current = 0.0
        self.moves = [] if moves is None else list(moves)
        self.set_top_calls = 0
        self.next_page_calls = 0

    def appear(self, _main) -> bool:
        return True

    def cal_position(self, _main) -> float:
        return self.current

    def set_top(self, _main, **_kwargs) -> None:
        self.set_top_calls += 1
        self.current = 0.0

    def next_page(self, _main, **_kwargs) -> None:
        self.next_page_calls += 1
        if self.moves:
            self.current = self.moves.pop(0)


def test_initial_nudge_uses_fixed_small_swipe_and_keeps_top_evidence() -> None:
    runtime = _Runtime()
    scroll = _Scroll(moves=[1.0])
    traversal = DockInventoryTraversal(
        runtime,
        scroll=scroll,
        prefer_keyevents=False,
    )

    result = traversal.traverse(lambda _viewport: None)

    assert runtime.device.swipes == [
        (
            DockInventoryTraversal.INITIAL_NUDGE_START,
            DockInventoryTraversal.INITIAL_NUDGE_END,
            DockInventoryTraversal.INITIAL_NUDGE_DURATION,
            "НОРМАЛИЗАЦИЯ_ПЕРВОГО_ОКНА_ДОКА",
        )
    ]
    assert result.initial_nudge_applied is True
    assert result.positions == (0.0, 1.0)
    assert scroll.set_top_calls == 0
    assert scroll.next_page_calls == 1


def test_initial_nudge_that_leaves_top_is_restored_before_first_scan() -> None:
    runtime = _Runtime()
    scroll = _Scroll(moves=[1.0])

    def nudge() -> None:
        scroll.current = 0.2

    traversal = DockInventoryTraversal(
        runtime,
        scroll=scroll,
        prefer_keyevents=False,
        initial_nudge_sender=nudge,
    )

    result = traversal.traverse(lambda _viewport: None)

    assert result.initial_nudge_applied is False
    assert result.positions == (0.0, 1.0)
    assert scroll.set_top_calls == 1
    assert result.scroll_fallback_calls == 2


def test_initial_nudge_input_failure_is_not_hidden_by_scroll_fallback() -> None:
    scroll = _Scroll(moves=[1.0])

    def nudge() -> None:
        raise RuntimeError("input backend failed")

    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=scroll,
        prefer_keyevents=False,
        initial_nudge_sender=nudge,
    )

    with pytest.raises(DockInventoryTraversalError, match="input backend failed"):
        traversal.traverse(lambda _viewport: None)

    assert scroll.next_page_calls == 0
