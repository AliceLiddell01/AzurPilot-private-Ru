"""模板匹配模块。

定义 Template 类，用于对截图进行模板匹配以识别游戏 UI 元素。
支持服务器特定资源路径、GIF 动画模板和二值化匹配等高级功能。
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
        """初始化模板资源。

        Args:
            file: 模板文件路径，支持字典形式的服务器路径映射或普通字符串路径。
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
                        # 与第一帧保持通道数一致，取单通道
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
        """GIF 模板匹配，对每帧同时尝试原图和水平翻转。"""
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
        """对输入图像进行预处理。

        Args:
            image: 输入图像，np.ndarray 格式。

        Returns:
            预处理后的图像。
        """
        return image

    @cached_property
    def size(self):
        if self.is_gif:
            return self.image[0].shape[0:2][::-1]
        else:
            return self.image.shape[0:2][::-1]

    def match(self, image, scaling=1.0, similarity=0.85, direct_match=False):
        """在截图图像上进行模板匹配。

        Args:
            image: 截图图像。
            scaling: 缩放比例，用于缩放模板以匹配图像。
            similarity: 相似度阈值，范围 0 到 1。
            direct_match: 若为 True，跳过 lower_template_match_similarity 的阈值限制。

        Returns:
            是否匹配成功。
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
        """二值化后进行模板匹配。

        Args:
            image: 截图图像。
            similarity: 相似度阈值，范围 0 到 1。

        Returns:
            是否匹配成功。
        """
        similarity = lower_template_match_similarity(similarity)
        if self.is_gif:
            # 灰度化
            image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # 二值化
            _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            return self._match_gif(image_binary, self.image_binary, similarity, name=self.name)

        else:
            # 灰度化
            image_gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            # 二值化
            _, image_binary = cv2.threshold(image_gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
            # 模板匹配
            res = template_match(image_binary, self.image_binary, name=self.name)
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim > similarity

    def match_luma(self, image, similarity=0.85):
        similarity = lower_template_match_similarity(similarity)
        if self.is_gif:
            image = rgb2luma(image)
            return self._match_gif(image, self.image_luma, similarity, name=self.name)

        else:
            res = template_match(
                image,
                self.image,
                template_gray=lambda: self.image_gray,
                name=self.name,
            )
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim > similarity

    def _point_to_button(self, point, image=None, name=None):
        """将匹配点转换为 Button 对象。

        Args:
            point: 匹配位置的坐标点 (x, y)。
            image: 截图图像。若提供，则从中加载颜色和图像信息。
            name: 按钮名称。

        Returns:
            根据匹配点生成的 Button 对象。
        """
        if name is None:
            name = self.name
        area = area_offset(area=(0, 0, *self.size), offset=point)
        button = Button(area=area, color=(), button=area, name=name)
        if image is not None:
            button.load_color(image)
        return button

    def match_result(self, image, name=None):
        """模板匹配并返回相似度和匹配位置的 Button 对象。

        Args:
            image: 截图图像。
            name: 按钮名称。

        Returns:
            相似度（float）和对应的 Button 对象。
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

        # result: np.array([[x0, y0], [x1, y1], ...])  匹配位置坐标数组
        if scaling != 1.0:
            result = np.round(result / scaling).astype(int)
        result = Points(result).group(threshold=threshold)
        return [self._point_to_button(point, image=raw, name=name) for point in result]

    def split_server(self):
        """按服务器拆分为 4 个独立的 Button 对象。

        Returns:
            以服务器名称为键、Button 对象为值的字典。
        """
        out = {}
        for s in VALID_SERVER:
            out[s] = Template(
                file=self.parse_property(self.raw_file, s),
            )
        return out
