"""Fail-closed scrollbar traversal for Dock Inventory runtime consumers."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from module.logger import logger
from module.retire.dock import DOCK_SCROLL


class DockInventoryTraversalError(RuntimeError):
    """Dock traversal could not prove a required navigation invariant."""


@dataclass(frozen=True, slots=True)
class DockTraversalViewport:
    """One stable Dock viewport passed to the immediate visitor."""

    index: int
    scroll_position: float
    is_top: bool
    is_bottom: bool
    frame: np.ndarray = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("index должен быть неотрицательным int")
        if not math.isfinite(self.scroll_position) or not 0.0 <= self.scroll_position <= 1.0:
            raise ValueError("scroll_position должен быть конечным числом в [0, 1]")
        if not isinstance(self.frame, np.ndarray) or not self.frame.size:
            raise ValueError("frame должен быть непустым numpy.ndarray")
        if type(self.is_top) is not bool or type(self.is_bottom) is not bool:
            raise TypeError("is_top и is_bottom должны быть bool")


@dataclass(frozen=True, slots=True)
class DockTraversalResult:
    """Compact traversal evidence without retaining full screenshots."""

    visited_viewports: int
    positions: tuple[float, ...]
    reached_bottom: bool
    final_viewport_visited: bool
    no_progress_retries: int

    def __post_init__(self) -> None:
        if type(self.visited_viewports) is not int or self.visited_viewports < 0:
            raise ValueError("visited_viewports должен быть неотрицательным int")
        if self.visited_viewports != len(self.positions):
            raise ValueError("visited_viewports должен совпадать с числом positions")
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in self.positions):
            raise ValueError("positions должны содержать конечные значения в [0, 1]")
        if type(self.reached_bottom) is not bool or type(self.final_viewport_visited) is not bool:
            raise TypeError("Флаги результата обхода должны быть bool")
        if type(self.no_progress_retries) is not int or self.no_progress_retries < 0:
            raise ValueError("no_progress_retries должен быть неотрицательным int")


class _DockRuntime(Protocol):
    device: object

    def capture_stable_dock_frame(self) -> np.ndarray: ...


class _DockScroll(Protocol):
    edge_threshold: float

    def appear(self, main: object) -> bool: ...

    def cal_position(self, main: object) -> float: ...

    def set_top(self, main: object, **kwargs: object) -> object: ...

    def next_page(self, main: object, **kwargs: object) -> object: ...


DockViewportVisitor = Callable[[DockTraversalViewport], object]
DockKeyeventSender = Callable[[str], object]


class DockInventoryTraversal:
    """Visit every stable Dock viewport with independently proven progress.

    MuMu host-side arrow keys are known to scroll Dock smoothly, so runtime
    probes the Android DPAD equivalent as the preferred movement path. DPAD is
    kept only when the scrollbar independently proves the expected movement.
    If ADB keyevents are unavailable or ineffective, traversal disables them
    and falls back to the previously accepted canonical ``Scroll`` movement.

    Stage 2 intentionally requires a visible, non-degenerate scrollbar. A
    reliable single-viewport/small-Dock distinction needs card-presence data
    from a later stage, so absence is an operational failure here.
    """

    PAGE_STEP = 0.8
    PROGRESS_EPSILON = 0.005
    REVERSE_TOLERANCE = 0.01
    MAX_NO_PROGRESS_RETRIES = 3
    MAX_VIEWPORTS = 100
    MAX_STEPS = 400
    MAX_TOP_KEYEVENT_STEPS = 100
    KEYEVENT_NO_PROGRESS_RETRIES = 1
    DPAD_UP = "KEYCODE_DPAD_UP"
    DPAD_DOWN = "KEYCODE_DPAD_DOWN"

    def __init__(
        self,
        main: _DockRuntime,
        *,
        scroll: _DockScroll = DOCK_SCROLL,
        page_step: float = PAGE_STEP,
        progress_epsilon: float = PROGRESS_EPSILON,
        reverse_tolerance: float = REVERSE_TOLERANCE,
        max_no_progress_retries: int = MAX_NO_PROGRESS_RETRIES,
        max_viewports: int = MAX_VIEWPORTS,
        max_steps: int = MAX_STEPS,
        max_top_keyevent_steps: int = MAX_TOP_KEYEVENT_STEPS,
        keyevent_no_progress_retries: int = KEYEVENT_NO_PROGRESS_RETRIES,
        prefer_keyevents: bool = True,
        keyevent_sender: DockKeyeventSender | None = None,
    ) -> None:
        if not 0.0 < page_step < 1.0:
            raise ValueError("page_step должен быть в диапазоне (0, 1)")
        if progress_epsilon < 0 or reverse_tolerance < 0:
            raise ValueError("Допуски позиции не могут быть отрицательными")
        if any(
            type(value) is not int
            for value in (
                max_no_progress_retries,
                max_viewports,
                max_steps,
                max_top_keyevent_steps,
                keyevent_no_progress_retries,
            )
        ):
            raise TypeError("Лимиты обхода должны быть int")
        if (
            max_no_progress_retries < 0
            or max_viewports < 1
            or max_steps < 1
            or max_top_keyevent_steps < 1
            or keyevent_no_progress_retries < 0
        ):
            raise ValueError("Лимиты обхода должны быть положительными")
        if type(prefer_keyevents) is not bool:
            raise TypeError("prefer_keyevents должен быть bool")
        if keyevent_sender is not None and not callable(keyevent_sender):
            raise TypeError("keyevent_sender должен быть callable или None")

        self.main = main
        self.scroll = scroll
        self.page_step = page_step
        self.progress_epsilon = progress_epsilon
        self.reverse_tolerance = reverse_tolerance
        self.max_no_progress_retries = max_no_progress_retries
        self.max_viewports = max_viewports
        self.max_steps = max_steps
        self.max_top_keyevent_steps = max_top_keyevent_steps
        self.keyevent_no_progress_retries = keyevent_no_progress_retries
        self._keyevent_sender = (
            self._resolve_keyevent_sender(keyevent_sender) if prefer_keyevents else None
        )

    def _resolve_keyevent_sender(
        self,
        explicit: DockKeyeventSender | None,
    ) -> DockKeyeventSender | None:
        if explicit is not None:
            return explicit
        adb_shell = getattr(self.main.device, "adb_shell", None)
        if not callable(adb_shell):
            return None

        def send(keycode: str) -> object:
            return adb_shell(["input", "keyevent", keycode])

        return send

    def _disable_keyevents(self, reason: str) -> None:
        if self._keyevent_sender is None:
            return
        logger.warning(
            "[Инвентарь дока] ADB DPAD отключён: %s; используется проверенный резервный Scroll.",
            reason,
        )
        self._keyevent_sender = None

    def _keyevent_candidate(self, keycode: str) -> tuple[np.ndarray, float]:
        sender = self._keyevent_sender
        if sender is None:
            raise DockInventoryTraversalError(
                "Внутренняя ошибка: DPAD action запрошен после его отключения."
            )
        sender(keycode)
        frame = self.capture_stable_frame()
        return frame, self.read_scroll_position()

    def capture_stable_frame(self) -> np.ndarray:
        """Detach the current stable frame from a potentially reused backend buffer."""
        frame = self.main.capture_stable_dock_frame()
        if not isinstance(frame, np.ndarray) or not frame.size:
            raise DockInventoryTraversalError(
                "Стабильный кадр Dock отсутствует или имеет неверный тип."
            )
        owned = np.array(frame, copy=True)
        self.main.device.image = owned
        return owned

    def read_scroll_position(self) -> float:
        """Read and validate scrollbar evidence from the current stable frame."""
        if not self.scroll.appear(self.main):
            raise DockInventoryTraversalError(
                "Полоса прокрутки Dock не подтверждена на стабильном кадре."
            )
        try:
            position = float(self.scroll.cal_position(self.main))
        except (TypeError, ValueError, OverflowError) as exc:
            raise DockInventoryTraversalError(
                "Полоса прокрутки Dock вернула нечисловую позицию."
            ) from exc
        if not math.isfinite(position) or not 0.0 <= position <= 1.0:
            raise DockInventoryTraversalError(
                f"Недопустимая позиция полосы прокрутки Dock: {position!r}."
            )
        return position

    def canonicalize_top(self) -> tuple[np.ndarray, float]:
        """Reach top with DPAD_UP when proven, otherwise use verified Scroll fallback."""
        frame = self.capture_stable_frame()
        position = self.read_scroll_position()
        if position <= self.scroll.edge_threshold:
            return frame, position

        if self._keyevent_sender is not None:
            no_progress = 0
            for _step in range(self.max_top_keyevent_steps):
                previous = position
                candidate_frame, candidate = self._keyevent_candidate(self.DPAD_UP)
                if candidate <= self.scroll.edge_threshold:
                    return candidate_frame, candidate

                progressed = candidate < previous - self.progress_epsilon
                reversed_too_far = candidate > previous + self.reverse_tolerance
                if progressed and not reversed_too_far:
                    frame = candidate_frame
                    position = candidate
                    no_progress = 0
                    continue

                no_progress += 1
                reason = "обратное движение" if reversed_too_far else "нет прогресса"
                logger.warning(
                    "[Инвентарь дока] DPAD_UP: %s: до=%.6f, после=%.6f, "
                    "повтор=%s/%s",
                    reason,
                    previous,
                    candidate,
                    no_progress,
                    self.keyevent_no_progress_retries + 1,
                )
                if no_progress > self.keyevent_no_progress_retries:
                    self._disable_keyevents("DPAD_UP не подтвердил движение к началу")
                    break
            else:
                self._disable_keyevents(
                    f"DPAD_UP достиг лимита {self.max_top_keyevent_steps} действий"
                )

        self.scroll.set_top(self.main, skip_first_screenshot=True)
        frame = self.capture_stable_frame()
        position = self.read_scroll_position()
        if position > self.scroll.edge_threshold:
            raise DockInventoryTraversalError(
                "Команда перехода к началу Dock завершилась без подтверждённой "
                f"верхней позиции: position={position:.6f}."
            )
        return frame, position

    def traverse(self, visitor: DockViewportVisitor) -> DockTraversalResult:
        """Visit top through the confirmed final bottom viewport exactly once."""
        frame, position = self.canonicalize_top()
        positions: list[float] = []
        no_progress_retries = 0
        steps = 0

        while len(positions) < self.max_viewports:
            is_bottom = position >= 1.0 - self.scroll.edge_threshold
            viewport = DockTraversalViewport(
                index=len(positions),
                scroll_position=position,
                is_top=position <= self.scroll.edge_threshold,
                is_bottom=is_bottom,
                # The visitor must not be able to mutate the UI owner's
                # current evidence frame before the next controlled move.
                frame=np.array(frame, copy=True),
            )
            visitor(viewport)
            positions.append(position)

            if is_bottom:
                return DockTraversalResult(
                    visited_viewports=len(positions),
                    positions=tuple(positions),
                    reached_bottom=True,
                    final_viewport_visited=True,
                    no_progress_retries=no_progress_retries,
                )

            next_frame: np.ndarray | None = None
            next_position: float | None = None

            if self._keyevent_sender is not None:
                keyevent_failures = 0
                while keyevent_failures <= self.keyevent_no_progress_retries:
                    if steps >= self.max_steps:
                        raise DockInventoryTraversalError(
                            f"Достигнут safety-лимит шагов Dock: {self.max_steps}."
                        )
                    candidate_frame, candidate = self._keyevent_candidate(self.DPAD_DOWN)
                    steps += 1
                    reached_bottom = candidate >= 1.0 - self.scroll.edge_threshold
                    progressed = candidate > position + self.progress_epsilon
                    reversed_too_far = candidate < position - self.reverse_tolerance
                    if reached_bottom or (progressed and not reversed_too_far):
                        next_frame = candidate_frame
                        next_position = candidate
                        break

                    keyevent_failures += 1
                    no_progress_retries += 1
                    reason = "обратное движение" if reversed_too_far else "нет прогресса"
                    logger.warning(
                        "[Инвентарь дока] DPAD_DOWN: %s: до=%.6f, после=%.6f, "
                        "повтор=%s/%s",
                        reason,
                        position,
                        candidate,
                        keyevent_failures,
                        self.keyevent_no_progress_retries + 1,
                    )

                if next_frame is None:
                    self._disable_keyevents("DPAD_DOWN не подтвердил движение к низу")

            if next_frame is None:
                for attempt in range(self.max_no_progress_retries + 1):
                    if steps >= self.max_steps:
                        raise DockInventoryTraversalError(
                            f"Достигнут safety-лимит шагов Dock: {self.max_steps}."
                        )
                    self.scroll.next_page(
                        self.main,
                        page=self.page_step,
                        skip_first_screenshot=True,
                    )
                    steps += 1
                    candidate_frame = self.capture_stable_frame()
                    candidate = self.read_scroll_position()

                    reached_bottom = candidate >= 1.0 - self.scroll.edge_threshold
                    progressed = candidate > position + self.progress_epsilon
                    reversed_too_far = candidate < position - self.reverse_tolerance
                    if reached_bottom or (progressed and not reversed_too_far):
                        next_frame = candidate_frame
                        next_position = candidate
                        break

                    no_progress_retries += 1
                    reason = "обратное движение" if reversed_too_far else "нет прогресса"
                    logger.warning(
                        "[Инвентарь дока] %s после резервного Scroll: до=%.6f, "
                        "после=%.6f, повтор=%s/%s",
                        reason,
                        position,
                        candidate,
                        attempt + 1,
                        self.max_no_progress_retries + 1,
                    )

            if next_frame is None or next_position is None:
                raise DockInventoryTraversalError(
                    "Полоса прокрутки Dock не продвинулась за ограниченное число "
                    f"попыток: previous={position:.6f}, retries="
                    f"{self.max_no_progress_retries + 1}."
                )

            frame = next_frame
            position = next_position

        raise DockInventoryTraversalError(
            f"Достигнут safety-лимит окон Dock без подтверждённого низа: {self.max_viewports}."
        )
