"""Контролируемый обход прокручиваемой страницы Settings / Options.

У Options в EN-клиенте нет отдельного визуального thumb/track: правая
вертикальная линия принадлежит рамкам секций и не меняется вместе с позицией.
После последней секции content циклически продолжает ранние секции, поэтому
bottom здесь логический и подтверждается последней секцией до wrap.
Поэтому ``Scroll``/``AdaptiveScroll`` здесь не могут дать достоверную
нормализованную позицию. Traversal использует подтверждённые структурные
anchors, движение контента и ограниченные recovery-попытки.
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

OPTIONS_SAFE_SWIPE_START = (690, 610)
OPTIONS_SAFE_SWIPE_END = (690, 360)
"""Центральный зазор между колонками: gesture не начинается на toggle."""

OPTIONS_BOTTOM_ANCHOR_OFFSET = (-8, -215, 8, 115)
"""Допустимое окно последней секции: вверх шире, вниз только до полного показа."""


@dataclass(frozen=True, slots=True)
class OptionsViewport:
    """Метаданные стабильного viewport, доступного visitor-у.

    Актуальный frame во время callback находится в ``self.device.image``.
    ``scroll_offset`` — накопленное визуальное смещение в пикселях, а не
    выдуманная позиция 0..1: у страницы нет измеримого scrollbar.
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
            abs(self.vertical_shift) <= 3.0
            and abs(self.horizontal_shift) <= 3.0
            and self.edge_change <= 0.045
        )


def _options_edges(image: np.ndarray) -> np.ndarray:
    x1, y1, x2, y2 = OPTIONS_VIEWPORT_AREA
    viewport = image[y1:y2, x1:x2]
    if viewport.shape[:2] != (y2 - y1, x2 - x1):
        raise ValueError(
            "Options traversal ожидает screenshot в нативной геометрии 1280 x 720."
        )
    gray = cv2.cvtColor(viewport, cv2.COLOR_BGR2GRAY)
    return cv2.Canny(gray, 80, 160)


