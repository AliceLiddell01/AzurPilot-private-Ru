"""Модуль обзора карты. Определяет класс View, наследующий MapDetector, управляющий совокупностью
всех ячеек карты, предоставляющий выборку ячеек, обход, вычисление координат флота и смещений свайпа."""

import collections
import time

from module.base.utils import *
from module.exception import MapDetectionError
from module.logger import logger
from module.map.map_grids import SelectedGrids
from module.map_detection.detector import MapDetector
from module.map_detection.grid import Grid
from module.map_detection.utils import *
from module.map_detection.utils_assets import *


class View(MapDetector):
    grids: dict
    shape: np.ndarray
    center_loca: tuple
    center_offset: np.ndarray
    swipe_base: np.ndarray

    def __init__(self, config, mode='main', grid_class=Grid):
        """
        Args:
            config (AzurLaneConfig): Объект конфигурации.
            mode (str): 'main' — обычная карта Azur Lane, 'os' — Operation Siren.
            grid_class: Класс ячейки сетки.
        """
        super().__init__(config)
        self.mode = mode
        self.grid_class = grid_class

    def __iter__(self):
        return iter(self.grids.values())

    def __getitem__(self, item):
        return self.grids[tuple(item)]

    def __contains__(self, item):
        return tuple(item) in self.grids

    def _log_detection(self, message):
        logger.debug(message)

    def show(self):
        for y in range(self.shape[1] + 1):
            text = ' '.join([self[(x, y)].str if (x, y) in self else '..' for x in range(self.shape[0] + 1)])
            self._log_detection(text)

    def _image_clear_ui(self, image):
        if self.mode == 'os':
            return cv2.copyTo(image, ASSETS.ui_mask_os_in_map)
        else:
            return cv2.copyTo(image, ASSETS.ui_mask_in_map)

    def load(self, image):
        """Загрузить изображение и построить локальную карту поля зрения.

        Args:
            image: Снимок экрана.
        """
        image = self._image_clear_ui(np.array(image))
        self.image = image
        super().load(image)

        # Создаём карту локального поля зрения.
        grids = {}

        for loca, points in self.generate():
            if area_in_area(area1=corner2area(points), area2=self.config.DETECTING_AREA):
                grids[loca] = self.grid_class(location=loca, image=image, corner=points, config=self.config)

        # Обрабатываем смещение сетки.
        offset = list(grids.keys())
        if not len(offset):
            raise MapDetectionError('Клетки карты не найдены')
        offset = np.min(offset, axis=0)
        if np.sum(np.abs(offset)) > 0:
            self._log_detection(f'[Распознавание карты — обзор] Смещение сетки: {tuple(offset.tolist())}')
            self.grids = {}
            for loca, grid in grids.items():
                x, y = np.subtract(loca, offset)
                grid.location = (x, y)
                self.grids[(x, y)] = grid
        else:
            self.grids = grids
        self.shape = np.max(list(self.grids.keys()), axis=0)

        # Ищем центр локального поля зрения.
        for loca, grid in self.grids.items():
            offset = grid.screen2grid([self.config.SCREEN_CENTER])[0].astype(int)
            points = grid.grid2screen(np.add([[0.5, 0], [-0.5, 0], [0, 0.5], [0, -0.5]], offset))
            self.swipe_base = np.array([np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3])])
            self.center_loca = tuple(np.add(loca, offset).tolist())
            self._log_detection(f'[Распознавание карты — обзор] Центр обзора: {self.center_loca}')
            if self.center_loca in self:
                self.center_offset = self.grids[self.center_loca].screen2grid([self.config.SCREEN_CENTER])[0]
            else:
                x = max(self.center_loca[0] - self.shape[0], 0) if self.center_loca[0] > 0 else self.center_loca[0]
                y = max(self.center_loca[1] - self.shape[1], 0) if self.center_loca[1] > 0 else self.center_loca[1]
                self.center_offset = offset - self.center_loca
                raise MapDetectionError(f'Camera outside map: смещение=({x}, {y})')
            break

    def predict(self):
        """Распознать информацию обо всех ячейках сетки."""
        start_time = time.time()
        for grid in self:
            grid.predict()
        time_cost = float2str(time.time() - start_time)
        self._log_detection(
            f'[Распознавание карты — обзор] Распознано клеток: {len(self.grids.keys())} (время {time_cost} с)'
        )

    def update(self, image):
        """Обновить изображение всех ячеек сетки.
        Если положение камеры не изменилось, пересчёт не требуется, достаточно обновить изображение.
        """
        image = self._image_clear_ui(image)
        self.image = image
        for grid in self:
            grid.reset()
            grid.image = image

    def select(self, **kwargs):
        """Отфильтровать ячейки сетки по заданным свойствам.

        Args:
            **kwargs: Пары ключ-значение атрибутов ячейки.

        Returns:
            SelectedGrids: Набор ячеек, удовлетворяющих условиям.
        """
        result = []
        for grid in self:
            flag = True
            for k, v in kwargs.items():
                if grid.__getattribute__(k) != v:
                    flag = False
            if flag:
                result.append(grid)

        return SelectedGrids(result)

    def predict_swipe(self, prev, with_current_fleet=True, with_sea_grids=True):
        """Спрогнозировать величину смещения свайпа.

        Args:
            prev (View): Экземпляр View до свайпа.
            with_current_fleet (bool): Использовать ли зелёную стрелку текущего флота для прогнозирования.
            with_sea_grids (bool): Использовать ли все морские ячейки для прогнозирования.
                Обратите внимание: данный метод имеет некоторую погрешность.

        Returns:
            tuple[int]: Смещение (x, y). Возвращает None, если прогноз невозможен.

        Log:
            Map swipe predict: (2, 0) (0.023s, текущий флот совпал)
        """
        start_time = time.time()
        offset = np.subtract(self.center_loca, prev.center_loca)

        if with_current_fleet:
            for grid in self:
                grid.is_fleet = grid.predict_fleet()
                grid.is_current_fleet = grid.predict_current_fleet()
            for grid in prev:
                grid.is_fleet = grid.predict_fleet()
                grid.is_current_fleet = grid.predict_current_fleet()

            # Если текущий флот найден, используем его для прогнозирования свайпа.
            current_fleet = self.select(is_fleet=True, is_current_fleet=True)
            previous_fleet = prev.select(is_fleet=True, is_current_fleet=True)
            if len(current_fleet) == 1 and len(previous_fleet) == 1:
                diff = np.subtract(current_fleet[0].location, previous_fleet[0].location) - offset
                # print(current_fleet[0].location, previous_fleet[0].location, offset, diff)
                diff = tuple(diff.tolist())
                self._log_detection(
                    f'[Распознавание карты — обзор] Прогноз сдвига карты: {diff} '
                    f'({float2str(time.time() - start_time) + "s"}, совпадение текущего флота)'
                )
                return diff

        if with_sea_grids:
            # Перебором ищем смещение свайпа.
            swipes = []
            for current_loca, current_piece in self.grids.items():
                for previous_loca, previous_piece in prev.grids.items():
                    if current_piece.is_similar_to(previous_piece):
                        diff = np.subtract(current_loca, previous_loca) - offset
                        swipes.append(tuple(diff.tolist()))
                        # print(current_loca, previous_loca, offset, diff)

            counter = collections.Counter(swipes)
            diff = counter.most_common()
            # print(diff)
            if len(diff) == 1 \
                    or len(diff) >= 2 and diff[0][1] > diff[1][1]:
                self._log_detection(
                    f'[Распознавание карты — обзор] Прогноз сдвига карты: {diff[0][0]} '
                    f'({float2str(time.time() - start_time) + "s"}, совпадений: {diff[0][1]})'
                )
                return diff[0][0]

        # Не удалось спрогнозировать.
        self._log_detection(
            f'[Распознавание карты — обзор] Прогноз сдвига карты отсутствует '
            f'({float2str(time.time() - start_time) + "s"}, совпадений нет)'
        )
        return None
