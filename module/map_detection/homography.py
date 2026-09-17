"""Модуль гомографического преобразования. Вычисляет матрицу гомографии между снимком экрана и шаблоном карты по ключевым точкам,
используется для определения положения снимка на карте и перспективных преобразований."""

import time

import numpy as np
from PIL import ImageDraw, ImageOps

from module.base.utils import *
from module.config.config import AzurLaneConfig
from module.exception import MapDetectionError
from module.logger import logger
from module.map_detection.perspective import Perspective
from module.map_detection.utils import *
from module.map_detection.utils_assets import *


class Homography:
    """Гомографическое преобразование.

    Examples:
        hm = Homography(AzurLaneConfig('template'))
        hm.load(image)

    Examples:
        hm = Homography(AzurLaneConfig('template'))
        storage = ((8, 3), [(80.773, 281.635), (1164.829, 281.635), (-20.123, 609.332), (1259.794, 609.332)])
        hm.load_homography(storage=storage)
        hm.detect(image)

    Logs:
                   tile_center: 0.968 (good match)
        0.062s  _   edge_lines: 3 hori, 3 vert
        Edges: /_    homo_loca: ( 26,  58)
    """

    """
    Выходные данные
    """
    image: np.ndarray
    config: AzurLaneConfig
    # Четыре линии краёв: bool или объект с атрибутом __bool__
    left_edge: int
    right_edge: int
    lower_edge: int
    upper_edge: int

    """
    Приватные атрибуты
    """
    homo_storage: tuple
    homo_data: np.ndarray
    homo_invt: np.ndarray
    homo_size: tuple
    homo_loca: np.ndarray
    homo_loaded: bool

    map_inner: np.ndarray
    _map_edge_count: tuple

    def __init__(self, config):
        """
        Args:
            config (AzurLaneConfig): Объект конфигурации.
        """
        self.config = config
        self.homo_loaded = False

    def _log_detection(self, message):
        if self.config.Scheduler_Command.startswith('Opsi'):
            logger.debug(message)
        else:
            logger.info(message)

    def _log_detection_attr(self, name, text):
        if self.config.Scheduler_Command.startswith('Opsi'):
            logger.debug(f'[Карта — гомография] {name}: {text}')
        else:
            logger.attr_align(name, text)

    @cached_property
    def ui_mask_homo_stroke(self):
        if self.config.Scheduler_Command.startswith('Opsi'):
            mask = ASSETS.ui_mask_os
        else:
            mask = ASSETS.ui_mask
        image = cv2.warpPerspective(mask, self.homo_data, self.homo_size)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        image = cv2.erode(image, kernel).astype('uint8')
        # Удаляем края: перспективное преобразование может создавать ступенчатые артефакты
        pad = 2
        image[:pad, :] = 0
        image[-pad:, :] = 0
        image[:, :pad] = 0
        image[:, -pad:] = 0
        return image

    def load(self, image):
        """Загрузить изображение и выполнить распознавание.

        Args:
            image (np.ndarray): Снимок экрана, форма (720, 1280, 3).
        """
        if not self.homo_loaded:
            self.load_homography(storage=self.config.HOMO_STORAGE, image=image)

        self.detect(image)

    def load_homography(self, storage=None, perspective=None, image=None, file=None):
        """Загрузить параметры гомографического преобразования из различных источников.

        Args:
            storage (tuple): Формат хранения ((x, y), [левый верхний, правый верхний, левый нижний, правый нижний]).
            perspective (Perspective): Экземпляр детектора перспективы.
            image (np.ndarray): Снимок экрана.
            file (str): Путь к файлу изображения.
        """
        if storage is not None:
            self.find_homography(*storage)
        elif perspective is not None:
            hori = perspective.horizontal[0].add(perspective.horizontal[-1])
            vert = perspective.vertical[0].add(perspective.vertical[-1])
            src_pts = hori.cross(vert).points
            x = len(perspective.vertical) - 1
            y = len(perspective.horizontal) - 1
            self.find_homography(size=(x, y), src_pts=src_pts)
        elif image is not None:
            perspective_ = Perspective(self.config)
            perspective_.load(image)
            self.load_homography(perspective=perspective_)
        elif file is not None:
            image_ = load_image(file)
            perspective_ = Perspective(self.config)
            perspective_.load(image_)
            self.load_homography(perspective=perspective_)
        else:
            raise MapDetectionError('Для load_homography не переданы данные; укажите хотя бы один набор.')

    def find_homography(self, size, src_pts, overflow=True):
        """Вычислить матрицу гомографического преобразования.

        Args:
            size (tuple): Размер сетки (x, y).
            src_pts (list[tuple]): Исходные угловые точки [левый верхний, правый верхний, левый нижний, правый нижний].
            overflow (bool): True — получить полное преобразованное изображение, False — только эффективную область.
        """
        self.homo_storage = (size, [(x, y) for x, y in np.round(src_pts, 3)])
        logger.attr('Сохранение гомографии', self.homo_storage)

        # Формируем данные перспективного преобразования
        src_pts = np.array(src_pts) - self.config.DETECTING_AREA[:2]
        dst_pts = src_pts[0] + area2corner((0, 0, *np.multiply(size, self.config.HOMO_TILE)))
        homo = cv2.getPerspectiveTransform(src_pts.astype(np.float32), dst_pts.astype(np.float32))

        # Пересоздаём преобразование, чтобы выровнять изображение по левому верхнему углу
        area = area2corner(self.config.DETECTING_AREA) - self.config.DETECTING_AREA[:2]
        transformed = perspective_transform(area, data=homo)
        if overflow:
            transformed -= np.min(transformed, axis=0)
            size = np.ceil(np.max(transformed, axis=0)).astype(int)
        else:
            x0, y0, x1, y1, x2, y2, x3, y3 = transformed.flatten()
            inner = np.array((max(x0, x2), max(y0, y1), min(x1, x3), min(y2, y3)))
            transformed -= inner[:2]
            size = np.ceil(inner[2:] - inner[:2]).astype(int)
        homo = cv2.getPerspectiveTransform(area.astype(np.float32), transformed.astype(np.float32))

        self.homo_data = homo
        self.homo_invt = cv2.invert(homo)[1]
        self.homo_size = tuple(size.tolist())
        self.homo_loaded = True

    def detect(self, image):
        """Выполнить распознавание гомографии на снимке экрана.

        Args:
            image (np.ndarray): Снимок экрана.

        Returns:
            bool: Успешно ли выполнено распознавание.
        """
        start_time = time.time()
        self.image = image

        # Инициализация изображения
        image = rgb2gray(crop(image, self.config.DETECTING_AREA, copy=False))

        # Перспективное преобразование
        image_trans = cv2.warpPerspective(image, self.homo_data, self.homo_size)

        # Обнаружение краёв
        image_edge = cv2.Canny(image_trans, *self.config.HOMO_CANNY_THRESHOLD)
        cv2.bitwise_and(image_edge, self.ui_mask_homo_stroke, dst=image_edge)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        cv2.morphologyEx(image_edge, cv2.MORPH_CLOSE, kernel, dst=image_edge)
        # Image.fromarray(image_edge, mode='L').show()

        # Ищем свободную клетку
        if self.search_tile_center(image_edge, threshold_good=self.config.HOMO_CENTER_GOOD_THRESHOLD,
                                   threshold=self.config.HOMO_CENTER_THRESHOLD):
            pass
        elif self.search_tile_corner(image_edge, threshold=self.config.HOMO_CORNER_THRESHOLD):
            pass
        elif self.search_tile_rectangle(image_edge, threshold=self.config.HOMO_RECTANGLE_THRESHOLD):
            pass
        else:
            raise MapDetectionError('Не удалось найти свободную клетку')

        self.homo_loca %= self.config.HOMO_TILE

        # Обнаруживаем края карты
        self.lower_edge, self.upper_edge, self.left_edge, self.right_edge = False, False, False, False
        self._map_edge_count = (0, 0)
        if self.config.HOMO_EDGE_DETECT:
            # image_edge = cv2.bitwise_and(cv2.dilate(image_edge, kernel),
            #                              cv2.inRange(image_trans, *self.config.HOMO_EDGE_COLOR_RANGE))
            # image_edge = cv2.bitwise_and(image_edge, self.ui_mask_homo_stroke)
            cv2.dilate(image_edge, kernel, dst=image_edge)
            cv2.inRange(image_trans, *self.config.HOMO_EDGE_COLOR_RANGE, dst=image_trans)
            cv2.bitwise_and(image_edge, image_trans, dst=image_edge)
            cv2.bitwise_and(image_edge, self.ui_mask_homo_stroke, dst=image_edge)
            self.detect_edges(image_edge, hough_th=self.config.HOMO_EDGE_HOUGHLINES_THRESHOLD)

        # Вывод журнала
        time_cost = round(time.time() - start_time, 3)
        self._log_detection('[Карта — гомография] %s с  %s   Линии краёв: %s горизонтальных, %s вертикальных' % (
            float2str(time_cost), '_' if self.lower_edge else ' ',
            self._map_edge_count[1], self._map_edge_count[0])
                    )
        self._log_detection('[Карта — гомография] Края: %s%s%s   Позиция гомографии: %s' % (
            '/' if self.left_edge else ' ', '_' if self.upper_edge else ' ', '\\' if self.right_edge else ' ',
            point2str(*self.homo_loca, length=3))
                    )

    def search_tile_center(self, image, threshold_good=0.9, threshold=0.8, encourage=1.0):
        """Найти центральное положение свободных тайлов.
        Примечание: это основной метод.
        `len(res[res > 0.8])` в 3 раза быстрее `np.sum(res > 0.8)`.

        Args:
            image (np.ndarray): Полутоновое изображение (градации серого).
            threshold_good (float): Порог хорошего совпадения.
            threshold (float): Порог совпадения.
            encourage (int, float): Вес поощрения подгонки.

        Returns:
            bool: Успешен ли поиск.
        """
        threshold_good = lower_template_match_similarity(threshold_good)
        threshold = lower_template_match_similarity(threshold)
        result = cv2.matchTemplate(image, ASSETS.tile_center_image, cv2.TM_CCOEFF_NORMED)
        _, similarity, _, loca = cv2.minMaxLoc(result)
        if similarity > threshold_good:
            self.homo_loca = np.array(loca) - self.config.HOMO_CENTER_OFFSET
            self.map_inner = np.array(loca)
            message = 'good match'
        elif similarity > threshold:
            location = np.argwhere(result > threshold)[:, ::-1]
            self.homo_loca = fit_points(
                location, mod=self.config.HOMO_TILE, encourage=encourage) - self.config.HOMO_CENTER_OFFSET
            self.map_inner = get_map_inner(location)
            message = f'{len(location)} matches'
        else:
            message = 'bad match'

        # print(self.homo_loca % self.config.HOMO_TILE)
        self._log_detection_attr('Центры клеток', f'{float2str(similarity)} ({message})')
        return message != 'bad match'

    def search_tile_corner(self, image, threshold=0.8, encourage=1.0):
        """Найти угловые положения свободных тайлов.
        Это резервный метод, используется крайне редко.
        Примечание: метод даёт погрешность около 0.5–1.0 пикселя.

        Args:
            image (np.ndarray): Полутоновое изображение (градации серого).
            threshold (float): Порог совпадения.
            encourage (int, float): Вес поощрения подгонки.

        Returns:
            bool: Успешен ли поиск.
        """
        threshold = lower_template_match_similarity(threshold)
        similarity = 0
        location = np.array([])
        for index in range(4):
            template = ASSETS.tile_corner_image_list[index]
            result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
            similarity = max(similarity, np.max(result))
            loca = np.argwhere(result > threshold)[:, ::-1] - self.config.HOMO_CORNER_OFFSET_LIST[index]
            location = np.append(location, loca, axis=0) if len(location) else loca

        if similarity > threshold:
            self.homo_loca = fit_points(
                location, mod=self.config.HOMO_TILE, encourage=encourage) - self.config.HOMO_CENTER_OFFSET
            self.map_inner = get_map_inner(location)
            message = f'{len(location)} matches'
        else:
            message = 'bad match'

        # print(self.homo_loca % self.config.HOMO_TILE)
        self._log_detection_attr('Углы клеток', f'{float2str(similarity)} ({message})')
        return message != 'bad match'

    def search_tile_rectangle(self, image, threshold=10, encourage=5.1, close_kernel=(5, 10, 15, 20, 25)):
        """Найти прямоугольные положения свободных тайлов.
        Это резервный метод для резервного метода, практически никогда не требуется.
        Примечание: метод может иметь погрешность около 2 пикселей.

        Args:
            image (np.ndarray): Полутоновое изображение (градации серого).
            threshold (int): Порог количества прямоугольников.
            encourage (int, float): Вес поощрения подгонки.
            close_kernel (tuple[int]): Размер ядра для морфологического замыкания.

        Returns:
            bool: Успешен ли поиск.
        """
        location = np.array([])
        for kernel in close_kernel:
            # Заново создаём изображение после операции закрытия
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
            image_closed = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel)
            # Ищем прямоугольники
            contours, _ = cv2.findContours(image_closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            rectangle = np.array([cv2.boundingRect(cv2.convexHull(cont).astype(np.float32)) for cont in contours])

            try:
                # Отбираем подходящие прямоугольники
                rectangle = rectangle[(rectangle[:, 2] > 100) & (rectangle[:, 3] > 100)]
                shape = rectangle[:, 2:]
                diff = np.abs(shape - np.round(shape / self.config.HOMO_TILE) * self.config.HOMO_TILE)
                rectangle = rectangle[np.all(diff < encourage, axis=1)]
                location = np.append(location, rectangle[:, :2], axis=0) if len(location) else rectangle[:, :2]
            except IndexError:
                location = []

        if len(location) > threshold:
            self.homo_loca = fit_points(location, mod=self.config.HOMO_TILE, encourage=encourage)
            self.map_inner = get_map_inner(location)
            message = 'good match'
        else:
            message = 'bad match'

        # print(self.homo_loca % self.config.HOMO_TILE)
        self._log_detection_attr('Прямоугольники клеток', f'{len(location)} прямоугольников ({message})')
        return message != 'bad match'

    def detect_edges(self, image, hough_th=120, theta_th=0.005, edge_th=9):
        """Распознать границы карты.

        Args:
            image (np.ndarray): Полутоновое изображение (градации серого).
            hough_th (int): Порог cv2.HoughLines.
            theta_th (float): Угловой порог отрезка, в градусах.
            edge_th (int): Порог границы, в пикселях.
        """
        lines = cv2.HoughLines(image, 1, np.pi / 180, hough_th)
        if lines is None:
            self.lower_edge, self.upper_edge = separate_edges([], inner=self.map_inner[1])
            self.left_edge, self.right_edge = separate_edges([], inner=self.map_inner[0])
            self._map_edge_count = (0, 0)
            return None

        lines = lines[:, 0, :]
        rho, theta = lines[:, 0], lines[:, 1]
        area = self.config.DETECTING_AREA
        area = area2corner([0, 0, *np.subtract(area[2:], area[:2])])
        area = np.mean(area.reshape((2, 2, 2)), axis=0)
        area = perspective_transform(area, self.homo_data)
        mid_left, _, mid_right, _ = area.flatten()

        hori = lines[(np.deg2rad(90 - theta_th) < theta) & (theta < np.deg2rad(90 + theta_th))]
        hori = Lines(hori, is_horizontal=True).group()
        vert = lines[(theta < np.deg2rad(theta_th)) | (np.deg2rad(180 - theta_th) < theta)]
        vert = [[-rho, theta - np.pi] if rho < 0 else [rho, theta] for rho, theta in vert]
        vert = [[rho, theta] for rho, theta in vert if mid_left < rho < mid_right]
        vert = Lines(vert, is_horizontal=False).group()

        self._map_edge_count = (len(vert), len(hori))

        if hori:
            hori = hori.rho
            diff = (hori - self.homo_loca[1]) % self.config.HOMO_TILE[1]
            hori = hori[(diff < edge_th) | (diff > self.config.HOMO_TILE[1] - edge_th)]
        if vert:
            vert = vert.rho
            diff = (vert - self.homo_loca[0]) % self.config.HOMO_TILE[0]
            vert = vert[(diff < edge_th) | (diff > self.config.HOMO_TILE[0] - edge_th)]

        self.lower_edge, self.upper_edge = separate_edges(hori, inner=self.map_inner[1])
        self.left_edge, self.right_edge = separate_edges(vert, inner=self.map_inner[0])

    def generate(self, edge_th=9):
        """Сгенерировать координаты сетки и соответствующие четыре угловые точки.

        Yields:
            tuple: ((x, y), [левый верхний, правый верхний, левый нижний, правый нижний]).
        """
        area = [
            self.left_edge - edge_th if self.left_edge else 0,
            self.lower_edge - edge_th if self.lower_edge else 0,
            self.right_edge + edge_th if self.right_edge else self.homo_size[0],
            self.upper_edge + edge_th if self.upper_edge else self.homo_size[1]
        ]
        x = np.arange(-25, 25) * self.config.HOMO_TILE[0] + self.homo_loca[0]
        x = x[(x > area[0]) & (x < area[2])]
        y = np.arange(-25, 25) * self.config.HOMO_TILE[1] + self.homo_loca[1]
        y = y[(y > area[1]) & (y < area[3])]

        shape = (len(x), len(y))
        points = np.array(np.meshgrid(x, y)).reshape((2, -1)).T
        points = perspective_transform(points, data=self.homo_invt) + self.config.DETECTING_AREA[:2]
        for data in points_to_area_generator(points.reshape(*shape[::-1], 2), shape=shape):
            yield data

    def to_perspective(self):
        """Преобразовать результат гомографического преобразования в перспективные отрезки.

        Returns:
            (Lines, Lines): Горизонтальные и вертикальные отрезки.
        """
        grids = {}
        for loca, points in self.generate():
            grids[loca] = points
        shape = np.max(list(grids.keys()), axis=0)

        hori = Points([640, grids[(0, 0)][1, 1]]).link(None, is_horizontal=True)
        for y in range(shape[1] + 1):
            hori = hori.add(Points([640, grids[(0, y)][3, 1]]).link(None, is_horizontal=True))
        vert = Points(grids[(0, 0)][0]).link(grids[(0, shape[1])][2])
        for x in range(shape[0] + 1):
            vert = vert.add(Points(grids[(x, 0)][1]).link(grids[(x, shape[1])][3]))
        return hori, vert

    def draw(self, lines=None, bg=None, expend=0):
        if lines is None:
            hori, vert = self.to_perspective()
            lines = hori.add(vert)
        if bg is None:
            image = self.image.copy()
        else:
            image = bg.copy()
        image = Image.fromarray(image)
        if expend:
            image = ImageOps.expand(image, border=expend, fill=0)
        draw = ImageDraw.Draw(image)
        for rho, theta in zip(lines.rho, lines.theta):
            a = np.cos(theta)
            b = np.sin(theta)
            x0 = a * rho
            y0 = b * rho
            x1 = int(x0 + 10000 * (-b)) + expend
            y1 = int(y0 + 10000 * a) + expend
            x2 = int(x0 - 10000 * (-b)) + expend
            y2 = int(y0 - 10000 * a) + expend
            draw.line([x1, y1, x2, y2], 'white')

        image.show()