def measure_options_viewport_motion(
    previous: np.ndarray,
    current: np.ndarray,
) -> OptionsViewportMotion:
    """Измерить вертикальное движение, подавляя animated background.

    Canny оставляет главным образом стабильные рамки и текст, а phase
    correlation оценивает общий вертикальный сдвиг перекрывающейся области.
    ``edge_change`` отдельно отличает неподвижный viewport от неоднозначного
    кадра с визуальным шумом.
    """

    previous_edges = _options_edges(previous)
    current_edges = _options_edges(current)
    (horizontal, raw_vertical), response = cv2.phaseCorrelate(
        previous_edges.astype(np.float32),
        current_edges.astype(np.float32),
    )
    edge_change = float(np.mean(previous_edges != current_edges))
    # После жеста вниз content движется вверх, поэтому положительный scroll
    # progress равен ``-raw_vertical``. Повторяющиеся строки Options могут
    # сопоставить завершённый шаг 200+ px с другим пиком; edge density даёт
    # консервативный magnitude floor, не уничтожая знак обратного движения.
    viewport_height = previous_edges.shape[0]
    signed_vertical = -float(raw_vertical)
    if (
        abs(raw_vertical) <= 3.0
        and abs(horizontal) <= 3.0
        and edge_change <= 0.045
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
    """Reusable navigation/progression contract для будущих detectors."""

    options_max_viewports = 16
    options_max_top_swipes = 16
    options_max_no_progress_retries = 2
    options_stabilization_timeout = 1.5
    options_stable_frames = 4
    # Selected-icon animation после входа может не совпасть до 5 кадров;
    # восемь последовательных misses всё ещё дают bounded fail за < 0.5 с.
    options_page_loss_frames = 8
    options_min_progress = 5.0
    options_min_motion_response = 0.10
    options_swipe_duration = 0.24

    def traverse_options(
        self,
        visitor: Callable[[OptionsViewport], bool | None],
    ) -> OptionsTraversalResult:
        """Посетить Options сверху вниз; оставить Options на bottom/early-stop.

        Visitor вызывается только на подтверждённой странице и после visual
        stabilization. Истинный результат visitor-а означает раннюю остановку.
        Interpretation текущего frame остаётся обязанностью visitor-а.
        """

        if not callable(visitor):
            raise TypeError("visitor должен быть callable")

        self.ensure_options_page()
        frame = self._normalize_options_top()
        offset = 0.0
        visited = 0
        no_progress = 0

        logger.info("[Игровые настройки] Options: верх подтверждён")

        while visited < self.options_max_viewports:
            self._confirm_options_page(frame)
            is_bottom = self._options_anchor_matches(
                frame,
                GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR,
            )
            viewport = OptionsViewport(
                index=visited + 1,
                scroll_offset=offset,
                is_top=visited == 0,
                is_bottom=is_bottom,
            )
            logger.info(
                "[Игровые настройки] Viewport Options #%s: смещение %.1f px%s",
                viewport.index,
                viewport.scroll_offset,
                " (низ)" if is_bottom else "",
            )
            visited += 1

            if bool(visitor(viewport)):
                logger.info("[Игровые настройки] Обход Options: досрочная остановка")
                return OptionsTraversalResult(
                    visited_viewports=visited,
                    final_offset=offset,
                    reached_bottom=is_bottom,
                    stopped_early=True,
                )

            if is_bottom:
                logger.info("[Игровые настройки] Options: низ подтверждён")
                return OptionsTraversalResult(
                    visited_viewports=visited,
                    final_offset=offset,
                    reached_bottom=True,
                    stopped_early=False,
                )

            while True:
                self._swipe_options(down=True)
                next_frame = self._wait_options_stable()
                motion = self._measure_options_motion(frame, next_frame)

                if motion.stable:
                    no_progress += 1
                    logger.warning(
                        "[Игровые настройки] Options: нет прогресса (%s/%s)",
                        no_progress,
                        self.options_max_no_progress_retries,
                    )
                    if no_progress >= self.options_max_no_progress_retries:
                        raise GameStuckError(
                            "[Game Settings] Options не прокручивается, но bottom anchor отсутствует."
                        )
                    frame = next_frame
                    continue

                no_progress = 0
                self._validate_downward_progress(motion)
                offset += motion.vertical_shift
                frame = next_frame
                break

        raise GameStuckError(
            "[Game Settings] Options traversal превысил аварийный лимит viewport."
        )

    def _normalize_options_top(self) -> np.ndarray:
        frame = self._wait_options_stable()
        if self._options_anchor_matches(frame, GAME_SETTINGS_OPTIONS_TOP_ANCHOR):
            return frame

        no_progress = 0
        for _ in range(self.options_max_top_swipes):
            self._swipe_options(down=False)
            next_frame = self._wait_options_stable()
            if self._options_anchor_matches(
                next_frame,
                GAME_SETTINGS_OPTIONS_TOP_ANCHOR,
            ):
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
                # На section boundary phase response иногда слабый. Значимое
                # изменение edges допустимо только до визуального top anchor.
                if (
                    motion.response < self.options_min_motion_response
                    and motion.edge_change <= 0.06
                ):
                    raise GameStuckError(
                        "[Game Settings] Неоднозначное движение Options при reset к top."
                    )
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

    def _confirm_options_page(self, _frame: np.ndarray) -> None:
        if not self._options_page_visible(_frame):
            raise GamePageUnknownError(
                "[Game Settings] Страница Options потеряна во время traversal."
            )

    def _options_page_visible(self, _frame: np.ndarray) -> bool:
        return page_settings_options.check_button.match(
            _frame,
            offset=(5, 5),
            similarity=0.78,
        )

    @staticmethod
    def _options_anchor_matches(frame: np.ndarray, anchor) -> bool:
        if anchor is GAME_SETTINGS_OPTIONS_BOTTOM_ANCHOR:
            # Последняя секция должна войти в центральную часть viewport,
            # чтобы visitor увидел её полностью до циклического wrap. Live
            # drag может поставить заголовок на y=197, поэтому верхняя граница
            # шире симметричного offset; нижняя намеренно остаётся bounded.
            return anchor.match(
                frame,
                offset=OPTIONS_BOTTOM_ANCHOR_OFFSET,
                similarity=0.82,
            )
        return anchor.match(frame, offset=(3, 3), similarity=0.82)

    @staticmethod
    def _measure_options_motion(
        previous: np.ndarray,
        current: np.ndarray,
    ) -> OptionsViewportMotion:
        return measure_options_viewport_motion(previous, current)

    def _swipe_options(self, *, down: bool) -> None:
        start = OPTIONS_SAFE_SWIPE_START
        end = OPTIONS_SAFE_SWIPE_END
        if not down:
            start, end = end, start
        # ``drag`` удерживает endpoint перед отпусканием. На live minitouch
        # обычный swipe сохранял инерцию около 2 секунд; controlled drag
        # стабилизируется к следующему screenshot и сохраняет overlap.
        self.device.drag(
            start,
            end,
            segments=1,
            shake=(0, 0),
            point_random=(0, 0, 0, 0),
            shake_random=(0, 0, 0, 0),
            swipe_duration=self.options_swipe_duration,
            name="GAME_SETTINGS_OPTIONS",
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
