import re
from collections.abc import Mapping

import cv2
import numpy as np

import module.config.server as server
from module.base.utils import (
    color_similar,
    color_similarity_2d,
    lower_template_match_similarity,
    rgb2luma,
)
from module.logger import logger
from module.ocr.ocr import Digit, Ocr
from module.shop_event.catalog import (
    bind_catalog_source,
    catalog_template_names,
    resolve_catalog_claim,
)
from module.shop_event.selector import FILTER_REGEX
from module.statistics.item import Item, ItemGrid

ITEM_SHAPE = (63, 63)
GRID_SHAPE = (152, 206)
DELTA_PRICE_BACKGROUND = (14, 164)
DELTA_ITEM = (45, 44, 45 + ITEM_SHAPE[0], 33 + ITEM_SHAPE[1])
DELTA_AMOUNT = (13, 144, 136, 160)
DELTA_PRICE = (28, 164, 128, 193)
# Текст цены начинается правее иконки валюты. Отдельная область не даёт
# локальным краям плашки или PT-иконке превращаться в ведущую OCR-цифру.
DELTA_PRICE_TEXT = (64, 164, 128, 193)
DELTA_TAG = (108, 30, 155, 52)
COUNTER_COLOR = (106, 120, 131)
COUNTER_THRESHOLD = 150
PRICE_THRESHOLD = 230
PRICE_BACKGROUND_COLOR = (61, 78, 91)
EVENT_TEMPLATE_SEARCH_MARGIN = 4
EVENT_TEMPLATE_MIN_GAP = 0.02
if server.server == 'jp':
    COUNTER_LEFT_STRIP = 54
elif server.server == 'en':
    COUNTER_LEFT_STRIP = 42
else:
    COUNTER_LEFT_STRIP = 70


class CounterOcr(Ocr):
    def __init__(self, buttons, lang='azur_lane', letter=(255, 255, 255), threshold=128,
                 alphabet='0123456789/IDSB', name=None):
        super().__init__(buttons, lang=lang, letter=letter, threshold=threshold, alphabet=alphabet, name=name)

    def pre_process(self, image):
        mask = color_similarity_2d(image, (255, 255, 255))
        brightness = np.min(mask, axis=0)
        match = np.where(brightness < COUNTER_THRESHOLD)[0]
        if len(match):
            left = match[0] + COUNTER_LEFT_STRIP
            total = mask.shape[1]
            if left < total:
                image = image[:, left:]
        image = super().pre_process(image)
        return image

    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace('I', '1').replace('D', '0').replace('S', '5')
        result = result.replace('B', '8')
        return result

    @staticmethod
    def parse_counter_result(value):
        """Разобрать один OCR-токен ``текущее/всего`` без догадок об остатке."""
        text = str(value or '').strip()
        parts = text.split('/')
        if len(parts) != 2:
            logger.warning(
                f'[Магазин события — товар] Некорректный формат счётчика OCR: {text!r}'
            )
            return [0, 0]

        current_text, total_text = (part.strip() for part in parts)
        if not total_text.isdecimal():
            logger.warning(
                f'[Магазин события — товар] Не удалось прочитать максимальный остаток OCR: {text!r}; '
                'товар заблокирован для текущего сканирования'
            )
            return [0, 0]

        total = int(total_text)
        if not current_text.isdecimal():
            logger.warning(
                f'[Магазин события — товар] Не удалось прочитать текущий остаток OCR: {text!r}; '
                f'используется безопасный остаток 0/{total}'
            )
            return [0, total]

        current = int(current_text)
        if current > total:
            logger.warning(
                f'[Магазин события — товар] OCR вернул невозможный остаток {current}/{total}; '
                'товар заблокирован для текущего сканирования'
            )
            return [0, total]
        return [current, total]

    def ocr(self, image, direct_ocr=False):
        """Распознать счётчик вида ``14/15`` и вернуть пару ``[14, 15]``.

        Аргументы:
            image: исходное изображение или список изображений.
            direct_ocr: передать ли изображение непосредственно в OCR.

        Возвращает:
            Для списка изображений — список пар ``[текущее, всего]``;
            для одного изображения — одну такую пару.
        """
        result = super().ocr(image, direct_ocr=direct_ocr)
        if isinstance(result, list):
            return [self.parse_counter_result(value) for value in result]
        return self.parse_counter_result(result)


PRICE_OCR = Digit([], letter=(221, 221, 221), threshold=128, name='Price_ocr')


URPT_PRICE_IN_PT = 150  # Один URpt стоит 150 PT.
COIN_PRICE_IN_URPT = 1  # Одна монета стоит 1 URpt.
UR_SHIP_PRICES_IN_URPT = [200, 300]  # UR-корабли стоят 200 или 300 URpt.


