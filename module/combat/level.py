"""Модуль определения уровня кораблей.

Распознаёт информацию об уровнях кораблей на боевом экране с помощью OCR.

Сценарии использования проверки уровней:
- Условие остановки по уровню: остановка кампании, когда любой корабль достигает целевого уровня
- Проверка LV.32: остановка при достижении флагманом 32 уровня (фарм алмазов)

Формат отображения уровня — "LV.XX". Перед OCR требуется:
1. Удалить префикс "LV.", оставив только цифры
2. Обработать эффект затемнения при низком здоровье
3. Обработать полупрозрачный синий фон

Данные об уровнях отслеживаются независимо для 6 позиций (3 авангард + 3 мейн).
"""

import module.config.server as server

from module.base.base import ModuleBase
from module.base.button import *
from module.base.decorator import Config
from module.logger import logger
from module.ocr.ocr import Digit

# Эталонные цвета: белый и после наложения маски
COLOR_WHITE = (255, 255, 255)
COLOR_MASKED = (107, 105, 107)


class Level(ModuleBase):
    """Детектор уровня кораблей.

    Считывает уровни кораблей на каждой позиции через OCR и предоставляет логику условий остановки по уровню.

    Attributes:
        _lv (list[int]): Текущие уровни по позициям, -1 означает, что уровень не определён.
        _lv_before_battle (list[int]): Снимок уровней до боя для определения повышения уровня.
    """
    _lv = [-1, -1, -1, -1, -1, -1]
    _lv_before_battle = [-1, -1, -1, -1, -1, -1]

    @property
    def lv(self):
        """
        Returns:
            list[int]: Список уровней по позициям.
        """
        return self._lv

    @lv.setter
    def lv(self, value):
        """
        Args:
            value (list[int]): Список уровней по позициям.
        """
        self._lv = value

    def lv_reset(self):
        """Сбрасывает данные уровней при входе на карту."""
        self._lv = [-1] * 6
        self._lv_before_battle = [-1] * 6

    @Config.when(SERVER='en')
    def _lv_grid(self):
        return ButtonGrid(origin=(56, 113), delta=(0, 100), button_shape=(46, 19), grid_shape=(1, 6))

    @Config.when(SERVER='jp')
    def _lv_grid(self):
        return ButtonGrid(origin=(34, 128), delta=(0, 100), button_shape=(68, 19), grid_shape=(1, 6))

    @Config.when(SERVER=None)
    def _lv_grid(self):
        return ButtonGrid(origin=(58, 128), delta=(0, 100), button_shape=(46, 19), grid_shape=(1, 6))

    def lv_get(self, after_battle=False):
        """Получает уровни кораблей на всех позициях.

        Args:
            after_battle (bool): Вызывается ли метод после боя.

        Returns:
            list[int]: Список уровней по позициям.
        """
        if not self.config.StopCondition_ReachLevel and not self.config.STOP_IF_REACH_LV32:
            return [-1] * 6

        self._lv_before_battle = self.lv if after_battle else [-1] * 6

        ocr = LevelOcr(self._lv_grid().buttons, name='LevelOcr')
        self.lv = ocr.ocr(self.device.image)
        logger.attr('Уровень', ', '.join(str(data) for data in self.lv))

        if after_battle:
            self.lv_triggered()
            self.lv32_triggered()

        return self.lv

    def lv_triggered(self):
        limit = self.config.StopCondition_ReachLevel
        if not limit:
            return False

        for i in range(6):
            before, after = self._lv_before_battle[i], self.lv[i]
            if after > before > 0:
                logger.info(f'[Уровень — проверка] Позиция {i}: ур. {before} -> ур. {after}')
            if after >= limit > before > 0:
                if after - before == 1 or after < 35:
                    logger.info(f'[Уровень — проверка] На позиции {i} достигнут ур. {limit}.')
                    self.config.LV_TRIGGERED = True
                    return True
                else:
                    logger.warning(f'[Уровень — проверка] Слишком большая разница уровней между {before} и {after}. '
                                   f'Это не считается выполнением условия')

        return False

    def lv32_triggered(self):
        if not self.config.STOP_IF_REACH_LV32:
            return False

        if self.lv[0] >= 32:
            logger.info('[Уровень — проверка] На позиции 0 достигнут ур. 32')
            self.config.LV32_TRIGGERED = True
            return True

        return False


class LevelOcr(Digit):
    def pre_process(self, image):
        # Проверяем максимум красного канала, чтобы определить, наложена ли на изображение маска.
        # При наложенной маске максимум красного канала не превышает COLOR_MASKED[0]=107.
        # Сначала обрезаем изображение, чтобы убрать значок «требуется ремонт» и сохранить верхнюю половину символа 'V'.
        max_red = image[:8, :, 0].max()
        if max_red <= COLOR_MASKED[0]:
            # Маска корабля с низким HP преобразует COLOR_WHITE=(255, 255, 255) в COLOR_MASKED=(107, 105, 107)
            # Восстанавливаем все каналы умножением на скаляр.
            scalar = np.mean(COLOR_WHITE) / np.mean(COLOR_MASKED)
            image = cv2.addWeighted(image, scalar, image, 0, 0)

        # Перед переводом в оттенки серого обрабатываем синий фон символов.
        # Фон полупрозрачный: (0, 0, 0) превращается в (33, 65, 115), а (255, 255, 255) — в (107, 138, 189).
        # Используем среднюю точку (70, 102, 152).
        bg = (70, 102, 152)
        # Преобразование яркости BT.601
        luma_trans = (0.299, 0.587, 0.114)
        luma_bg = np.dot(bg, luma_trans)
        image = cv2.subtract(image, bg).dot(luma_trans).round().astype(np.uint8)
        image = cv2.subtract(255, cv2.multiply(image, 255 / (255 - luma_bg)))
        # Ищем 'L', чтобы удалить префикс 'LV.'. Если 'L' не найден, возвращаем пустое изображение.
        if server.server != 'jp':
            letter_l = np.nonzero(image[9:15, :].max(axis=0) < 127)[0]
            if len(letter_l):
                first_digit = letter_l[0] + 17
                if first_digit + 3 < 46:  # LV_GRID_MAIN.button_shape[0] = 46
                    return image[:, first_digit:]
        else:
            letter_l = np.nonzero(image[5:11, :].max(axis=0) < 63)[0]
            if len(letter_l):
                first_digit = letter_l[0] + 23  # Максимальный размер в доке, минимальный — в сетке области
                if first_digit + 3 < 70:  # LV_GRID_MAIN.button_shape[0] = 46
                    image = image[:, first_digit:]
                    image = cv2.copyMakeBorder(image, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=(255, 255, 255))
                    return image
        return np.array([[255]], dtype=np.uint8)

    def after_process(self, result):
        result = result.replace('I', '1').replace('D', '0').replace('S', '5')
        result = result.replace('B', '8')

        # Не логируем исправления, поскольку значения уровней часто пустые
        # Например: [23, 0, 0, 100, 0, 0]
        result = int(result) if result else 0

        return result
