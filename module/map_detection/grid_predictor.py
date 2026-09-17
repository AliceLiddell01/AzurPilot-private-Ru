"""Модуль распознавания ячеек сетки. Определяет класс GridPredictor, выполняющий распознавание содержимого ячеек по снимку экрана и координатам четырёх углов,
включая обнаружение врагов, флота, сопоставление ассетов и т.д."""

from module.base.utils import *
from module.config.config import AzurLaneConfig
from module.exception import ScriptError
from module.logger import logger
from module.map_detection.utils import *
from module.map_detection.utils_assets import *
from module.template.assets import *


class GridPredictor:
    def __init__(self, location, image, corner, config):
        """Инициализировать распознаватель ячеек сетки.

        Args:
            location (tuple): Координаты ячейки, (x, y).
            image (np.ndarray): Снимок экрана, форма (720, 1280, 3).
            corner (np.ndarray): Координаты четырёх углов, форма (4, 2), [левый верхний, правый верхний, левый нижний, правый нижний].
            config (AzurLaneConfig): Объект конфигурации.
        """
        self.location = location
        self.image = image
        self.corner = corner
        self.config = config

        # Прямое вычисление быстрее вызова существующих функций.
        x0, y0, x1, y1, x2, y2, x3, y3 = corner.flatten()
        divisor = x0 - x1 + x2 - x3
        x = (x0 * x2 - x1 * x3) / divisor
        y = (x0 * y2 - x1 * y2 + x2 * y0 - x3 * y0) / divisor
        self._image_center = np.array([x, y, x, y])
        self._image_a = (-x0 * x2 + x0 * x3 + x1 * x2 - x1 * x3) / divisor * self.config.GRID_IMAGE_A_MULTIPLY

        self.template_enemy_genre = {}
        for name in self.config.MAP_ENEMY_TEMPLATE:
            self.template_enemy_genre[name] = globals().get(f'TEMPLATE_ENEMY_{name}')
        if self.config.MAP_HAS_SIREN:
            for name in self.config.MAP_SIREN_TEMPLATE:
                self.template_enemy_genre[f'Siren_{name}'] = globals().get(f'TEMPLATE_SIREN_{name}')

        self.area = corner2area(self.corner)
        self.homo_data = cv2.getPerspectiveTransform(
            src=self.corner.astype(np.float32),
            dst=area2corner((0, 0, *self.config.HOMO_TILE)).astype(np.float32))
        self.homo_invt = cv2.invert(self.homo_data)[1]

    def screen2grid(self, points):
        """Преобразовать экранные координаты в координаты сетки морской поверхности.

        Args:
            points (np.ndarray): Экранные координаты, [[x1, y1], [x2, y2], ...].

        Returns:
            np.ndarray: Координаты сетки морской поверхности, [[x1, y1], [x2, y2], ...].
                Начало координат в левом верхнем углу.
            (0, 0) +------+
                   |      |
                   |      |
                   +------+ (1, 1)
        """
        return perspective_transform(points, self.homo_data) / self.config.HOMO_TILE

    def grid2screen(self, points):
        """Преобразовать координаты сетки морской поверхности в экранные координаты.

        Args:
            points (np.ndarray): Координаты сетки морской поверхности, [[x1, y1], [x2, y2], ...].
                См. screen2grid().

        Returns:
            np.ndarray: Экранные координаты, [[x1, y1], [x2, y2], ...].
        """
        return perspective_transform(np.multiply(points, self.config.HOMO_TILE), self.homo_invt)

    @cached_property
    def image_trans(self):
        return cv2.warpPerspective(self.image, self.homo_data, self.config.HOMO_TILE)

    @cached_property
    def image_homo(self):
        image_edge = rgb2gray(self.image_trans)
        cv2.Canny(image_edge, 100, 150, dst=image_edge)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        cv2.morphologyEx(image_edge, cv2.MORPH_CLOSE, kernel, dst=image_edge)
        return image_edge

    def predict(self):
        self.enemy_scale = self.predict_enemy_scale()
        self.enemy_genre = self.predict_enemy_genre()
        self.is_boss = self.predict_boss()
        self.is_submarine = self.predict_submarine()
        if self.is_submarine:
            self.is_fleet = False
        else:
            self.is_fleet = self.predict_fleet()
        if self.config.MAP_HAS_MYSTERY:
            self.is_mystery = self.predict_mystery()
        self.is_current_fleet = self.predict_current_fleet()
        # self.is_caught_by_siren = self.predict_caught_by_siren()

        if self.config.MAP_HAS_MISSILE_ATTACK:
            if self.predict_missile_attack():
                self.is_missile_attack = True
        if self.enemy_genre:
            self.is_enemy = True
        if self.enemy_scale:
            self.is_enemy = True
        # if not self.is_enemy:
        #     self.is_enemy = self.predict_static_red_border()
        if self.is_enemy and not self.enemy_genre:
            self.enemy_genre = 'Enemy'
        if self.config.MAP_HAS_SIREN:
            if self.enemy_genre is not None and self.enemy_genre.startswith('Siren'):
                self.is_siren = True
                self.enemy_scale = 0

    def relative_crop(self, area, shape=None):
        """Обрезать изображение и масштабировать к целевому размеру, устраняя перспективные искажения.

        Args:
            area (tuple): Координаты относительной области (левый_x, верхний_y, правый_x, нижний_y), например (-1, -1, 1, 1).
            shape (tuple): Размер выходного изображения, (ширина, высота).

        Returns:
            np.ndarray: Форма (высота, ширина, каналы).
        """
        area = self._image_center + np.array(area) * self._image_a
        image = crop(self.image, area=np.rint(area).astype(int), copy=False)
        if shape is not None:
            # Используем стандартный фильтр ресемплинга Pillow — BICUBIC.
            image = cv2.resize(image, shape, interpolation=cv2.INTER_CUBIC)
        return image

    def relative_rgb_count(self, area, color, shape=(50, 50), threshold=221):
        """Подсчитать количество пикселей, соответствующих целевому RGB-цвету в относительной области.

        Args:
            area (tuple): Координаты относительной области (левый_x, верхний_y, правый_x, нижний_y), например (-1, -1, 1, 1).
            color (tuple): Целевой RGB-цвет.
            shape (tuple): Размер выходного изображения, (ширина, высота).
            threshold (int): Порог 0-255; чем выше, тем строже; 255 — полное совпадение.

        Returns:
            int: Количество совпавших пикселей.
        """
        mask = color_similarity_2d(self.relative_crop(area, shape=shape), color=color)
        cv2.inRange(mask, threshold, 255, dst=mask)
        count = cv2.countNonZero(mask)
        return count

    def relative_hsv_count(self, area, h=(0, 360), s=(0, 100), v=(0, 100), shape=(50, 50)):
        """Подсчитать количество пикселей, соответствующих целевому диапазону HSV-цвета в относительной области.

        Args:
            area (tuple): Координаты относительной области (левый_x, верхний_y, правый_x, нижний_y), например (-1, -1, 1, 1).
            h (tuple): Диапазон оттенка (Hue).
            s (tuple): Диапазон насыщенности (Saturation).
            v (tuple): Диапазон яркости (Value).
            shape (tuple): Размер выходного изображения, (ширина, высота).

        Returns:
            int: Количество совпавших пикселей.
        """
        image = self.relative_crop(area, shape=shape)
        cv2.cvtColor(image, cv2.COLOR_RGB2HSV, dst=image)
        lower = (h[0] / 2, s[0] * 2.55, v[0] * 2.55)
        upper = (h[1] / 2 + 1, s[1] * 2.55 + 1, v[1] * 2.55 + 1)
        # Не задаём `dst`: выходное изображение имеет форму (50, 50), а `image` — (50, 50, 3)
        image = cv2.inRange(image, lower, upper)
        count = cv2.countNonZero(image)
        return count

    def predict_enemy_scale(self):
        """Определить значок масштаба противника в левом верхнем углу: крупный, средний, малый.

        Returns:
            int: 1: малый, 2: средний, 3: крупный, 0: неизвестно.
        """
        image = self.relative_crop((-0.415 - 0.7, -0.62 - 0.7, -0.415, -0.62), shape=(50, 50))
        red = color_similarity_2d(image, (255, 130, 132))
        yellow = color_similarity_2d(image, (255, 235, 156))

        if TEMPLATE_ENEMY_L.match(red, similarity=0.75):
            scale = 3
        elif TEMPLATE_ENEMY_M.match(yellow):
            scale = 2
        elif TEMPLATE_ENEMY_S.match(yellow):
            scale = 1
        else:
            scale = 0

        return scale

    def predict_enemy_genre(self):
        if self.config.MAP_SIREN_HAS_BOSS_ICON:
            if self.enemy_scale:
                return ''
            image = self.relative_crop((-0.55, -0.2, 0.45, 0.2), shape=(50, 20))
            image = color_similarity_2d(image, color=(255, 150, 24))
            if image[image > 221].shape[0] > 200:
                if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.6):
                    return 'Siren_Siren'
        if self.config.MAP_SIREN_HAS_BOSS_ICON_SMALL:
            if self.relative_hsv_count(area=(0.03, -0.15, 0.63, 0.15), h=(32 - 3, 32 + 3), shape=(50, 20)) > 100:
                image = self.relative_crop((0.03, -0.15, 0.63, 0.15), shape=(50, 20))
                image = color_similarity_2d(image, color=(255, 150, 33))
                if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.7):
                    return 'Siren_Siren'

        image_dic = {}
        scaling_dic = self.config.MAP_ENEMY_GENRE_DETECTION_SCALING
        for name, template in self.template_enemy_genre.items():
            if template is None:
                logger.warning(f'[Распознавание карты] Шаблон распознавания противника не найден: {name}')
                logger.warning('[Распознавание карты] Создайте шаблон с помощью dev_tools/relative_record.py или dev_tools/relative_crop.py '
                               'и поместите его в каталог ./assets/<server>/template')
                logger.warning('[Распознавание карты] Шаблон распознавания элитного противника не найден. Обычно это означает, что карта события ещё не полностью поддерживается; дождитесь обновления AzurPilot.')
                raise ScriptError(f'Шаблон распознавания противника не найден: {name}')

            short_name = name[6:] if name.startswith('Siren_') else name
            scaling = scaling_dic.get(short_name, 1)
            scaling = (scaling,) if not isinstance(scaling, tuple) else scaling
            for scale in scaling:
                if scale not in image_dic:
                    shape = tuple(np.round(np.array((60, 60)) * scale).astype(int))
                    image_dic[scale] = rgb2gray(self.relative_crop((-0.5, -1, 0.5, 0), shape=shape))

                if template.match(image_dic[scale], similarity=self.config.MAP_ENEMY_GENRE_SIMILARITY):
                    return name

        return None

    def predict_boss(self):
        if self.enemy_genre == 'Siren_Siren':
            return False

        image = self.relative_crop((-0.55, -0.2, 0.45, 0.2), shape=(50, 20))
        image = color_similarity_2d(image, color=(255, 77, 82))
        if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.75):
            return True

        # Маленький значок Boss
        if self.relative_hsv_count(area=(0.03, -0.15, 0.63, 0.15), h=(358 - 3, 358 + 3), shape=(50, 20)) > 100:
            image = self.relative_crop((0.03, -0.15, 0.63, 0.15), shape=(50, 20))
            image = color_similarity_2d(image, color=(255, 77, 82))
            if TEMPLATE_ENEMY_BOSS.match(image, similarity=0.7):
                return True

        return False

    def predict_missile_attack(self):
        return self.relative_rgb_count(area=(-0.5, -1, 0.5, 0), color=(255, 255, 60), shape=(50, 50)) > 35

    def predict_fleet(self):
        image = self.relative_crop((-1, -2, -0.5, -1.5), shape=(50, 50))
        image = color_similarity_2d(image, color=(255, 255, 255))
        return TEMPLATE_FLEET_AMMO.match(image)

    def predict_submarine(self):
        image = self.relative_crop((-0.86, 0.08, -0.36, 0.58), shape=(50, 50))
        image = color_similarity_2d(image, color=(255, 243, 156))
        return TEMPLATE_SUBMARINE.match(image)

    def predict_caught_by_siren(self):
        image = self.relative_crop((-1, -1.5, 1, 0.5), shape=(120, 120))
        return TEMPLATE_CAUGHT_BY_SIREN.match(image, similarity=0.6)

    def predict_mystery(self):
        """Определить, является ли ячейка таинственным событием.

        Returns:
            bool: True, если это таинственное событие.
        """
        # Бирюзовый знак вопроса
        if self.relative_rgb_count(
                area=(-0.3, -2, 0.3, -0.6), color=(148, 255, 247), shape=(20, 50)) > 50:
            return True
        # Белый фон
        # if self.relative_rgb_count(
        #         area=(-0.7, -1.7, 0.7, -0.3), color=(239, 239, 239), shape=(50, 50)) > 700:
        #     return True

        return False

    def predict_current_fleet(self):
        count = self.relative_hsv_count(area=(-0.5, -3.5, 0.5, -2.5), h=(141 - 3, 141 + 10), shape=(50, 50))
        if count < 600:
            return False

        image = self.relative_crop((-0.5, -3.5, 0.5, -2.5), shape=(60, 60))
        image = color_similarity_2d(image, color=(24, 255, 107))
        if not TEMPLATE_FLEET_CURRENT.match(image):
            return False

        return True

    def predict_sea(self):
        area = area_pad((48, 48, 48 + 46, 48 + 46), pad=5)
        res = cv2.matchTemplate(ASSETS.tile_center_image, crop(self.image_homo, area=area, copy=False), cv2.TM_CCOEFF_NORMED)
        _, sim, _, _ = cv2.minMaxLoc(res)
        if sim > lower_template_match_similarity(0.8):
            return True

        tile = 135
        corner = 25
        corner = [(5, 5, corner, corner), (tile - corner, 5, tile, corner), (5, tile - corner, corner, tile),
                  (tile - corner, tile - corner, tile, tile)]
        for area, template in zip(corner[::-1], ASSETS.tile_corner_image_list[::-1]):
            res = cv2.matchTemplate(template, crop(self.image_homo, area=area, copy=False), cv2.TM_CCOEFF_NORMED)
            _, sim, _, _ = cv2.minMaxLoc(res)
            if sim > lower_template_match_similarity(0.8):
                return True

        return False

    def predict_submarine_move(self):
        # Определяем оранжевую стрелку в режиме перемещения подлодки.
        return self.relative_rgb_count((-0.5, -1, 0.5, 0), color=(231, 138, 49), shape=(60, 60)) > 200

    def predict_mob_move_icon(self):
        image = rgb2gray(self.relative_crop(area=(-0.5, -0.5, 0.5, 0.5), shape=(60, 60)))
        return TEMPLATE_MOB_MOVE_ICON.match(image)

    def predict_air_strike_icon(self):
        # area = area_pad((0, 0, 140, 140), pad=5)
        # image = color_similarity_2d(crop(self.image_trans, area=area, copy=False), color=(255, 255, 160))
        image = color_similarity_2d(self.image_trans, color=(255, 255, 160))
        cv2.threshold(image, 175, 255, cv2.THRESH_BINARY, dst=image)
        return TEMPLATE_AIR_STRIKE_ICON.match(image, similarity=0.7)

    @cached_property
    def _image_similar_piece(self):
        return rgb2gray(self.relative_crop(area=(-0.5, -0.5, 0.5, 0.5), shape=(60, 60)))

    @cached_property
    def _image_similar_full(self):
        return rgb2gray(self.relative_crop(area=(-0.6, -0.6, 0.6, 0.6), shape=(72, 72)))

    is_os: int

    @cached_property
    def is_in_detecting_area(self, area=(-0.5, -0.5, 0.5, 0.5)):
        area = self._image_center + np.array(area) * self._image_a
        area = area_offset(area, offset=DETECTING_AREA[:2])
        mask = UI_MASK_OS if self.is_os else UI_MASK
        color = cv2.mean(crop(mask.image, area=np.rint(area).astype(int), copy=False))
        return color[0] > 235

    def is_similar_to(self, grid, similarity=0.9):
        """Определить, похожа ли текущая ячейка на другую ячейку.

        Args:
            grid (GridPredictor): Экземпляр другой ячейки.
            similarity (float): Порог схожести, от 0 до 1.

        Returns:
            bool: Похожа ли текущая ячейка на другую.
        """
        if not self.is_in_detecting_area or not grid.is_in_detecting_area:
            return False
        piece_1 = self._image_similar_piece
        piece_2 = grid._image_similar_full
        res = cv2.matchTemplate(piece_2, piece_1, cv2.TM_CCOEFF_NORMED)
        _, sim, _, point = cv2.minMaxLoc(res)
        return sim > lower_template_match_similarity(similarity)
