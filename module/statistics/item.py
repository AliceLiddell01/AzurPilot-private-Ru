"""Определение и распознавание предметов добычи.

``Item`` представляет один предмет и его количество. ``ItemGrid`` распознаёт
иконки, количество, стоимость и метки по шаблонам и OCR, а также ограничивает
аномальные значения количества.
"""

import numpy as np

import module.config.server as server
from module.base.button import ButtonGrid
from module.base.utils import *
from module.logger import logger
from module.ocr.ocr import Digit, DigitYuv
from module.statistics.utils import *

ITEM_AMOUNT_MAX = {
    'Chip': 100,
    'Gem': 100,
    'Cube': 20,
    'Oil': 1000,
    'Coin': 5000,
}
DEFAULT_AMOUNT_MAX = 2147483645


class AmountOcr(Digit):
    MAX_RETRY = 3

    def pre_process(self, image):
        """Выделить белый текст количества на исходном изображении."""
        image = extract_white_letters(image, threshold=self.threshold)
        return image.astype(np.uint8)

    def ocr_with_validation(self, image, item_name=None, direct_ocr=False):
        """Распознать количество с проверкой верхнего предела и повторами.

        Если три повтора всё ещё дают значение выше допустимого максимума,
        последняя цифра отбрасывается как вероятная OCR-ошибка.
        """
        max_val = ITEM_AMOUNT_MAX.get(item_name, DEFAULT_AMOUNT_MAX)

        if direct_ocr:
            images = [self.pre_process(image)]
        else:
            images = [self.pre_process(crop(image, area)) for area in self.buttons]
        images = [crop_to_text(i) for i in images]

        result_str = self.cnocr.atomic_ocr_for_single_lines(images, self.alphabet)[0]
        amount = self.after_process(result_str)

        if amount <= max_val:
            return amount

        for retry in range(self.MAX_RETRY):
            logger.warning(f'{item_name}: количество {amount} превышает максимум {max_val}; повтор {retry + 1}/{self.MAX_RETRY}')
            result_str = self.cnocr.atomic_ocr_for_single_lines(images, self.alphabet)[0]
            amount = self.after_process(result_str)
            if amount <= max_val:
                logger.info(f'{item_name}: количество подтверждено после повторов {retry + 1}: {amount}')
                return amount

        if amount > max_val and amount >= 10:
            truncated = int(str(amount)[:-1])
            logger.warning(f'{item_name}: количество {amount} всё ещё превышает максимум после {self.MAX_RETRY} повторов; '
                          f'сокращается до {truncated}')
            return truncated

        return amount

    def ocr_batch_with_validation(self, image_list, item_names=None, direct_ocr=True):
        """Распознать и проверить количество для списка предметов."""
        if item_names is None:
            item_names = [None] * len(image_list)

        results = []
        for image, item_name in zip(image_list, item_names):
            amount = self.ocr_with_validation(image, item_name=item_name, direct_ocr=direct_ocr)
            results.append(amount)
        return results


AMOUNT_OCR = AmountOcr([], threshold=96, name='Amount_ocr')
# Интерфейс обновился 2025-08-14, но сервер TW всё ещё использует старую версию.
if server.server == 'tw':
    PRICE_OCR = DigitYuv([], letter=(255, 223, 57), threshold=128, name='Price_ocr')
elif server.server == 'jp':
    PRICE_OCR = Digit([], lang='cnocr', letter=(205, 205, 205), threshold=128, name='Price_ocr')
else:
    PRICE_OCR = Digit([], letter=(255, 255, 255), threshold=128, name='Price_ocr')


