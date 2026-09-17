"""Модуль компонентов кнопок и сеток кнопок.

Определяет базовые классы системы визуального взаимодействия Button и ButtonGrid —
базовые единицы любого UI-взаимодействия.
Включает логику смещения координат, распознавания цветов/шаблонов и эмуляции кликов.
"""

# Этот файл определяет основные классы визуального взаимодействия Alas: Button (кнопка) и связанные сетки.
# Это базовая единица всех UI-взаимодействий: смещения координат, распознавание цветов/шаблонов и эмуляция кликов.
import os
import traceback
import typing as t

from PIL import ImageDraw

from module.base.decorator import cached_property
from module.base.resource import Resource
from module.base.utils import *
from module.base.utils import template_match
from module.config.server import VALID_SERVER
from module.logger import logger


class Button(Resource):
    def __init__(self, area, color, button, file=None, name=None):
        """Инициализировать экземпляр Button.

        Args:
            area (dict[tuple], tuple): Область появления кнопки на скриншоте.
                (левый верхний x, левый верхний y, правый нижний x, правый нижний y)
            color (dict[tuple], tuple): Ожидаемый цвет данной области.
                (r, g, b)
            button (dict[tuple], tuple): Кликабельная область при появлении кнопки.
                (левый верхний x, левый верхний y, правый нижний x, правый нижний y)
                Если передан пустой кортеж, объект используется исключительно как детектор.

        Examples:
            BATTLE_PREPARATION = Button(
                area=(1562, 908, 1864, 1003),
                color=(231, 181, 90),
                button=(1562, 908, 1864, 1003)
            )
        """
        self.raw_area = area
        self.raw_color = color
        self.raw_button = button
        self.raw_file = file
        self.raw_name = name

        self._button_offset = None
        self._match_init = False
        self._match_gray_init = False
        self._match_binary_init = False
        self._match_luma_init = False
        self.image = None
        self.image_gray = None
        self.image_binary = None
        self.image_luma = None

        if self.file:
            self.resource_add(key=self.file)

    cached = ['area', 'color', '_button', 'file', 'name', 'is_gif']

    @cached_property
    def area(self):
        return self.parse_property(self.raw_area)

    @cached_property
    def color(self):
        return self.parse_property(self.raw_color)

    @cached_property
    def _button(self):
        return self.parse_property(self.raw_button)

    @cached_property
    def file(self):
        return self.parse_property(self.raw_file)

    @cached_property
    def name(self):
        if self.raw_name:
            return self.raw_name
        elif self.file:
            return os.path.splitext(os.path.split(self.file)[1])[0]
        else:
            return 'BUTTON'

    @cached_property
    def is_gif(self):
        if self.file:
            return os.path.splitext(self.file)[1] == '.gif'
        else:
            return False

    def __str__(self):
        return self.name

    __repr__ = __str__

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return hash(self.name)

    def __bool__(self):
        return True

    @property
    def button(self):
        if self._button_offset is None:
            return self._button
        else:
            return self._button_offset

    def appear_on(self, image, threshold=10):
        """Проверить появление кнопки на скриншоте по цвету.

        Args:
            image (np.ndarray): Изображение скриншота.
            threshold (int): Порог сходства цвета, по умолчанию 10.

        Returns:
            bool: True, если кнопка присутствует на скриншоте.
        """
        return color_similar(
            color1=get_color(image, self.area),
            color2=self.color,
            threshold=threshold
        )

    def load_color(self, image):
        """Загрузить цвет из соответствующей области указанного скриншота. Этот метод необратим и используется в особых случаях.

        Args:
            image: Изображение скриншота.

        Returns:
            tuple: Значение цвета (r, g, b).
        """
        self.__dict__['color'] = get_color(image, self.area)
        self.image = crop(image, self.area)
        self.__dict__['is_gif'] = False
        self.image_gray = None
        self.image_binary = None
        self.image_luma = None
        self._match_gray_init = False
        self._match_binary_init = False
        self._match_luma_init = False
        return self.color

    def load_offset(self, button):
        """Загрузить смещение от другой кнопки.

        Args:
            button (Button): Опорная кнопка.
        """
        offset = np.subtract(button.button, button._button)[:2]
        self._button_offset = area_offset(self._button, offset=offset)

    def clear_offset(self):
        self._button_offset = None

    def ensure_template(self):
        """Загрузить изображение ресурса. Этот метод необходимо вызвать перед вызовом self.match."""
        if not self._match_init:
            if self.is_gif:
                self.image = []
                import imageio
                for image in imageio.mimread(self.file):
                    image = image[:, :, :3].copy() if len(image.shape) == 3 else image
                    image = crop(image, self.area)
                    self.image.append(image)
            else:
                self.image = load_image(self.file, self.area)
            self._match_init = True

    def ensure_binary_template(self):
        """Загрузить бинаризованное изображение ресурса. Этот метод необходимо вызвать перед вызовом self.match_binary."""
        if not self._match_binary_init:
            if self.is_gif:
                self.image_binary = []
                for image in self.image:
                    image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
                    self.image_binary.append(image_binary)
            else:
                image_gray = self.image if self.image.ndim == 2 else cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
                _, self.image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            self._match_binary_init = True

    def ensure_gray_template(self):
        """Загрузить кэшированное одноканальное представление шаблона."""
        if not self._match_gray_init:
            if self.image is None:
                self.ensure_template()
            if self.is_gif:
                self.image_gray = [image if image.ndim == 2 else rgb2gray(image) for image in self.image]
            else:
                self.image_gray = self.image if self.image.ndim == 2 else rgb2gray(self.image)
            self._match_gray_init = True

    def _get_gray_template(self, index=None):
        """Лениво вернуть одноканальный кэш шаблона для matcher-а."""
        self.ensure_gray_template()
        return self.image_gray if index is None else self.image_gray[index]

    def ensure_luma_template(self):
        if not self._match_luma_init:
            if self.is_gif:
                self.image_luma = []
                for image in self.image:
                    luma = rgb2luma(image)
                    self.image_luma.append(luma)
            else:
                self.image_luma = rgb2luma(self.image)
            self._match_luma_init = True

    def resource_release(self):
        super().resource_release()
        self.image = None
        self.image_gray = None
        self.image_binary = None
        self.image_luma = None
        self._match_init = False
        self._match_gray_init = False
        self._match_binary_init = False
        self._match_luma_init = False

    def match(self, image, offset=30, similarity=0.85):
        """Обнаружить кнопку через сопоставление с шаблоном. Позиция части кнопок может быть не фиксирована.

        Args:
            image: Изображение скриншота.
            offset (int, tuple): Смещение области поиска.
            similarity (float): Порог сходства в диапазоне 0-1.

        Returns:
            bool: True при успешном сопоставлении.
        """
        similarity = lower_template_match_similarity(similarity)
        self.ensure_template()

        if isinstance(offset, tuple):
            if len(offset) == 2:
                offset = np.array((-offset[0], -offset[1], offset[0], offset[1]))
            else:
                offset = np.array(offset)
        else:
            offset = np.array((-3, -offset, 3, offset))
        image = crop(image, offset + self.area, copy=False)

        if self.is_gif:
            for index, template in enumerate(self.image):
                res = template_match(
                    image,
                    template,
                    template_gray=lambda index=index: self._get_gray_template(index),
                    name=self.name,
                )
                _, sim, _, point = cv2.minMaxLoc(res)
                self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
                if sim > similarity:
                    return True
            return False
        else:
            res = template_match(
                image,
                self.image,
                template_gray=lambda: self._get_gray_template(),
                name=self.name,
            )
            _, sim, _, point = cv2.minMaxLoc(res)
            self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
            return sim > similarity

    def match_binary(self, image, offset=30, similarity=0.85):
        """Обнаружить кнопку через бинаризованное сопоставление с шаблоном. Позиция части кнопок может быть не фиксирована.

        Args:
            image: Изображение скриншота.
            offset (int, tuple): Смещение области поиска.
            similarity (float): Порог сходства в диапазоне 0-1.

        Returns:
            bool: True при успешном сопоставлении.
        """
        similarity = lower_template_match_similarity(similarity)
        self.ensure_template()
        self.ensure_binary_template()

        if isinstance(offset, tuple):
            if len(offset) == 2:
                offset = np.array((-offset[0], -offset[1], offset[0], offset[1]))
            else:
                offset = np.array(offset)
        else:
            offset = np.array((-3, -offset, 3, offset))
        image = crop(image, offset + self.area, copy=False)
        image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

        if self.is_gif:
            for template in self.image_binary:
                # Сопоставление с шаблоном
                res = template_match(image_binary, template, name=self.name)
                _, sim, _, point = cv2.minMaxLoc(res)
                self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
                if sim > similarity:
                    return True
            return False
        else:
            # Сопоставление с шаблоном
            res = template_match(image_binary, self.image_binary, name=self.name)
            _, sim, _, point = cv2.minMaxLoc(res)
            self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
            return sim > similarity

    def match_luma(self, image, offset=30, similarity=0.85):
        """Обнаружить кнопку через сопоставление с шаблоном по Y-каналу (яркости).

        Args:
            image: Изображение скриншота.
            offset (int, tuple): Смещение области поиска.
            similarity (float): Порог сходства в диапазоне 0-1.

        Returns:
            bool: True при успешном сопоставлении.
        """
        similarity = lower_template_match_similarity(similarity)
        self.ensure_template()
        self.ensure_luma_template()

        if isinstance(offset, tuple):
            if len(offset) == 2:
                offset = np.array((-offset[0], -offset[1], offset[0], offset[1]))
            else:
                offset = np.array(offset)
        else:
            offset = np.array((-3, -offset, 3, offset))
        image = crop(image, offset + self.area, copy=False)

        if self.is_gif:
            image_luma = rgb2luma(image)
            for template in self.image_luma:
                res = template_match(image_luma, template, name=self.name)
                _, sim, _, point = cv2.minMaxLoc(res)
                self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
                if sim > similarity:
                    return True
        else:
            image_luma = rgb2luma(image)
            res = template_match(image_luma, self.image_luma, name=self.name)
            _, sim, _, point = cv2.minMaxLoc(res)
            self._button_offset = area_offset(self._button, offset[:2] + np.array(point))
            return sim > similarity

    def match_template_color(self, image, offset=(20, 20), similarity=0.85, threshold=30):
        """Сначала выполнить сопоставление с шаблоном, затем проверку цвета.

        Args:
            image: Изображение скриншота.
            offset (int, tuple): Смещение области поиска.
            similarity (float): Порог сходства сопоставления с шаблоном в диапазоне 0-1.
            threshold (int): Порог сходства цвета, по умолчанию 30.

        Returns:
            bool: True при успешном сопоставлении.
        """
        if self.match_luma(image, offset=offset, similarity=similarity):
            diff = np.subtract(self.button, self._button)[:2]
            area = area_offset(self.area, offset=diff)
            color = get_color(image, area)
            return color_similar(color1=color, color2=self.color, threshold=threshold)
        else:
            return False

    def crop(self, area, image=None, name=None):
        """Получить новую кнопку по относительным координатам.

        Args:
            area (tuple): Область обрезки относительно текущей кнопки.
            image (np.ndarray): Изображение скриншота; если передано, загружает из него цвет и изображение.
            name (str): Имя новой кнопки.

        Returns:
            Button: Новая кнопка после обрезки.
        """
        if name is None:
            name = self.name
        new_area = area_offset(area, offset=self.area[:2])
        new_button = area_offset(area, offset=self.button[:2])
        button = Button(area=new_area, color=self.color, button=new_button, file=self.file, name=name)
        if image is not None:
            button.load_color(image)
        return button

    def move(self, vector, image=None, name=None):
        """Сместить позицию кнопки.

        Args:
            vector (tuple): Вектор смещения.
            image (np.ndarray): Изображение скриншота; если передано, загружает из него цвет и изображение.
            name (str): Имя новой кнопки.

        Returns:
            Button: Новая кнопка после перемещения.
        """
        if name is None:
            name = self.name
        new_area = area_offset(self.area, offset=vector)
        new_button = area_offset(self.button, offset=vector)
        button = Button(area=new_area, color=self.color, button=new_button, file=self.file, name=name)
        if image is not None:
            button.load_color(image)
        return button

    def split_server(self):
        """Разбить на 4 кнопки под конкретные серверы.

        Returns:
            dict[str, Button]: Словарь с именами серверов в качестве ключей и кнопками в качестве значений.
        """
        out = {}
        for s in VALID_SERVER:
            out[s] = Button(
                area=self.parse_property(self.raw_area, s),
                color=self.parse_property(self.raw_color, s),
                button=self.parse_property(self.raw_button, s),
                file=self.parse_property(self.raw_file, s),
                name=self.name
            )
        return out