class EventShopItem(Item):
    IMAGE_SHAPE = ITEM_SHAPE

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_ship = False
        self._scroll_pos = None
        self.total_count = -1
        self.count = 1
        self.ocr_price = self.price
        self.ocr_amount = self.amount
        self.catalog_row_id = None

    def __str__(self):
        name = f'{self.name}_x{self.amount}_{self.count}/{self.total_count}_{self.cost}_x{self.price}'

        if self.tag is not None:
            name = f'{name}_{self.tag}'

        return name

    def predict_valid(self):
        luma = rgb2luma(self.image)
        return np.mean(luma > 127) >= 0.3

    @property
    def scroll_pos(self):
        return self._scroll_pos

    @scroll_pos.setter
    def scroll_pos(self, value):
        self._scroll_pos = value

    def __eq__(self, other):
        return id(self) == id(other)

    def correct_name_and_cost(self):
        if self.price in UR_SHIP_PRICES_IN_URPT and self.total_count == 1:
            self.name = 'ShipUR'
            self.cost = 'URpt'
            self.is_ship = True
        elif self.price == COIN_PRICE_IN_URPT and self.total_count == 350:
            # Обмен URpt на монеты.
            self.name = 'Coin'
            self.cost = 'URpt'
        else:
            self.cost = 'pt'
            if self.price == 2000:
                if self.total_count == 10:
                    self.name = 'SkinBox'
                elif self.total_count == 4:
                    self.name = 'Meta'
                else:
                    self.name = 'EquipSSR'
            elif self.price == 8000:
                self.name = 'ShipSSR'
                self.is_ship = True
            elif self.price == 10000:
                self.name = 'EquipUR'
            elif self.price == URPT_PRICE_IN_PT and self.total_count == 500:
                self.name = 'URpt'
            elif self.name.isdigit():
                logger.warning(
                    f'[Магазин события — товар] Неопознанный товар, цена {self.price}, всего {self.total_count}; '
                    f'принадлежность не подтверждена; область={self.button}, '
                    f'позиция прокрутки={self.scroll_pos}.'
                )

    def predict_genre(self):
        self.group, self.sub_genre, self.tier = None, None, None

        # Регулярное выражение позволяет сразу заполнить новые признаки товара.
        name = self.name.lower()
        result = re.search(FILTER_REGEX, name)
        if result:
            self.group, self.sub_genre, self.tier = \
            [group.lower()
             if group is not None else None
             for group in result.groups()]


