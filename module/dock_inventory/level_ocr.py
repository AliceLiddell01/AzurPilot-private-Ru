"""Dock-specific preprocessing для OCR уровня корабля в Stage 5."""

from __future__ import annotations

import cv2
import numpy as np

from module.combat.level import COLOR_MASKED, COLOR_WHITE, LevelOcr


class DockLevelOcr(LevelOcr):
    """Изолирует правый числовой блок Dock перед единственным OCR-прогоном.

    Карточки Dock выравнивают одну, две или три цифры уровня по правому краю
    стабильного ROI 58x31. Combat ``LevelOcr`` сначала ищет префикс ``Lv.``, а
    затем применяет ограничение ширины combat-grid. Для Dock эта геометрия
    неверна и на реальных кадрах может дополнительно цепляться за artwork в
    области префикса. Здесь сохраняется та же цветовая нормализация, но число
    занятых numeric slots доказывается непосредственно по правой части ROI.
    """

    MIN_HEIGHT = 23
    MIN_WIDTH = 54

    ONES_AREA = (44, 2, 54, 23)
    ONES_THRESHOLD = 140
    ONES_PIXEL_MIN = 15

    HUNDREDS_AREA = (26, 2, 29, 22)
    HUNDREDS_THRESHOLD = 127
    HUNDREDS_PIXEL_MIN = 13

    TENS_AREA = (34, 4, 45, 15)
    TENS_THRESHOLD = 140
    TENS_PIXEL_MIN = 15

    DIGIT_LEFT_BY_COUNT = {
        1: 45,
        2: 32,
        3: 24,
    }

    @staticmethod
    def _normalize_level_pixels(image: np.ndarray) -> np.ndarray:
        image = np.array(image, copy=True)
        max_red = image[:8, :, 0].max()
        if max_red <= COLOR_MASKED[0]:
            scalar = np.mean(COLOR_WHITE) / np.mean(COLOR_MASKED)
            image = cv2.addWeighted(image, scalar, image, 0, 0)

        bg = (70, 102, 152)
        luma_trans = (0.299, 0.587, 0.114)
        luma_bg = np.dot(bg, luma_trans)
        image = cv2.subtract(image, bg).dot(luma_trans).round().astype(np.uint8)
        return cv2.subtract(
            255,
            cv2.multiply(image, 255 / (255 - luma_bg)),
        )

    @staticmethod
    def _dark_pixel_count(
        image: np.ndarray,
        area: tuple[int, int, int, int],
        threshold: int,
    ) -> int:
        left, top, right, bottom = area
        return int(np.count_nonzero(image[top:bottom, left:right] < threshold))

    def pre_process(self, image: np.ndarray) -> np.ndarray:
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] != 3
            or image.shape[0] < self.MIN_HEIGHT
            or image.shape[1] < self.MIN_WIDTH
        ):
            return np.array([[255]], dtype=np.uint8)

        normalized = self._normalize_level_pixels(image)

        ones = self._dark_pixel_count(
            normalized,
            self.ONES_AREA,
            self.ONES_THRESHOLD,
        )
        if ones < self.ONES_PIXEL_MIN:
            return np.array([[255]], dtype=np.uint8)

        hundreds = self._dark_pixel_count(
            normalized,
            self.HUNDREDS_AREA,
            self.HUNDREDS_THRESHOLD,
        )
        if hundreds >= self.HUNDREDS_PIXEL_MIN:
            digit_count = 3
        else:
            tens = self._dark_pixel_count(
                normalized,
                self.TENS_AREA,
                self.TENS_THRESHOLD,
            )
            digit_count = 2 if tens >= self.TENS_PIXEL_MIN else 1

        return normalized[:, self.DIGIT_LEFT_BY_COUNT[digit_count] :]


__all__ = ["DockLevelOcr"]