class Item:
    IMAGE_SHAPE = (96, 96)

    def __init__(self, image, button):
        """Создать предмет, обрезав и при необходимости масштабировав изображение."""
        self.image_raw = image
        self._button = button
        image = crop(image, button.area)
        if image.shape == self.IMAGE_SHAPE:
            self.image = image
        else:
            self.image = cv2.resize(image, self.IMAGE_SHAPE, interpolation=cv2.INTER_CUBIC)
        self.is_valid = self.predict_valid()
        self._name = 'DefaultItem'
        self.amount = 1
        self._cost = 'DefaultCost'
        self.price = 0
        self.tag = None

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        """Установить имя предмета, отбросив числовой суффикс шаблона."""
        if '_' in value:
            pre, suffix = value.rsplit('_', 1)
            if suffix.isdigit():
                value = pre
        self._name = value

    @property
    def cost(self):
        return self._cost

    @cost.setter
    def cost(self, value):
        if '_' in value:
            pre, suffix = value.rsplit('_', 1)
            if suffix.isdigit():
                value = pre
        self._cost = value

    def is_known_item(self):
        if self.name == 'DefaultItem':
            return False
        elif self.name.isdigit():
            return False
        else:
            return True

    def __str__(self):
        if self.name != 'DefaultItem' and self.cost == 'DefaultCost':
            name = f'{self.name}_x{self.amount}'
        elif self.name == 'DefaultItem' and self.cost != 'DefaultCost':
            name = f'{self.cost}_x{self.price}'
        else:
            name = f'{self.name}_x{self.amount}_{self.cost}_x{self.price}'

        if self.tag is not None:
            name = f'{name}_{self.tag}'

        return name

    def predict_valid(self):
        return np.mean(rgb2gray(self.image) > 127) > 0.1

    @property
    def button(self):
        return self._button.button

    def crop(self, area):
        return crop(self.image_raw, area_offset(area, offset=self._button.area[:2]))

    def __eq__(self, other):
        # Используется для устранения дублей внутри Filter.apply().
        return str(self) == str(other)

    def __hash__(self):
        # Имя объединяет дубли при сопоставлении двух снимков полученных предметов.
        return hash(self.name)