class ButtonGrid:
    def __init__(self, origin, delta, button_shape, grid_shape, name=None):
        self.origin = np.array(origin)
        self.delta = np.array(delta)
        self.button_shape = np.array(button_shape)
        self.grid_shape = np.array(grid_shape)
        if name:
            self._name = name
        else:
            (filename, line_number, function_name, text) = traceback.extract_stack()[-2]
            self._name = text[:text.find('=')].strip()

    def __getitem__(self, item):
        base = np.round(np.array(item) * self.delta + self.origin).astype(int)
        area = tuple(np.append(base, base + self.button_shape))
        return Button(area=area, color=(), button=area, name='%s_%s_%s' % (self._name, item[0], item[1]))

    def generate(self):
        for y in range(self.grid_shape[1]):
            for x in range(self.grid_shape[0]):
                yield x, y, self[x, y]

    @cached_property
    def buttons(self):
        return list([button for _, _, button in self.generate()])

    def crop(self, area, name=None):
        """Обрезать ButtonGrid по относительным координатам.

        Args:
            area (tuple): Область обрезки относительно self.origin.
            name (str): Имя нового экземпляра ButtonGrid.

        Returns:
            ButtonGrid: Новый экземпляр ButtonGrid после обрезки.
        """
        if name is None:
            name = self._name
        origin = self.origin + area[:2]
        button_shape = np.subtract(area[2:], area[:2])
        return ButtonGrid(
            origin=origin, delta=self.delta, button_shape=button_shape, grid_shape=self.grid_shape, name=name)

    def move(self, vector, name=None):
        """Сместить позицию ButtonGrid.

        Args:
            vector (tuple): Вектор смещения.
            name (str): Имя нового экземпляра ButtonGrid.

        Returns:
            ButtonGrid: Новый экземпляр ButtonGrid после перемещения.
        """
        if name is None:
            name = self._name
        origin = self.origin + vector
        return ButtonGrid(
            origin=origin, delta=self.delta, button_shape=self.button_shape, grid_shape=self.grid_shape, name=name)

    def gen_mask(self):
        """Сгенерировать изображение маски для отладочного отображения объекта ButtonGrid.

        Returns:
            PIL.Image.Image: Изображение маски с белыми областями кнопок на чёрном фоне.
        """
        image = Image.new("RGB", (1280, 720), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        for button in self.buttons:
            draw.rectangle((button.area[:2], button.button[2:]), fill=(255, 255, 255), outline=None)
        return image

    def show_mask(self):
        self.gen_mask().show()

    def save_mask(self):
        """Сохранить изображение маски в файл {name}.png."""
        self.gen_mask().save(f'{self._name}.png')
