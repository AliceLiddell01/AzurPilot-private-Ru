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

    EXPECTED_HEIGHT = 31
    EXPECTED_WIDTH = 58

    # На 387 реальных Dock ROI и умеренных stress-вариантах минимальный raw
    # dynamic range равен 197. Этот guard оставляет большой запас и отсекает
    # пустые/почти константные входы до цветовой нормализации.
    RAW_RANGE_MIN = 64

    ONES_AREA = (44, 2, 54, 23)
    ONES_THRESHOLD = 140
    ONES_PIXEL_MIN = 15

    # Реальный v15 показал отдельный low-level стиль: тонкая серая цифра ``1``
    # может быть светлее strong evidence. Fallback лишь разрешает тот же один
    # OCR-проход и не подставляет значение. На ideal corpus strong path остаётся
    # достаточным для всех 380 PRESENT level ROI.
    LOW_CONTRAST_ONES_THRESHOLD = 170
    LOW_CONTRAST_ONES_PIXEL_MIN = 16

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

    ONE_DIGIT_TOP = 2
    ONE_DIGIT_BOTTOM = 23
    ONE_DIGIT_BINARY_THRESHOLD = 200

    # v16.4: семь реальных ``Lv.1`` имели соседний artwork, который переживал
    # обычный threshold. Отдельный более тёмный proof ищет длинный вертикальный
    # stroke только в каноническом numeric core. Если proof не сложился, старый
    # one-digit preprocessing остаётся полностью неизменным — это важно для
    # будущих однозначных цифр, не похожих на ``1``.
    ONE_DIGIT_STROKE_BINARY_THRESHOLD = 180
    ONE_DIGIT_STROKE_CORE_LEFT = 1
    ONE_DIGIT_STROKE_CORE_RIGHT = 6
    ONE_DIGIT_STROKE_COLUMN_MIN = 14
    ONE_DIGIT_STROKE_WIDTH_MAX = 3
    ONE_DIGIT_STROKE_WINDOW_PADDING = 2

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

    def _one_digit_region(self, normalized: np.ndarray) -> np.ndarray:
        left = self.DIGIT_LEFT_BY_COUNT[1]
        region = normalized[
            self.ONE_DIGIT_TOP : self.ONE_DIGIT_BOTTOM,
            left:,
        ]
        _threshold, binary = cv2.threshold(
            region,
            self.ONE_DIGIT_BINARY_THRESHOLD,
            255,
            cv2.THRESH_BINARY,
        )
        _threshold, stroke_binary = cv2.threshold(
            region,
            self.ONE_DIGIT_STROKE_BINARY_THRESHOLD,
            255,
            cv2.THRESH_BINARY,
        )

        foreground = stroke_binary < 128
        column_counts = np.count_nonzero(foreground, axis=0)
        core_counts = column_counts[
            self.ONE_DIGIT_STROKE_CORE_LEFT : self.ONE_DIGIT_STROKE_CORE_RIGHT
        ]
        stroke_columns = np.flatnonzero(
            core_counts >= self.ONE_DIGIT_STROKE_COLUMN_MIN
        ) + self.ONE_DIGIT_STROKE_CORE_LEFT
        if not 1 <= len(stroke_columns) <= self.ONE_DIGIT_STROKE_WIDTH_MAX:
            return binary
        if len(stroke_columns) > 1 and np.any(np.diff(stroke_columns) != 1):
            return binary

        # Не превращаем proof в нарисованную цифру: сохраняем реальные pixels
        # вокруг доказанного stroke и только отбрасываем дальний artwork.
        window_left = max(
            0,
            int(stroke_columns[0]) - self.ONE_DIGIT_STROKE_WINDOW_PADDING,
        )
        window_right = min(
            stroke_binary.shape[1],
            int(stroke_columns[-1]) + self.ONE_DIGIT_STROKE_WINDOW_PADDING + 1,
        )
        cleaned = np.full_like(stroke_binary, 255)
        cleaned[:, window_left:window_right] = stroke_binary[:, window_left:window_right]
        return cleaned

    def _multi_digit_region(
        self,
        normalized: np.ndarray,
        digit_count: int,
    ) -> np.ndarray:
        left = self.DIGIT_LEFT_BY_COUNT[digit_count]
        region = np.array(normalized[:, left:], copy=True)

        # ONES_AREA уже является canonical authority правого numeric slot.
        # Всё правее его exclusive-right не участвовало в доказательстве цифры.
        # v16.4 Roon ``Lv.125`` показал там длинный artwork-штрих, который OCR
        # прочитал как четвёртую ``1``. Форму region сохраняем для старого OCR,
        # но недоказанный хвост делаем белым до единственного OCR-прогона.
        numeric_right = self.ONES_AREA[2] - left
        region[:, numeric_right:] = 255
        return region

    def pre_process(self, image: np.ndarray) -> np.ndarray:
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] != 3
            or image.shape[0] != self.EXPECTED_HEIGHT
            or image.shape[1] != self.EXPECTED_WIDTH
        ):
            return np.array([[255]], dtype=np.uint8)

        raw_range = int(image.max()) - int(image.min())
        if raw_range < self.RAW_RANGE_MIN:
            return np.array([[255]], dtype=np.uint8)

        normalized = self._normalize_level_pixels(image)

        ones = self._dark_pixel_count(
            normalized,
            self.ONES_AREA,
            self.ONES_THRESHOLD,
        )
        if ones < self.ONES_PIXEL_MIN:
            relaxed_ones = self._dark_pixel_count(
                normalized,
                self.ONES_AREA,
                self.LOW_CONTRAST_ONES_THRESHOLD,
            )
            if relaxed_ones < self.LOW_CONTRAST_ONES_PIXEL_MIN:
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

        if digit_count == 1:
            return self._one_digit_region(normalized)
        return self._multi_digit_region(normalized, digit_count)


__all__ = ["DockLevelOcr"]