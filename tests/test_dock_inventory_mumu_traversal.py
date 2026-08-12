from __future__ import annotations

import cv2
import numpy as np

from module.dock_inventory.mumu_traversal import DockMuMuInventoryTraversal


class _Device:
    def __init__(self) -> None:
        self.image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.adb_calls: list[list[str]] = []

    def adb_shell(self, command: list[str]) -> None:
        self.adb_calls.append(command)


class _Runtime:
    def __init__(self, frames: list[np.ndarray] | None = None) -> None:
        self.device = _Device()
        self.frames = [] if frames is None else [np.array(frame, copy=True) for frame in frames]
        self.capture_calls = 0

    def capture_stable_dock_frame(self) -> np.ndarray:
        self.capture_calls += 1
        if self.frames:
            return np.array(self.frames.pop(0), copy=True)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[80:650, 90:1220] = self.capture_calls
        return frame


class _Scroll:
    edge_threshold = 0.05

    def __init__(self, initial: float, moves: list[float] | None = None) -> None:
        self.current = initial
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


def _texture(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(720, 1280, 3), dtype=np.uint8)


def _shift_y(frame: np.ndarray, shift_y: int) -> np.ndarray:
    matrix = np.float32([[1, 0, 0], [0, 1, shift_y]])
    return cv2.warpAffine(
        frame,
        matrix,
        (frame.shape[1], frame.shape[0]),
        borderMode=cv2.BORDER_REFLECT,
    )


def test_default_mumu_sender_uses_adb_input_swipe() -> None:
    runtime = _Runtime()
    traversal = DockMuMuInventoryTraversal(
        runtime,
        scroll=_Scroll(0.0),
        normalize_initial_viewport=False,
    )

    assert traversal._mumu_swipe_sender is not None
    traversal._mumu_swipe_sender((640, 560), (640, 160))

    assert runtime.device.adb_calls == [
        ["input", "swipe", "640", "560", "640", "160"]
    ]


def test_mumu_swipe_is_preferred_when_scrollbar_proves_progress() -> None:
    scroll = _Scroll(0.0)
    runtime = _Runtime()
    positions = iter((0.25, 0.52, 0.77, 1.0))
    swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def send(start: tuple[int, int], end: tuple[int, int]) -> None:
        swipes.append((start, end))
        scroll.current = next(positions)

    traversal = DockMuMuInventoryTraversal(
        runtime,
        scroll=scroll,
        mumu_swipe_sender=send,
        normalize_initial_viewport=False,
    )
    result = traversal.traverse(lambda _viewport: None)

    assert result.positions == (0.0, 0.25, 0.52, 0.77, 1.0)
    assert swipes == [
        (
            DockMuMuInventoryTraversal.MUMU_DOWN_START,
            DockMuMuInventoryTraversal.MUMU_DOWN_END,
        )
    ] * 4
    assert result.mumu_swipe_actions == 4
    assert result.mumu_swipe_progress_actions == 4
    assert result.dpad_actions == 0
    assert result.dpad_progress_actions == 0
    assert result.scroll_fallback_calls == 0
    assert scroll.next_page_calls == 0


def test_mumu_swipe_without_progress_disables_it_and_uses_scroll_fallback() -> None:
    scroll = _Scroll(0.0, moves=[1.0])
    swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def send(start: tuple[int, int], end: tuple[int, int]) -> None:
        swipes.append((start, end))

    traversal = DockMuMuInventoryTraversal(
        _Runtime(),
        scroll=scroll,
        mumu_swipe_sender=send,
        mumu_no_progress_retries=1,
        normalize_initial_viewport=False,
    )
    result = traversal.traverse(lambda _viewport: None)

    assert result.positions == (0.0, 1.0)
    assert len(swipes) == 2
    assert result.mumu_swipe_actions == 2
    assert result.mumu_swipe_progress_actions == 0
    assert result.scroll_fallback_calls == 1
    assert result.no_progress_retries == 2
    assert scroll.next_page_calls == 1


def test_phase_correlation_proves_expected_small_upward_content_shift() -> None:
    before = _texture()
    after = _shift_y(before, -22)

    shift_x, shift_y, response = DockMuMuInventoryTraversal._content_shift(
        before,
        after,
    )

    assert abs(shift_x) < 1.0
    assert -23.0 < shift_y < -21.0
    assert response > DockMuMuInventoryTraversal.INITIAL_NUDGE_MIN_PHASE_RESPONSE


def test_animation_only_change_does_not_mark_initial_nudge_as_applied() -> None:
    before = _texture()
    animated = np.array(before, copy=True)
    animated[200:235, 300:350] = 255 - animated[200:235, 300:350]
    restored = np.array(before, copy=True)
    scroll = _Scroll(0.0, moves=[1.0])
    runtime = _Runtime([before, animated, restored, _texture(2)])

    traversal = DockMuMuInventoryTraversal(
        runtime,
        scroll=scroll,
        mumu_swipe_sender=lambda _start, _end: None,
    )
    result = traversal.traverse(lambda _viewport: None)

    assert result.initial_nudge_applied is False
    assert result.initial_nudge_shift_y is not None
    assert abs(result.initial_nudge_shift_y) < 12.0
    assert scroll.set_top_calls == 1
    assert result.scroll_fallback_calls >= 2


def test_proven_initial_nudge_records_motion_evidence() -> None:
    before = _texture()
    shifted = _shift_y(before, -22)
    bottom = _texture(3)
    scroll = _Scroll(0.0, moves=[1.0])
    runtime = _Runtime([before, shifted, bottom])

    traversal = DockMuMuInventoryTraversal(
        runtime,
        scroll=scroll,
        mumu_swipe_sender=lambda _start, _end: None,
    )
    result = traversal.traverse(lambda _viewport: None)

    assert result.initial_nudge_applied is True
    assert result.initial_nudge_shift_y is not None
    assert -23.0 < result.initial_nudge_shift_y < -21.0
    assert result.initial_nudge_phase_response is not None
    assert (
        result.initial_nudge_phase_response
        > DockMuMuInventoryTraversal.INITIAL_NUDGE_MIN_PHASE_RESPONSE
    )