class ItemGrid:
    item_class = Item
    similarity = 0.92
    extract_similarity = 0.92
    cost_similarity = 0.75

    def __init__(self, grids, templates, template_area=(40, 21, 89, 70), amount_area=(60, 71, 91, 92),
                 cost_area=(6, 123, 84, 166), price_area=(52, 132, 132, 156), tag_area=(81, 4, 91, 8)):
        """Создать сетку предметов и настроить области распознавания."""
        self.amount_ocr = AMOUNT_OCR
        self.price_ocr = PRICE_OCR
        self.grids = grids
        self.template_area = template_area
        self.amount_area = amount_area
        self.cost_area = cost_area
        self.price_area = price_area
        self.tag_area = tag_area

        self.colors = {}
        self.templates = {}
        self.templates_hit = {}
        self.next_template_index = len(self.templates.keys())
        for name, template in templates.items():
            self.templates[name] = crop(template.image, area=self.template_area)
            self.templates_hit[name] = 0
            if name.isdigit() and int(name) > self.next_template_index:
                self.next_template_index = int(name)

        self.cost_templates = {}
        self.cost_templates_hit = {}
        self.next_cost_template_index = len(self.cost_templates.keys())

        self.items = []

    def _load_image(self, image):
        """Загрузить из снимка все визуально допустимые предметы."""
        self.items = []
        for button in self.grids.buttons:
            item = self.item_class(image, button)
            if item.is_valid:
                self.items.append(item)

    def load_template_folder(self, folder):
        """Загрузить шаблоны предметов из каталога."""
        logger.info(f'Загрузка каталога шаблонов: {folder}')
        max_digit = 0
        data = load_folder(folder)
        for name, image in data.items():
            if name in self.templates:
                continue
            image = load_image(image)
            image = crop(image, area=self.template_area)
            self.colors[name] = cv2.mean(image)[:3]
            self.templates[name] = image
            self.templates_hit[name] = 0
            if name.isdigit():
                max_digit = max(max_digit, int(name))
            self.next_template_index += 1
        self.next_template_index = max(self.next_template_index, max_digit + 1)
        logger.attr('next_template_index', self.next_template_index)

    def load_cost_template_folder(self, folder):
        """Загрузить шаблоны типа стоимости из каталога."""
        max_digit = 0
        data = load_folder(folder)
        for name, image in data.items():
            if name in self.cost_templates:
                continue
            image = load_image(image)
            self.cost_templates[name] = image
            self.cost_templates_hit[name] = 0
            if name.isdigit():
                max_digit = max(max_digit, int(name))
            self.next_cost_template_index += 1
        self.next_cost_template_index = max(self.next_cost_template_index, max_digit + 1)

    def match_template(self, image, similarity=None):
        """Сопоставить изображение предмета с шаблоном.

        Сначала проверяются наиболее часто совпадавшие известные шаблоны, затем
        числовые. При отсутствии совпадения создаётся новый числовой шаблон.
        """
        if similarity is None:
            similarity = self.similarity
        similarity = lower_template_match_similarity(similarity)
        color = cv2.mean(crop(image, self.template_area))[:3]
        # Сначала проверяем шаблоны с большей исторической частотой совпадений.
        names = np.array(list(self.templates.keys()))[np.argsort(list(self.templates_hit.values()))][::-1]
        # Известные именованные шаблоны проверяются раньше числовых.
        names = [name for name in names if not name.isdigit()] + [name for name in names if name.isdigit()]
        best_name = None
        best_similarity = similarity
        for name in names:
            if color_similar(color1=color, color2=self.colors[name], threshold=30):
                res = cv2.matchTemplate(image, self.templates[name], cv2.TM_CCOEFF_NORMED)
                _, current_similarity, _, _ = cv2.minMaxLoc(res)
                if current_similarity > best_similarity:
                    best_name = name
                    best_similarity = current_similarity

        if best_name is not None:
            self.templates_hit[best_name] += 1
            return best_name

        self.next_template_index += 1
        name = str(self.next_template_index)
        logger.info(f'Новый шаблон: {name}')
        image = crop(image, self.template_area)
        self.colors[name] = cv2.mean(image)[:3]
        self.templates[name] = image
        self.templates_hit[name] = self.templates_hit.get(name, 0) + 1
        return name

    def extract_template(self, image, folder=None):
        """Извлечь новые шаблоны предметов из снимка и при необходимости сохранить."""
        self._load_image(image)
        prev = set(self.templates.keys())
        new = {}
        for item in self.items:
            name = self.match_template(item.image, similarity=self.extract_similarity)
            if name not in prev:
                new[name] = item.image

        if folder is not None:
            for name, im in new.items():
                save_image(im, os.path.join(folder, f'{name}.png'))

        return new

    def match_cost_template(self, item):
        """Сопоставить тип стоимости; отсутствие шаблона делает предмет недопустимым."""
        image = item.crop(self.cost_area)
        cost_similarity = lower_template_match_similarity(self.cost_similarity)
        names = np.array(list(self.cost_templates.keys()))[np.argsort(list(self.cost_templates_hit.values()))][::-1]
        for name in names:
            res = cv2.matchTemplate(image, self.cost_templates[name], cv2.TM_CCOEFF_NORMED)
            _, similarity, _, _ = cv2.minMaxLoc(res)
            if similarity > cost_similarity:
                self.cost_templates_hit[name] += 1
                return name

        # Новые шаблоны стоимости автоматически не создаются.
        return None

    @staticmethod
    def predict_tag(image):
        """Определить метку предмета по цвету области тега."""
        threshold = 50
        color = cv2.mean(np.array(image))[:3]
        if color_similar(color1=color, color2=(49, 125, 222), threshold=threshold):
            # Синий — catchup.
            return 'catchup'
        elif color_similar(color1=color, color2=(33, 199, 239), threshold=threshold):
            # Голубой — bonus.
            return 'bonus'
        elif color_similar(color1=color, color2=(255, 85, 41), threshold=threshold):
            # Красный — event.
            return 'event'
        else:
            return None

    def predict(self, image, name=True, amount=True, cost=False, price=False, tag=False):
        """Распознать запрошенные свойства всех предметов на снимке."""
        self._load_image(image)
        if name:
            name_list = [self.match_template(item.image) for item in self.items]
            for item, n in zip(self.items, name_list):
                item.name = n
        if amount:
            amount_images = [item.crop(self.amount_area) for item in self.items]
            item_names = [item.name for item in self.items]
            amount_list = self.amount_ocr.ocr_batch_with_validation(
                amount_images, item_names=item_names, direct_ocr=True
            )
            for item, a in zip(self.items, amount_list):
                item.amount = a
        if cost:
            cost_list = [self.match_cost_template(item) for item in self.items]
            self.items = [item for item, c in zip(self.items, cost_list) if c is not None]
            cost_list = [c for c in cost_list if c is not None]
            for item, c in zip(self.items, cost_list):
                item.cost = c
        if price and len(self.items):
            price_list = [item.crop(self.price_area) for item in self.items]
            price_list = self.price_ocr.ocr(price_list, direct_ocr=True)
            for item, p in zip(self.items, price_list):
                item.price = p
        if tag:
            tag_list = [self.predict_tag(item.crop(self.tag_area)) for item in self.items]
            for item, t in zip(self.items, tag_list):
                item.tag = t

        # Исключаем предметы с некорректной ценой, когда цена была обязательна.
        items = [item for item in self.items if not (price and item.price <= 0)]
        diff = len(self.items) - len(items)
        if diff > 0:
            logger.warning(f'[Статистика — предметы] Пропущено предметов с ценой <= 0: {diff} шт.')
            self.items = items

        return self.items