class EventShopItemGrid(ItemGrid):
    item_class = EventShopItem

    def __init__(self,
                 grids,
                 templates,
                 template_area=(0, 0, ITEM_SHAPE[0], ITEM_SHAPE[1]),
                 # Последние два пикселя иконки содержат нижнюю рамку карточки;
                 # она не относится к amount и при малом Y-сдвиге загрязняет OCR.
                 amount_area=(31, 50, ITEM_SHAPE[0], ITEM_SHAPE[1] - 2),
                 cost_area=(DELTA_PRICE[0] - DELTA_ITEM[0], DELTA_PRICE[1] - DELTA_ITEM[1],
                            DELTA_PRICE[2] - DELTA_ITEM[0], DELTA_PRICE[3] - DELTA_ITEM[1]),
                 price_area=(DELTA_PRICE_TEXT[0] - DELTA_ITEM[0], DELTA_PRICE_TEXT[1] - DELTA_ITEM[1],
                             DELTA_PRICE_TEXT[2] - DELTA_ITEM[0], DELTA_PRICE_TEXT[3] - DELTA_ITEM[1]),
                 tag_area=(DELTA_TAG[0] - DELTA_ITEM[0], DELTA_TAG[1] - DELTA_ITEM[1],
                           DELTA_TAG[2] - DELTA_ITEM[0], DELTA_TAG[3] - DELTA_ITEM[1]),
                 counter_area=(DELTA_AMOUNT[0] - DELTA_ITEM[0], DELTA_AMOUNT[1] - DELTA_ITEM[1],
                               DELTA_AMOUNT[2] - DELTA_ITEM[0], DELTA_AMOUNT[3] - DELTA_ITEM[1]),
                 ):
        super().__init__(grids, templates, template_area, amount_area, cost_area, price_area, tag_area)
        self.counter_ocr = CounterOcr([], letter=COUNTER_COLOR, name="CounterOcr")
        self.counter_area = counter_area
        self.price_ocr = PRICE_OCR
        self._catalog_spec = None
        self._allowed_template_names = None

    @staticmethod
    def _canonical_template_name(name):
        value = str(name or '')
        if '_' in value:
            prefix, suffix = value.rsplit('_', 1)
            if suffix.isdigit():
                return prefix
        return value

    @staticmethod
    def _template_similarity(image, template, *, tolerant):
        """Оценить similarity с ограниченным поиском малой трансляции иконки."""
        candidate = template
        if tolerant:
            margin = EVENT_TEMPLATE_SEARCH_MARGIN
            height, width = template.shape[:2]
            if height > margin * 2 and width > margin * 2:
                candidate = template[margin:-margin, margin:-margin]
        if image.shape[0] < candidate.shape[0] or image.shape[1] < candidate.shape[1]:
            return -1.0
        result = cv2.matchTemplate(image, candidate, cv2.TM_CCOEFF_NORMED)
        return float(cv2.minMaxLoc(result)[1])

    def set_catalog_spec(self, spec):
        """Ограничить runtime identity source-каталогом одного прохода EventShop."""
        self._catalog_spec = spec if isinstance(spec, Mapping) else None
        if self._catalog_spec is None:
            self._allowed_template_names = None
        else:
            self._allowed_template_names = catalog_template_names(self._catalog_spec)

    def _template_color_matches(self, image_color, name):
        template_color = self.colors.get(name)
        if template_color is None:
            template_color = cv2.mean(self.templates[name])[:3]
        return color_similar(color1=image_color, color2=template_color, threshold=30)

    def _best_named_catalog_template(self, image, image_color, similarity):
        """Найти уверенную именованную identity, группируя варианты одного товара."""
        by_identity = {}
        names = sorted(
            (name for name in self.templates if not name.isdigit()),
            key=lambda name: self.templates_hit.get(name, 0),
            reverse=True,
        )
        for name in names:
            identity = self._canonical_template_name(name)
            if identity not in self._allowed_template_names:
                continue
            if not self._template_color_matches(image_color, name):
                continue
            score = self._template_similarity(image, self.templates[name], tolerant=True)
            current = by_identity.get(identity)
            if current is None or score > current[0]:
                by_identity[identity] = (score, name)

        ranked = sorted(by_identity.values(), key=lambda item: item[0], reverse=True)
        if not ranked:
            return None
        best_score, best_name = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1.0
        if best_score > similarity and best_score - second_score >= EVENT_TEMPLATE_MIN_GAP:
            return best_name
        return None

    def _best_numeric_template(self, image, image_color, similarity):
        """Использовать временную identity только если именованная не доказана."""
        ranked = []
        for name in sorted(
            (name for name in self.templates if name.isdigit()),
            key=lambda name: self.templates_hit.get(name, 0),
            reverse=True,
        ):
            if not self._template_color_matches(image_color, name):
                continue
            score = self._template_similarity(image, self.templates[name], tolerant=False)
            if score > similarity:
                ranked.append((score, name))
        ranked.sort(reverse=True)
        if not ranked:
            return None
        best_score, best_name = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1.0
        if best_score - second_score < EVENT_TEMPLATE_MIN_GAP:
            return None
        return best_name

    def match_template(self, image, similarity=None):
        """Сначала доказать catalog identity, затем использовать временный fallback."""
        if self._allowed_template_names is None:
            return super().match_template(image, similarity=similarity)
        if similarity is None:
            similarity = self.similarity
        similarity = lower_template_match_similarity(similarity)
        color = cv2.mean(image)[:3]

        best_name = self._best_named_catalog_template(image, color, similarity)
        if best_name is None:
            best_name = self._best_numeric_template(image, color, similarity)
        if best_name is not None:
            self.templates_hit[best_name] += 1
            return best_name

        self.next_template_index += 1
        name = str(self.next_template_index)
        template = image.copy()
        self.colors[name] = cv2.mean(template)[:3]
        self.templates[name] = template
        self.templates_hit[name] = self.templates_hit.get(name, 0) + 1
        logger.debug(f'[Магазин события — товар] Временная numeric identity: {name}')
        return name

    def _apply_catalog_evidence(self, item):
        item.ocr_price = item.price
        item.ocr_amount = item.amount
        item.catalog_row_id = None
        if self._catalog_spec is None:
            return
        claim = resolve_catalog_claim(self._catalog_spec, item)

        resolved_price = claim.get('price')
        if isinstance(resolved_price, int) and resolved_price > 0 and item.price != resolved_price:
            logger.debug(
                '[Магазин события — товар] OCR price нормализована согласованным EventSpec: '
                f'{item.price} -> {resolved_price}'
            )
            item.price = resolved_price

        resolved_amount = claim.get('amount')
        if isinstance(resolved_amount, int) and resolved_amount > 0 and item.amount != resolved_amount:
            logger.debug(
                '[Магазин события — товар] OCR amount нормализован согласованным EventSpec: '
                f'{item.amount} -> {resolved_amount}'
            )
            item.amount = resolved_amount

        if claim.get('status') != 'matched':
            return
        source = claim.get('source')
        if not isinstance(source, Mapping):
            return
        bind_catalog_source(
            item,
            source,
            evidence=str(claim.get('identity_evidence') or 'source_key'),
        )

    def predict_tag(self, image):
        color = cv2.mean(np.array(image))[:3]
        if color_similar(color1=color, color2=(255, 72, 72), threshold=50):
            return 'unobtained'
        return None

    def predict(self, image, name=True, amount=True, cost=False, price=True, tag=True, counter=True, scroll_pos=None):
        super().predict(image, name=name, amount=amount, cost=cost, price=price, tag=tag)
        if counter and len(self.items):
            counter_list = [item.crop(self.counter_area) for item in self.items]
            counter_list = self.counter_ocr.ocr(counter_list, direct_ocr=True)
            for i, t in zip(self.items, counter_list):
                i.count, i.total_count = t

        if isinstance(scroll_pos, float) and len(self.items):
            for i in self.items:
                i.scroll_pos = scroll_pos

        for i in self.items:
            i.correct_name_and_cost()
            i.predict_genre()
            self._apply_catalog_evidence(i)

        return self.items
