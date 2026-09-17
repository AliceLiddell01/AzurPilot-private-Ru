"""Модуль сопоставления с шаблоном.

Определяет класс Template для сопоставления фрагментов скриншота с шаблонами
с целью распознавания элементов игрового интерфейса. Поддерживает серверные пути
к ресурсам, анимированные GIF-шаблоны и бинаризованное сопоставление.
"""

import os
from functools import partial

import imageio

from module.base.button import Button
from module.base.decorator import cached_property
from module.base.resource import Resource
from module.base.utils import *
from module.base.utils import template_match
from module.config.server import VALID_SERVER
from module.map_detection.utils import Points


class Template(Resource):
    def __init__(self, file):
        """Инициализировать ресурс шаблона.

        Args:
            file: Путь к файлу шаблона; поддерживает словарь сопоставления серверных путей или обычную строку пути.
        """
        self.raw_file = file
        self._image = None
        self._image_binary = None
        self._image_luma = None
        self._image_gray = None

        self.resource_add(self.file)

    cached = ['file', 'name', 'is_gif']

    @cached_property
    def file(self):
        return self.parse_property(self.raw_file)

    @cached_property
    def name(self):
        return os.path.splitext(os.path.basename(self.file))[0].upper()

    @cached_property
    def is_gif(self):
        return os.path.splitext(self.file)[1] == '.gif'

    @property
    def image(self):
        if self._image is None:
            if self.is_gif:
                self._image = []
                channel = 0
                for image in imageio.mimread(self.file):
                    if not channel:
                        channel = len(image.shape)
                    if channel == 3:
                        image = image[:, :, :3].copy()
                    elif len(image.shape) == 3:
                        # Сохраняем число каналов как у первого кадра, оставляя один канал
                        image = image[:, :, 0].copy()

                    image = self.pre_process(image)
                    self._image.append(image)
            else:
                self._image = self.pre_process(load_image(self.file))

        return self._image

    @property
    def image_gray(self):
        """Вернуть кэшированное одноканальное представление шаблона."""
        if self._image_gray is None:
            if self.is_gif:
                self._image_gray = [
                    image if image.ndim == 2 else rgb2gray(image)
                    for image in self.image
                ]
            else:
                self._image_gray = (
                    self.image
                    if self.image.ndim == 2
                    else rgb2gray(self.image)
                )

        return self._image_gray

    @property
    def image_binary(self):
        if self._image_binary is None:
            if self.is_gif:
                self._image_binary = []
                for image in self.image:
                    image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
                    self._image_binary.append(image_binary)
            else:
                image_gray = self.image if self.image.ndim == 2 else cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
                _, self._image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

        return self._image_binary

    @property
    def image_luma(self):
        if self._image_luma is None:
            if self.is_gif:
                self._image_luma = []
                for image in self.image:
                    luma = rgb2luma(image)
                    self._image_luma.append(luma)
            else:
                self._image_luma = rgb2luma(self.image)

        return self._image_luma

    @staticmethod
    def _match_gif(image, templates, similarity, gray_templates=None, name=None):
        """Сопоставление с GIF-шаблоном с одновременной проверкой оригинала и зеркального отражения каждого кадра."""
        for index, template in enumerate(templates):
            if gray_templates is None:
                gray_template = None
            elif callable(gray_templates):
                gray_template = partial(gray_templates, index)
            else:
                gray_template = gray_templates[index]
            res = template_match(image, template, template_gray=gray_template, name=name)
            _, sim, _, _ = cv2.minMaxLoc(res)
            if sim > similarity:
                return True
            flipped_template = cv2.flip(template, 1)
            if gray_template is None:
                flipped_gray = None
            elif callable(gray_template):
                flipped_gray = lambda gray_template=gray_template: cv2.flip(gray_template(), 1)
            else:
                flipped_gray = cv2.flip(gray_template, 1)
            res = template_match(image, flipped_template, template_gray=flipped_gray, name=name)
            _, sim, _, _ = cv2.minMaxLoc(res)
            if sim > similarity:
                return True
        return False

    @image.setter
    def image(self, value):
        self._image = value

    def resource_release(self):
        super().resource_release()
        self._image = None
        self._image_binary = None
        self._image_luma = None
        self._image_gray = None

    def pre_process(self, image):
        """Предварительно обработать входное изображение.

        Args:
            image: Входное изображение в формате np.ndarray.

        Returns:
            Обработанное изображение.
        """
        return image

    @cached_property
    def size(self):
        if self.is_gif:
            return self.image[0].shape[0:2][::-1]
        else:
            return self.image.shape[0:2][::-1]

    def match(self, image, scaling=1.0, similarity=0.85, direct_match=False):
        """Выполнить сопоставление с шаблоном на изображении скриншота.

        Args:
            image: Изображение скриншота.
            scaling: Масштаб для подгонки шаблона к изображению.
            similarity: Порог сходства в диапазоне от 0 до 1.
            direct_match: Если True, пропускает ограничение порога lower_template_match_similarity.

        Returns:
            Успешно ли сопоставление.
        """
        if not direct_match:
            similarity = lower_template_match_similarity(similarity)
        scaling = 1 / scaling
        if scaling != 1.0:
            image = cv2.resize(image, None, fx=scaling, fy=scaling)

        if self.is_gif:
            return self._match_gif(
                image,
                self.image,
                similarity,
                gray_templates=lambda index: self.image_gray[index],
                name=self.name,
            )

        else:
            res = template_match(
                image,
                self.image,
                template_gray=lambda: self.image_gray,
                name=self.name,
            )
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim > similarity

    def match_binary(self, image, similarity=0.85):
        """Выполнить сопоставление с шаблоном после бинаризации.

        Args:
            image: Изображение скриншота.
            similarity: Порог сходства в диапазоне от 0 до 1.

        Returns:
            Успешно ли сопоставление.
        """
        similarity = lower_template_match_similarity(similarity)
        if self.is_gif:
            # Преобразование в градации серого
            image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # Бинаризация
            _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            return self._match_gif(image_binary, self.image_binary, similarity, name=self.name)

        else:
            # Преобразование в градации серого
            image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # Бинаризация
            _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            # Сопоставление с шаблоном
            res = template_match(image_binary, self.image_binary, name=self.name)
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim > similarity

    def match_luma(self, image, similarity=0.85):
        similarity = lower_template_match_similarity(similarity)
        if self.is_gif:
            image = rgb2luma(image)
            return self._match_gif(image, self.image_luma, similarity, name=self.name)

        else:
            image_luma = rgb2luma(image)
            res = template_match(image_luma, self.image_luma, name=self.name)
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim > similarity

    def _point_to_button(self, point, image=None, name=None):
        """Преобразовать точку совпадения в объект Button.

        Args:
            point: Координаты точки совпадения (x, y).
            image: Изображение скриншота; если передано, загружает цвет и информацию об изображении.
            name: Имя кнопки.

        Returns:
            Объект Button, сформированный по точке совпадения.
        """
        if name is None:
            name = self.name
        area = area_offset(area=(0, 0, *self.size), offset=point)
        button = Button(area=area, color=(), button=area, name=name)
        if image is not None:
            button.load_color(image)
        return button

    def match_result(self, image, name=None):
        """Выполнить сопоставление с шаблоном и вернуть сходство и объект Button в найденной позиции.

        Args:
            image: Изображение скриншота.
            name: Имя кнопки.

        Returns:
            Сходство (float) и соответствующий объект Button.
        """
        res = template_match(
            image,
            self.image,
            template_gray=lambda: self.image_gray,
            name=self.name,
        )
        _, sim, _, point = cv2.minMaxLoc(res)
        # print(self.file, sim)

        button = self._point_to_button(point, image=image, name=name)
        return sim, button

    def match_luma_result(self, image, name=None):
        raw = image
        image_luma = rgb2luma(image)
        res = template_match(image_luma, self.image_luma, name=self.name)
        _, sim, _, point = cv2.minMaxLoc(res)
        # print(self.file, sim)

        button = self._point_to_button(point, image=raw, name=name)
        return sim, button

    def match_multi(self, image, scaling=1.0, similarity=0.85, threshold=3, name=None):
        """Найти все совпадения шаблона и вернуть список объектов Button.

        Args:
            image: Изображение screenshot.
            scaling: Масштаб для сопоставления с изображением.
            similarity: Порог сходства от 0 до 1.
            threshold: Расстояние кластеризации соседних совпадений.
            name: Имя кнопки.

        Returns:
            Список объектов Button для всех найденных позиций.
        """
        similarity = lower_template_match_similarity(similarity)
        scaling = 1 / scaling
        if scaling != 1.0:
            image = cv2.resize(image, None, fx=scaling, fy=scaling)

        raw = image

        if self.is_gif:
            result = []
            for index, template in enumerate(self.image):
                gray_template = lambda index=index: self.image_gray[index]
                res = template_match(image, template, template_gray=gray_template, name=self.name)
                result += np.array(np.where(res > similarity)).T[:, ::-1].tolist()
                flipped_template = cv2.flip(template, 1)
                flipped_gray = lambda index=index: cv2.flip(self.image_gray[index], 1)
                res = template_match(image, flipped_template, template_gray=flipped_gray, name=self.name)
                result += np.array(np.where(res > similarity)).T[:, ::-1].tolist()
            result = np.array(result)
        else:
            result = template_match(
                image,
                self.image,
                template_gray=lambda: self.image_gray,
                name=self.name,
            )
            result = np.array(np.where(result > similarity)).T[:, ::-1]

        # result: np.array([[x0, y0], [x1, y1], ...]) — массив координат позиций совпадений
        if scaling != 1.0:
            result = np.round(result / scaling).astype(int)
        result = Points(result).group(threshold=threshold)
        return [self._point_to_button(point, image=raw, name=name) for point in result]

    def split_server(self):
        """Разбить на 4 независимых объекта Button по серверам.

        Returns:
            Словарь, где ключ — имя сервера, а значение — объект Button.
        """
        out = {}
        for s in VALID_SERVER:
            out[s] = Template(
                file=self.parse_property(self.raw_file, s),
            )
        return out
