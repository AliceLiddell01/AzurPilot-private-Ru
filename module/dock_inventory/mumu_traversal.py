"""MuMu-first Dock traversal с проверяемым ADB swipe и Scroll fallback."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from module.dock_inventory.traversal import (
    DockInventoryTraversal,
    DockInventoryTraversalError,
    DockTraversalResult,
    DockTraversalViewport,
    DockViewportVisitor,
)
from module.logger import logger


DockMuMuSwipeSender = Callable[[tuple[int, int], tuple[int, int]], object]


@dataclass(frozen=True, slots=True)
class DockMuMuTraversalResult(DockTraversalResult):
    """Traversal evidence для MuMu swipe path поверх общего Stage 2 контракта."""

    mumu_swipe_actions: int = 0
    mumu_swipe_progress_actions: int = 0
    initial_nudge_shift_y: float | None = None
    initial_nudge_phase_response: float | None = None
    initial_nudge_reverted: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        for name, value in (
            ("mumu_swipe_actions", self.mumu_swipe_actions),
            ("mumu_swipe_progress_actions", self.mumu_swipe_progress_actions),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} должен быть неотрицательным int")
        if self.mumu_swipe_progress_actions > self.mumu_swipe_actions:
            raise ValueError(
                "mumu_swipe_progress_actions не может превышать mumu_swipe_actions"
            )
        for name, value in (
            ("initial_nudge_shift_y", self.initial_nudge_shift_y),
            ("initial_nudge_phase_response", self.initial_nudge_phase_response),
        ):
            if value is not None and (
                not isinstance(value, float) or not math.isfinite(value)
            ):
                raise ValueError(f"{name} должен быть конечным float или None")
        if type(self.initial_nudge_reverted) is not bool:
            raise TypeError("initial_nudge_reverted должен быть bool")
        if self.initial_nudge_applied and self.initial_nudge_reverted:
            raise ValueError(
                "Initial nudge не может одновременно быть принят и отменён"
            )


class DockMuMuInventoryTraversal(DockInventoryTraversal):
    """Предпочитать MuMu ADB swipe, сохраняя scrollbar как authority.

    MuMu keyboard mapping умеет превращать физическую клавишу в Slide/drag.
    Android DPAD keyevent этому жесту не эквивалентен, поэтому production path
    посылает непосредственно ``adb shell input swipe``. Ни один swipe не
    считается успешным сам по себе: новый stable frame и scrollbar обязаны
    независимо доказать движение. При недоступном/неэффективном swipe остаётся
    canonical ``Scroll`` fallback базового traversal.
    """

    # Основной жест примерно сдвигает содержимое на две строки, сохраняя overlap.
    MUMU_DOWN_START = (640, 560)
    MUMU_DOWN_END = (640, 160)
    MUMU_NO_PROGRESS_RETRIES = 1

    # Отдельный малый top-nudge нужен только для полного третьего ряда.
    INITIAL_NUDGE_START = (640, 360)
    INITIAL_NUDGE_END = (640, 336)
    INITIAL_NUDGE_MIN_SHIFT_Y = 12.0
    INITIAL_NUDGE_MAX_SHIFT_Y = 40.0
    INITIAL_NUDGE_MAX_SHIFT_X = 8.0
    INITIAL_NUDGE_NO_MOTION_MAX_SHIFT = 4.0
    INITIAL_NUDGE_MIN_PHASE_RESPONSE = 0.55
    INITIAL_NUDGE_ROI = (70, 60, 1230, 660)

    def __init__(
        self,
        main,
        *,
        prefer_mumu_swipe: bool = True,
        mumu_swipe_sender: DockMuMuSwipeSender | None = None,
        mumu_no_progress_retries: int = MUMU_NO_PROGRESS_RETRIES,
        normalize_initial_viewport: bool = True,
        **kwargs: object,
    ) -> None:
        if type(prefer_mumu_swipe) is not bool:
            raise TypeError("prefer_mumu_swipe должен быть bool")
        if type(normalize_initial_viewport) is not bool:
            raise TypeError("normalize_initial_viewport должен быть bool")
        if type(mumu_no_progress_retries) is not int or mumu_no_progress_retries < 0:
            raise ValueError("mumu_no_progress_retries должен быть неотрицательным int")
        if mumu_swipe_sender is not None and not callable(mumu_swipe_sender):
            raise TypeError("mumu_swipe_sender должен быть callable или None")

        # Legacy DPAD остаётся доступен только при явной работе с базовым классом.
        kwargs.pop("prefer_keyevents", None)
        kwargs.pop("keyevent_sender", None)
        kwargs.pop("keyevent_no_progress_retries", None)
        kwargs.pop("max_top_keyevent_steps", None)
        super().__init__(
            main,
            prefer_keyevents=False,
            normalize_initial_viewport=False,
            **kwargs,
        )
        self.mumu_no_progress_retries = mumu_no_progress_retries
        self._normalize_initial = normalize_initial_viewport
        self._mumu_swipe_sender = (
            self._resolve_mumu_swipe_sender(mumu_swipe_sender)
            if prefer_mumu_swipe
            else None
        )
        self._mumu_swipe_actions = 0
        self._mumu_swipe_progress_actions = 0
        self._initial_nudge_shift_y: float | None = None
        self._initial_nudge_phase_response: float | None = None
        self._initial_nudge_reverted = False

    def _resolve_mumu_swipe_sender(
        self,
        explicit: DockMuMuSwipeSender | None,
    ) -> DockMuMuSwipeSender | None:
        if explicit is not None:
            return explicit
        adb_shell = getattr(self.main.device, "adb_shell", None)
        if not callable(adb_shell):
            return None

        def send(start: tuple[int, int], end: tuple[int, int]) -> object:
            return adb_shell(
                [
                    "input",
                    "swipe",
                    str(start[0]),
                    str(start[1]),
                    str(end[0]),
                    str(end[1]),
                ]
            )

        return send

    def _disable_mumu_swipe(self, reason: str) -> None:
        if self._mumu_swipe_sender is None:
            return
        logger.warning(
            "[Инвентарь дока] MuMu ADB swipe отключён: %s; "
            "используется проверенный резервный Scroll.",
            reason,
        )
        self._mumu_swipe_sender = None

    def _mumu_swipe_candidate(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
    ) -> tuple[np.ndarray, float] | None:
        sender = self._mumu_swipe_sender
        if sender is None:
            return None
        try:
            sender(start, end)
        except Exception as exc:
            self._disable_mumu_swipe(
                "отправка ADB swipe завершилась ошибкой "
                f"{type(exc).__name__}: {exc}"
            )
            return None

        self._mumu_swipe_actions += 1
        frame = self.capture_stable_frame()
        try:
            position = self.read_scroll_position()
        except DockInventoryTraversalError as exc:
            self._disable_mumu_swipe(
                f"после swipe не подтверждена полоса прокрутки: {exc}"
            )
            return None
        return frame, position

    @classmethod
    def _initial_nudge_geometry_supported(cls, frame: np.ndarray) -> bool:
        if (
            not isinstance(frame, np.ndarray)
            or frame.dtype != np.uint8
            or frame.ndim != 3
            or frame.shape[2] != 3
        ):
            return False
        _left, _top, right, bottom = cls.INITIAL_NUDGE_ROI
        return frame.shape[1] >= right and frame.shape[0] >= bottom

    @classmethod
    def _content_shift(
        cls,
        before: np.ndarray,
        after: np.ndarray,
    ) -> tuple[float, float, float]:
        """Оценить глобальный сдвиг Dock, игнорируя локальную анимацию."""
        if (
            not cls._initial_nudge_geometry_supported(before)
            or not cls._initial_nudge_geometry_supported(after)
            or before.shape != after.shape
        ):
            raise DockInventoryTraversalError(
                "Initial nudge motion proof получил несовместимые stable frames."
            )
        left, top, right, bottom = cls.INITIAL_NUDGE_ROI
        before_gray = cv2.cvtColor(
            before[top:bottom, left:right],
            cv2.COLOR_RGB2GRAY,
        ).astype(np.float32)
        after_gray = cv2.cvtColor(
            after[top:bottom, left:right],
            cv2.COLOR_RGB2GRAY,
        ).astype(np.float32)
        window = cv2.createHanningWindow(
            (before_gray.shape[1], before_gray.shape[0]),
            cv2.CV_32F,
        )
        try:
            (shift_x, shift_y), response = cv2.phaseCorrelate(
                before_gray,
                after_gray,
                window,
            )
        except cv2.error as exc:
            raise DockInventoryTraversalError(
                f"Initial nudge phase correlation завершилась ошибкой: {exc}"
            ) from exc
        values = (float(shift_x), float(shift_y), float(response))
        if not all(math.isfinite(value) for value in values):
            raise DockInventoryTraversalError(
                "Initial nudge phase correlation вернула нечисловой результат."
            )
        return values

    @classmethod
    def _target_nudge_proven(
        cls,
        shift_x: float,
        shift_y: float,
        response: float,
    ) -> bool:
        return (
            abs(shift_x) <= cls.INITIAL_NUDGE_MAX_SHIFT_X
            and -cls.INITIAL_NUDGE_MAX_SHIFT_Y
            <= shift_y
            <= -cls.INITIAL_NUDGE_MIN_SHIFT_Y
            and response >= cls.INITIAL_NUDGE_MIN_PHASE_RESPONSE
        )

    @classmethod
    def _no_motion_proven(
        cls,
        shift_x: float,
        shift_y: float,
        response: float,
    ) -> bool:
        return (
            abs(shift_x) <= cls.INITIAL_NUDGE_MAX_SHIFT_X
            and abs(shift_y) <= cls.INITIAL_NUDGE_NO_MOTION_MAX_SHIFT
            and response >= cls.INITIAL_NUDGE_MIN_PHASE_RESPONSE
        )

    def _restore_verified_top(
        self,
        reference_frame: np.ndarray,
    ) -> tuple[np.ndarray, float]:
        self.scroll.set_top(self.main, skip_first_screenshot=True)
        self._initial_nudge_reverted = True
        restored_frame = self.capture_stable_frame()
        restored = self.read_scroll_position()
        if restored > self.scroll.edge_threshold:
            raise DockInventoryTraversalError(
                "После отката initial nudge начало Dock не подтверждено: "
                f"position={restored:.6f}."
            )
        shift_x, shift_y, response = self._content_shift(
            reference_frame,
            restored_frame,
        )
        if not self._no_motion_proven(shift_x, shift_y, response):
            raise DockInventoryTraversalError(
                "Scroll.set_top вернул scrollbar к top, но не доказал возврат "
                "исходной геометрии Dock: "
                f"dx={shift_x:.3f}, dy={shift_y:.3f}, response={response:.3f}."
            )
        logger.info(
            "[Инвентарь дока] Initial nudge откатан с доказанным возвратом top: "
            "dx=%.3f, dy=%.3f, response=%.3f.",
            shift_x,
            shift_y,
            response,
        )
        return restored_frame, restored

    def _normalize_mumu_initial_viewport(
        self,
        frame: np.ndarray,
        position: float,
    ) -> tuple[np.ndarray, float]:
        if not self._normalize_initial or self._mumu_swipe_sender is None:
            return frame, position
        if not self._initial_nudge_geometry_supported(frame):
            logger.warning(
                "[Инвентарь дока] MuMu initial nudge пропущен: stable frame "
                "не поддерживает 1280x720 motion-proof geometry."
            )
            return frame, position

        actions_before = self._mumu_swipe_actions
        candidate_result = self._mumu_swipe_candidate(
            self.INITIAL_NUDGE_START,
            self.INITIAL_NUDGE_END,
        )
        action_was_sent = self._mumu_swipe_actions > actions_before
        if candidate_result is None:
            if action_was_sent:
                raise DockInventoryTraversalError(
                    "Initial nudge был отправлен, но post-swipe scrollbar evidence "
                    "не подтверждён; безопасная геометрия первого viewport неизвестна."
                )
            return frame, position

        candidate_frame, candidate = candidate_result
        if candidate > self.scroll.edge_threshold:
            logger.warning(
                "[Инвентарь дока] Initial nudge вышел за top threshold: "
                "до=%.6f, после=%.6f; доказанный откат к исходному top.",
                position,
                candidate,
            )
            return self._restore_verified_top(frame)

        shift_x, shift_y, response = self._content_shift(frame, candidate_frame)
        self._initial_nudge_shift_y = shift_y
        self._initial_nudge_phase_response = response
        if self._target_nudge_proven(shift_x, shift_y, response):
            self._initial_nudge_applied = True
            logger.info(
                "[Инвентарь дока] MuMu initial nudge доказан: "
                "dx=%.3f, dy=%.3f, response=%.3f, scrollbar=%.6f.",
                shift_x,
                shift_y,
                response,
                candidate,
            )
            return candidate_frame, candidate

        if self._no_motion_proven(shift_x, shift_y, response):
            logger.warning(
                "[Инвентарь дока] Initial nudge не сдвинул Dock: "
                "dx=%.3f, dy=%.3f, response=%.3f; используется исходный top frame.",
                shift_x,
                shift_y,
                response,
            )
            return frame, position

        raise DockInventoryTraversalError(
            "Initial nudge изменил Dock вне доказанного целевого диапазона; "
            "scrollbar top недостаточен для безопасного восстановления micro-shift: "
            f"dx={shift_x:.3f}, dy={shift_y:.3f}, response={response:.3f}."
        )

    def _result(
        self,
        positions: list[float],
        no_progress_retries: int,
    ) -> DockMuMuTraversalResult:
        return DockMuMuTraversalResult(
            visited_viewports=len(positions),
            positions=tuple(positions),
            reached_bottom=True,
            final_viewport_visited=True,
            no_progress_retries=no_progress_retries,
            dpad_actions=0,
            dpad_progress_actions=0,
            scroll_fallback_calls=self._scroll_fallback_calls,
            initial_nudge_applied=self._initial_nudge_applied,
            mumu_swipe_actions=self._mumu_swipe_actions,
            mumu_swipe_progress_actions=self._mumu_swipe_progress_actions,
            initial_nudge_shift_y=self._initial_nudge_shift_y,
            initial_nudge_phase_response=self._initial_nudge_phase_response,
            initial_nudge_reverted=self._initial_nudge_reverted,
        )

    def traverse(self, visitor: DockViewportVisitor) -> DockMuMuTraversalResult:
        """Visit Dock with MuMu swipe first and canonical Scroll as fallback."""
        self._reset_movement_evidence()
        self._mumu_swipe_actions = 0
        self._mumu_swipe_progress_actions = 0
        self._initial_nudge_shift_y = None
        self._initial_nudge_phase_response = None
        self._initial_nudge_reverted = False

        frame, position = self.canonicalize_top()
        frame, position = self._normalize_mumu_initial_viewport(frame, position)
        positions: list[float] = []
        no_progress_retries = 0
        steps = 0

        while len(positions) < self.max_viewports:
            is_bottom = position >= 1.0 - self.scroll.edge_threshold
            visitor(
                DockTraversalViewport(
                    index=len(positions),
                    scroll_position=position,
                    is_top=position <= self.scroll.edge_threshold,
                    is_bottom=is_bottom,
                    frame=np.array(frame, copy=True),
                )
            )
            positions.append(position)
            if is_bottom:
                return self._result(positions, no_progress_retries)

            next_frame: np.ndarray | None = None
            next_position: float | None = None

            if self._mumu_swipe_sender is not None:
                swipe_failures = 0
                while swipe_failures <= self.mumu_no_progress_retries:
                    if steps >= self.max_steps:
                        raise DockInventoryTraversalError(
                            f"Достигнут safety-лимит шагов Dock: {self.max_steps}."
                        )
                    actions_before = self._mumu_swipe_actions
                    candidate_result = self._mumu_swipe_candidate(
                        self.MUMU_DOWN_START,
                        self.MUMU_DOWN_END,
                    )
                    steps += self._mumu_swipe_actions - actions_before
                    if candidate_result is None:
                        break
                    candidate_frame, candidate = candidate_result
                    reached_bottom = candidate >= 1.0 - self.scroll.edge_threshold
                    progressed = candidate > position + self.progress_epsilon
                    reversed_too_far = candidate < position - self.reverse_tolerance
                    if reached_bottom or (progressed and not reversed_too_far):
                        self._mumu_swipe_progress_actions += 1
                        next_frame = candidate_frame
                        next_position = candidate
                        break

                    swipe_failures += 1
                    no_progress_retries += 1
                    reason = "обратное движение" if reversed_too_far else "нет прогресса"
                    logger.warning(
                        "[Инвентарь дока] MuMu swipe: %s: до=%.6f, "
                        "после=%.6f, повтор=%s/%s",
                        reason,
                        position,
                        candidate,
                        swipe_failures,
                        self.mumu_no_progress_retries + 1,
                    )

                if next_frame is None:
                    self._disable_mumu_swipe(
                        "swipe не подтвердил движение Dock к низу"
                    )

            if next_frame is None:
                for attempt in range(self.max_no_progress_retries + 1):
                    if steps >= self.max_steps:
                        raise DockInventoryTraversalError(
                            f"Достигнут safety-лимит шагов Dock: {self.max_steps}."
                        )
                    self._scroll_fallback_calls += 1
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
                    f"попыток: previous={position:.6f}."
                )
            frame = next_frame
            position = next_position

        raise DockInventoryTraversalError(
            f"Достигнут safety-лимит окон Dock без подтверждённого низа: {self.max_viewports}."
        )


__all__ = ["DockMuMuInventoryTraversal", "DockMuMuTraversalResult"]
