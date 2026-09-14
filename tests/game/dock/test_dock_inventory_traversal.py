from __future__ import annotations

import math

import numpy as np
import pytest

from module.dock_inventory.traversal import (
    DockInventoryTraversal,
    DockInventoryTraversalError,
)


class _Device:
    def __init__(self, image: np.ndarray) -> None:
        self.image = image


class _Runtime:
    def __init__(self) -> None:
        self.shared = np.zeros((8, 8, 3), dtype=np.uint8)
        self.device = _Device(self.shared)
        self.capture_calls = 0

    def capture_stable_dock_frame(self) -> np.ndarray:
        self.capture_calls += 1
        self.shared.fill(self.capture_calls)
        return self.shared


class _Scroll:
    edge_threshold = 0.05

    def __init__(
        self,
        initial: float,
        moves: list[float] | None = None,
        *,
        top_result: float = 0.0,
        appear: bool = True,
        appear_results: list[bool] | None = None,
    ) -> None:
        self.current = initial
        self.moves = [] if moves is None else list(moves)
        self.top_result = top_result
        self.is_present = appear
        self.appear_results = [] if appear_results is None else list(appear_results)
        self.set_top_calls = 0
        self.next_page_calls = 0
        self.pages: list[float] = []

    def appear(self, main) -> bool:
        if self.appear_results:
            return self.appear_results.pop(0)
        return self.is_present

    def cal_position(self, main) -> float:
        return self.current

    def set_top(self, main, **kwargs):
        self.set_top_calls += 1
        self.current = self.top_result

    def next_page(self, main, *, page, **kwargs):
        self.next_page_calls += 1
        self.pages.append(page)
        if self.moves:
            self.current = self.moves.pop(0)


@pytest.mark.parametrize("position", [math.nan, math.inf, -0.01, 1.01])
def test_scroll_position_rejects_non_finite_or_out_of_range(position: float) -> None:
    traversal = DockInventoryTraversal(_Runtime(), scroll=_Scroll(position))

    with pytest.raises(DockInventoryTraversalError, match="Недопустимая позиция"):
        traversal.read_scroll_position()


def test_missing_scrollbar_fails_closed() -> None:
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=_Scroll(0.0, appear=False),
    )

    with pytest.raises(DockInventoryTraversalError, match="не подтверждена"):
        traversal.traverse(lambda _viewport: None)


def test_already_top_avoids_redundant_drag() -> None:
    scroll = _Scroll(0.01, moves=[1.0])
    traversal = DockInventoryTraversal(_Runtime(), scroll=scroll)

    result = traversal.traverse(lambda _viewport: None)

    assert result.positions == (0.01, 1.0)
    assert scroll.set_top_calls == 0


def test_middle_is_moved_to_top_and_independently_verified() -> None:
    scroll = _Scroll(0.5, moves=[1.0], top_result=0.0)
    traversal = DockInventoryTraversal(_Runtime(), scroll=scroll)

    result = traversal.traverse(lambda _viewport: None)

    assert result.positions == (0.0, 1.0)
    assert scroll.set_top_calls == 1


def test_top_command_without_top_evidence_fails() -> None:
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=_Scroll(0.5, top_result=0.2),
    )

    with pytest.raises(DockInventoryTraversalError, match="без подтверждённой"):
        traversal.traverse(lambda _viewport: None)


def test_scrollbar_disappearing_after_top_command_fails_closed() -> None:
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=_Scroll(
            0.5,
            top_result=0.0,
            appear_results=[True, False],
        ),
    )

    with pytest.raises(DockInventoryTraversalError, match="не подтверждена"):
        traversal.traverse(lambda _viewport: None)


def test_monotonic_traversal_visits_final_bottom_viewport_with_overlap_step() -> None:
    positions = [0.0, 0.18, 0.35, 0.52, 0.70, 0.86, 1.0]
    scroll = _Scroll(positions[0], moves=positions[1:])
    runtime = _Runtime()
    traversal = DockInventoryTraversal(runtime, scroll=scroll)
    visited = []

    result = traversal.traverse(visited.append)

    assert result.positions == tuple(positions)
    assert result.visited_viewports == len(positions)
    assert result.reached_bottom is True
    assert result.final_viewport_visited is True
    assert [viewport.scroll_position for viewport in visited] == positions
    assert visited[-1].is_bottom is True
    assert all(page == DockInventoryTraversal.PAGE_STEP for page in scroll.pages)


def test_no_progress_has_bounded_retries() -> None:
    scroll = _Scroll(0.0, moves=[0.0] * 10)
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=scroll,
        max_no_progress_retries=2,
    )

    with pytest.raises(DockInventoryTraversalError, match="не продвинулась"):
        traversal.traverse(lambda _viewport: None)

    assert scroll.next_page_calls == 3


def test_backwards_movement_has_bounded_retries() -> None:
    scroll = _Scroll(0.0, moves=[0.52, 0.31, 0.30])
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=scroll,
        max_no_progress_retries=1,
    )

    with pytest.raises(DockInventoryTraversalError, match="не продвинулась"):
        traversal.traverse(lambda _viewport: None)

    # One accepted forward step followed by two bounded reverse attempts.
    assert scroll.next_page_calls == 3


def test_max_viewports_is_a_safety_guard_not_bottom_detection() -> None:
    scroll = _Scroll(0.0, moves=[0.2, 0.4, 0.6])
    traversal = DockInventoryTraversal(
        _Runtime(),
        scroll=scroll,
        max_viewports=2,
    )

    with pytest.raises(DockInventoryTraversalError, match="окон Dock"):
        traversal.traverse(lambda _viewport: None)


def test_each_visitor_frame_owns_its_pixels_when_backend_reuses_buffer() -> None:
    runtime = _Runtime()
    traversal = DockInventoryTraversal(
        runtime,
        scroll=_Scroll(0.0, moves=[0.5, 1.0]),
    )
    frames = []

    traversal.traverse(lambda viewport: frames.append(viewport.frame))
    runtime.shared.fill(99)

    assert len({id(frame) for frame in frames}) == 3
    assert [int(frame[0, 0, 0]) for frame in frames] == [1, 2, 3]
