"""Контролируемый обход прокручиваемой страницы Settings / Options.

У Options в EN-клиенте нет отдельного визуального thumb/track: правая
вертикальная линия принадлежит рамкам секций и не меняется вместе с позицией.
Live-прогон на MuMu показал, что страница имеет фактический hard end: после
последней секции controlled drag больше не двигает content.

Исторический ``GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR`` на live MuMu оказался
не terminal bottom, а устойчивым landmark нижней части страницы: после него
ещё доступны Game Settings и последующие строки. Он сохраняется как маркер
того, что traversal уже вошёл в нижнюю область, но сам по себе не завершает
обход.

Позиция страницы определяется прежде всего структурными/семантическими
landmark-ами. Phase correlation остаётся вторичным сигналом движения и
стабилизации: повторяющиеся строки и elastic rebound у нижней границы могут
давать неверный знак глобального сдвига. Terminal landmark завершает обход
только после того, как соответствующий стабильный viewport был отдан visitor-у.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from module.base.timer import Timer
from module.exception import GamePageUnknownError, GameStuckError
from module.game_settings.assets import (
    GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
    GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
)
from module.game_settings.navigation import page_settings_options
from module.logger import logger

OPTIONS_VIEWPORT_AREA = (175, 80, 1207, 690)
"""Область движущегося Options content в нативной сетке 1280 x 720."""

OPTIONS_STABLE_SHIFT_PX = 3.0
OPTIONS_STABLE_EDGE_CHANGE = 0.045
OPTIONS_TOP_ANCHOR_OFFSET = (3, 3)

OPTIONS_SAFE_SWIPE_START = (690, 610)
OPTIONS_SAFE_SWIPE_END = (690, 360)
"""Центральный зазор между колонками: gesture не начинается на toggle."""

OPTIONS_CONTROL_NAME = "GAME_SETTINGS_OPTIONS"
"""Имя controlled drag в общей защите Device от повторных действий."""

OPTIONS_BOTTOM_ANCHOR_OFFSET = (-8, -215, 8, 115)
"""Окно исторического lower landmark; это не terminal bottom Options."""

_OPTIONS_VIEWPORT_WIDTH = OPTIONS_VIEWPORT_AREA[2] - OPTIONS_VIEWPORT_AREA[0]
_OPTIONS_VIEWPORT_HEIGHT = OPTIONS_VIEWPORT_AREA[3] - OPTIONS_VIEWPORT_AREA[1]
_OPTIONS_PHASE_WINDOW = cv2.createHanningWindow(
    (_OPTIONS_VIEWPORT_WIDTH, _OPTIONS_VIEWPORT_HEIGHT),
    cv2.CV_32F,
)


@dataclass(frozen=True, slots=True)
class OptionsViewport:
    """Метаданные стабильного viewport, доступного visitor-у.

    Актуальный frame во время callback находится в ``self.device.image``.
    ``scroll_offset`` — накопленная диагностическая величина прогресса, а не
    выдуманная позиция 0..1: у страницы нет измеримого scrollbar.

    Terminal bottom подтверждается semantic landmark либо bounded отсутствием
    прогресса после нижнего landmark. Visitor-кадр заранее не помечается как
    bottom; итог полного обхода сообщает ``OptionsTraversalResult.reached_bottom``.
    """

    index: int
    scroll_offset: float
    is_top: bool
    is_bottom: bool


@dataclass(frozen=True, slots=True)
class OptionsTraversalResult:
    visited_viewports: int
    final_offset: float
    reached_bottom: bool
    stopped_early: bool


@dataclass(frozen=True, slots=True)
class OptionsViewportMotion:
    vertical_shift: float
    horizontal_shift: float
    response: float
    edge_change: float

    @property
    def stable(self) -> bool:
        return (
            abs(self.vertical_shift) <= OPTIONS_STABLE_SHIFT_PX
            and abs(self.horizontal_shift) <= OPTIONS_STABLE_SHIFT_PX
            and self.edge_change <= OPTIONS_STABLE_EDGE_CHANGE
        )


def _options_edges(image: np.ndarray) -> np.ndarray:
    x1, y1, x2, y2 = OPTIONS_VIEWPORT_AREA
    viewport = image[y1:y2, x1:x2]
    if viewport.shape[:2] != (y2 - y1, x2 - x1):
        raise ValueError(
            "Options traversal ожидает screenshot в нативной геометрии 1280 x 720."
        )
    gray = cv2.cvtColor(viewport, cv2.COLOR_RGB2GRAY)
    return cv2.Canny(gray, 80, 160)


def measure_options_viewport_motion(
    previous: np.ndarray,
    current: np.ndarray,
) -> OptionsViewportMotion:
    """Измерить визуальное движение, подавляя animated background.

    Результат служит вторичным evidence. Знак phase correlation не считается
    абсолютным источником позиции страницы: на повторяющихся строках и возле
    hard end он может выбрать соседний корреляционный пик.
    """

    previous_edges = _options_edges(previous)
    current_edges = _options_edges(current)
    (horizontal, raw_vertical), response = cv2.phaseCorrelate(
        previous_edges.astype(np.float32),
        current_edges.astype(np.float32),
        _OPTIONS_PHASE_WINDOW,
    )
    edge_change = float(np.mean(previous_edges != current_edges))
    viewport_height = previous_edges.shape[0]
    signed_vertical = -float(raw_vertical)
    if (
        abs(raw_vertical) <= OPTIONS_STABLE_SHIFT_PX
        and abs(horizontal) <= OPTIONS_STABLE_SHIFT_PX
        and edge_change <= OPTIONS_STABLE_EDGE_CHANGE
    ):
        vertical = signed_vertical
    else:
        magnitude = max(abs(float(raw_vertical)), edge_change * viewport_height)
        vertical = magnitude if signed_vertical >= 0 else -magnitude
    return OptionsViewportMotion(
        vertical_shift=vertical,
        horizontal_shift=float(horizontal),
        response=float(response),
        edge_change=edge_change,
    )


class OptionsTraversalMixin:
    """Reusable navigation/progression contract для Options detectors."""

    options_max_viewports = 16
    options_max_top_swipes = 16
    options_max_no_progress_retries = 2
    options_stabilization_timeout = 1.5
    options_stable_frames = 4
    options_page_loss_frames = 8
    options_min_progress = 5.0
    options_min_motion_response = 0.10
    options_swipe_duration = 0.24

    def traverse_options(
        self,
        visitor: Callable[[OptionsViewport], bool | None],
    ) -> OptionsTraversalResult:
        """Посетить Options сверху вниз до semantic/hard end или early-stop.

        Structural anchors и semantic landmarks определяют положение страницы.
        Motion используется для обычного шага и как дополнительная защита.
        Если phase correlation сообщает обратный знак, но следующий semantic
        landmark доказывает более глубокую позицию, semantic evidence имеет
        приоритет. После lower landmark допускается также non-regressing
        semantic evidence — это покрывает elastic rebound у физического низа.
        """

        if not callable(visitor):
            raise TypeError("visitor должен быть callable")

        self.ensure_options_page()
        frame = self._normalize_options_top()
        offset = 0.0
        visited = 0
        no_progress = 0
        lower_landmark_seen = False
        highest_semantic_rank = -1
        highest_semantic_key = None

        logger.info("[Игровые настройки] Options: верх подтверждён")

        while visited < self.options_max_viewports:
            self._confirm_options_page(frame)
            is_lower_landmark = self._options_anchor_matches(
                frame,
                GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
                offset=OPTIONS_BOTTOM_ANCHOR_OFFSET,
            )
            viewport = OptionsViewport(
                index=visited + 1,
                scroll_offset=offset,
                is_top=visited == 0,
                is_bottom=False,
            )
            logger.info(
                "[Игровые настройки] Viewport Options #%s: прогресс %.1f%s",
                viewport.index,
                viewport.scroll_offset,
                " (lower landmark)" if is_lower_landmark else "",
            )
            visited += 1

            if bool(visitor(viewport)):
                logger.info("[Игровые настройки] Обход Options: досрочная остановка")
                return OptionsTraversalResult(
                    visited_viewports=visited,
                    final_offset=offset,
                    reached_bottom=False,
                    stopped_early=True,
                )

            semantic = self._detect_options_semantic_landmark(frame)
            if semantic is not None:
                logger.info(
                    "[Игровые настройки] Semantic landmark: %s (rank=%s, score=%.3f)",
                    semantic.key,
                    semantic.rank,
                    semantic.score,
                )
                if semantic.rank > highest_semantic_rank:
                    highest_semantic_rank = semantic.rank
                    highest_semantic_key = semantic.key
                if semantic.terminal:
                    logger.info(
                        "[Игровые настройки] Terminal semantic landmark подтверждён; "
                        "фактический низ Options достигнут"
                    )
                    return OptionsTraversalResult(
                        visited_viewports=visited,
                        final_offset=offset,
                        reached_bottom=True,
                        stopped_early=False,
                    )

            if is_lower_landmark:
                lower_landmark_seen = True
                logger.info(
                    "[Игровые настройки] Options: lower landmark подтверждён; "
                    "обход продолжается до terminal semantic/hard end"
                )

            while True:
                self._swipe_options(down=True)
                next_frame = self._wait_options_stable()
                motion = self._measure_options_motion(frame, next_frame)

                if motion.stable:
                    next_semantic = self._detect_options_semantic_landmark(next_frame)
                    if next_semantic is not None and next_semantic.terminal:
                        logger.info(
                            "[Игровые настройки] Terminal semantic landmark вошёл "
                            "в стабильный нижний кадр; передаём его visitor-у"
                        )
                        self._clear_options_control_record()
                        frame = next_frame
                        break

                    no_progress += 1
                    if lower_landmark_seen:
                        logger.info(
                            "[Игровые настройки] Options: нет прогресса в нижней "
                            "области (%s/%s)",
                            no_progress,
                            self.options_max_no_progress_retries,
                        )
                        if no_progress >= self.options_max_no_progress_retries:
                            logger.info(
                                "[Игровые настройки] Options: фактический низ "
                                "подтверждён bounded no-progress"
                            )
                            return OptionsTraversalResult(
                                visited_viewports=visited,
                                final_offset=offset,
                                reached_bottom=True,
                                stopped_early=False,
                            )
                    else:
                        logger.warning(
                            "[Игровые настройки] Options: нет прогресса до lower "
                            "landmark (%s/%s)",
                            no_progress,
                            self.options_max_no_progress_retries,
                        )
                        if no_progress >= self.options_max_no_progress_retries:
                            raise GameStuckError(
                                "[Game Settings] Options не прокручивается до "
                                "подтверждения нижней области страницы."
                            )
                    frame = next_frame
                    continue

                if self._motion_is_normal_downward(motion):
                    no_progress = 0
                    self._clear_options_control_record()
                    offset += motion.vertical_shift
                    frame = next_frame
                    break

                next_semantic = self._detect_options_semantic_landmark(next_frame)
                semantic_forward = (
                    next_semantic is not None
                    and next_semantic.rank > highest_semantic_rank
                )
                semantic_non_regressing_lower = (
                    lower_landmark_seen
                    and next_semantic is not None
                    and next_semantic.rank >= highest_semantic_rank
                )

                if semantic_forward or semantic_non_regressing_lower:
                    if semantic_forward:
                        no_progress = 0
                    else:
                        no_progress += 1
                        logger.info(
                            "[Игровые настройки] Options: semantic-прогресс не "
                            "увеличился в нижней области (%s/%s)",
                            no_progress,
                            self.options_max_no_progress_retries,
                        )
                        if no_progress >= self.options_max_no_progress_retries:
                            logger.info(
                                "[Игровые настройки] Options: фактический низ "
                                "подтверждён bounded semantic non-progress"
                            )
                            return OptionsTraversalResult(
                                visited_viewports=visited,
                                final_offset=offset,
                                reached_bottom=True,
                                stopped_early=False,
                            )
                    reason = (
                        "forward semantic landmark"
                        if semantic_forward
                        else "non-regressing lower semantic landmark"
                    )
                    logger.warning(
                        "[Игровые настройки] Phase motion неоднозначен "
                        "(vertical=%.1f, horizontal=%.1f, response=%.3f), "
                        "но %s подтверждает продолжение: %s -> %s",
                        motion.vertical_shift,
                        motion.horizontal_shift,
                        motion.response,
                        reason,
                        highest_semantic_key or "<none>",
                        next_semantic.key,
                    )
                    self._clear_options_control_record()
                    if next_semantic.rank > highest_semantic_rank:
                        highest_semantic_rank = next_semantic.rank
                        highest_semantic_key = next_semantic.key
                    offset += self._semantic_progress_amount(motion)
                    frame = next_frame
                    break

                self._validate_downward_progress(motion)
                no_progress = 0
                self._clear_options_control_record()
                offset += motion.vertical_shift
                frame = next_frame
                break

        raise GameStuckError(
            "[Game Settings] Options traversal превысил аварийный лимит viewport."
        )

    def _normalize_options_top(self) -> np.ndarray:
        frame = self._wait_options_stable()
        if self._options_anchor_matches(
            frame,
            GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
            offset=OPTIONS_TOP_ANCHOR_OFFSET,
        ):
            return frame

        no_progress = 0
        for _ in range(self.options_max_top_swipes):
            self._swipe_options(down=False)
            next_frame = self._wait_options_stable()
            if self._options_anchor_matches(
                next_frame,
                GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
                offset=OPTIONS_TOP_ANCHOR_OFFSET,
            ):
                self._clear_options_control_record()
                return next_frame

            motion = self._measure_options_motion(frame, next_frame)
            if motion.stable:
                no_progress += 1
                if no_progress >= self.options_max_no_progress_retries:
                    raise GameStuckError(
                        "[Game Settings] Верх Options не подтверждён после bounded reset."
                    )
            else:
                no_progress = 0
                if (
                    motion.response < self.options_min_motion_response
                    and motion.edge_change <= 0.06
                ):
                    raise GameStuckError(
                        "[Game Settings] Неоднозначное движение Options при reset к top."
                    )
                self._clear_options_control_record()
            frame = next_frame

        raise GameStuckError(
            "[Game Settings] Options top не достигнут до аварийного лимита."
        )

    def _wait_options_stable(self) -> np.ndarray:
        timer = Timer.from_seconds(self.options_stabilization_timeout, speed=0.05).start()
        previous = None
        page_misses = 0
        stable_frames = 0

        while True:
            current = self._capture_options_frame()
            if not self._options_page_visible(current):
                page_misses += 1
                stable_frames = 0
                previous = None
                if page_misses >= self.options_page_loss_frames:
                    raise GamePageUnknownError(
                        "[Game Settings] Страница Options потеряна во время traversal."
                    )
            else:
                page_misses = 0

            if page_misses == 0:
                if previous is not None:
                    if self._measure_options_motion(previous, current).stable:
                        stable_frames += 1
                        if stable_frames >= self.options_stable_frames:
                            return current
                    else:
                        stable_frames = 0
                previous = current

            if timer.reached():
                break

        raise GameStuckError(
            "[Game Settings] Options viewport не стабилизировался в bounded interval."
        )

    def _capture_options_frame(self) -> np.ndarray:
        frame = self.device.screenshot()
        if frame.shape[:2] != (720, 1280):
            raise GameStuckError(
                "[Game Settings] Options screenshot не нормализован к 1280 x 720."
            )
        return frame.copy()

    def _confirm_options_page(self, frame: np.ndarray) -> None:
        if not self._options_page_visible(frame):
            raise GamePageUnknownError(
                "[Game Settings] Страница Options потеряна во время traversal."
            )

    def _options_page_visible(self, frame: np.ndarray) -> bool:
        return page_settings_options.check_button.match(
            frame,
            offset=(5, 5),
            similarity=0.78,
        )

    @staticmethod
    def _options_anchor_matches(frame: np.ndarray, anchor, *, offset) -> bool:
        return anchor.match(frame, offset=offset, similarity=0.82)

    @staticmethod
    def _measure_options_motion(
        previous: np.ndarray,
        current: np.ndarray,
    ) -> OptionsViewportMotion:
        return measure_options_viewport_motion(previous, current)

    @staticmethod
    def _detect_options_semantic_landmark(frame: np.ndarray):
        from module.game_settings.options_landmarks import (
            detect_options_semantic_landmark,
        )

        return detect_options_semantic_landmark(frame)

    def _motion_is_normal_downward(self, motion: OptionsViewportMotion) -> bool:
        return (
            motion.response >= self.options_min_motion_response
            and abs(motion.horizontal_shift) <= 5.0
            and motion.vertical_shift >= self.options_min_progress
        )

    def _semantic_progress_amount(self, motion: OptionsViewportMotion) -> float:
        return max(
            abs(motion.vertical_shift),
            motion.edge_change * _OPTIONS_VIEWPORT_HEIGHT,
            self.options_min_progress,
        )

    def _clear_options_control_record(self) -> None:
        """Сбросить только наш drag из click guard после доказанного прогресса."""

        remove = getattr(self.device, "click_record_remove", None)
        if callable(remove):
            remove(OPTIONS_CONTROL_NAME)

    def _swipe_options(self, *, down: bool) -> None:
        start = OPTIONS_SAFE_SWIPE_START
        end = OPTIONS_SAFE_SWIPE_END
        if not down:
            start, end = end, start
        self.device.drag(
            start,
            end,
            segments=1,
            shake=(0, 0),
            point_random=(0, 0, 0, 0),
            shake_random=(0, 0, 0, 0),
            swipe_duration=self.options_swipe_duration,
            name=OPTIONS_CONTROL_NAME,
        )

    def _validate_downward_progress(self, motion: OptionsViewportMotion) -> None:
        if motion.response < self.options_min_motion_response:
            raise GameStuckError(
                "[Game Settings] Движение Options не удалось измерить надёжно."
            )
        if abs(motion.horizontal_shift) > 5.0:
            raise GameStuckError(
                "[Game Settings] Options получил неожиданное горизонтальное смещение."
            )
        if motion.vertical_shift < self.options_min_progress:
            raise GameStuckError(
                "[Game Settings] Позиция Options неожиданно пошла назад при обходе вниз."
            )
