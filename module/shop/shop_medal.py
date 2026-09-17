"""Обработчик магазина медалей.

С помощью шаблонного сопоставления находит значки медалей, динамически вычисляет сетку товаров,
распознаёт и фильтрует товары в магазине медалей и выполняет покупки по приоритетам конфигурации.
Использует адаптивную полосу прокрутки для перелистывания списка товаров.
"""

import cv2
import numpy as np
from scipy import signal

import module.config.server as server
from module.base.button import ButtonGrid
from module.base.decorator import cached_property, del_cached_property
from module.base.timer import Timer
from module.base.utils import rgb2gray
from module.logger import logger
from module.map_detection.utils import Points
from module.ocr.ocr import Digit, DigitYuv, Ocr
from module.shop.assets import *
from module.shop.base import ShopItemGrid_250814
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus
from module.ui.scroll import AdaptiveScroll


class ShopAdaptiveScroll(AdaptiveScroll):
    """Адаптивная полоса прокрутки магазина, определяющая позицию прокрутки по сопоставлению цвета.

    Использует поиск пиков сигнала scipy на инвертированном полутоновом изображении для нахождения ползунка,
    формируя булев массив маски позиций для определения положения прокрутки.
    """

    def match_color(self, main):
        """Сопоставить цвет полосы прокрутки через поиск пиков.

        Выполняет инвертирование оттенков серого в области полосы прокрутки, находит пики сигнала через scipy
        для определения положения ползунка и генерирует булеву маску позиций.

        Args:
            main: Экземпляр главного модуля для создания снимков и обрезки изображения

        Returns:
            np.array: Маска положения полосы прокрутки, dtype=bool
        """
        area = (self.area[0] - self.background, self.area[1], self.area[2] + self.background, self.area[3])
        image = main.image_crop(area, copy=False)

        image = rgb2gray(image)
        cv2.bitwise_not(image, dst=image)
        image = image.flatten()
        wlen = area[2] - area[0]
        parameters = {
            'height': (100, 200),
            'prominence': 35,
            'width': 1
        }
        parameters.update(self.parameters)
        peaks, _ = signal.find_peaks(image, **parameters)
        peaks = peaks[15: 123]
        peaks //= wlen
        self.length = 123
        mask = np.zeros((self.total,), dtype=np.bool_)
        mask[peaks] = 1
        return mask


MEDAL_SHOP_SCROLL_250814 = ShopAdaptiveScroll(
    MEDAL_SHOP_SCROLL_AREA_250814.button,
    background=1,
    name="MEDAL_SHOP_SCROLL_250814"
)
MEDAL_SHOP_SCROLL_250814.drag_threshold = 0.1
# Немного больше 0.1 для корректной обработки нижней границы.
MEDAL_SHOP_SCROLL_250814.edge_threshold = 0.12


class ShopPriceOcr(DigitYuv):
    """OCR-распознаватель цен магазина, исправляющий ошибки распознавания цен чертежей модернизации.

    Распознаёт цены товаров в цветовом пространстве YUV, исправляя типичную ошибку '00' -> '100'.
    """

    def after_process(self, result):
        """Постобработка OCR, исправляющая '00' на '100' (для чертежей модернизации)."""
        result = Ocr.after_process(self, result)
        # Для чертежей модернизации '100' ошибочно распознаётся как '00'.
        if result == '00':
            result = '100'
        return Digit.after_process(self, result)


PRICE_OCR = ShopPriceOcr([], letter=(255, 223, 57), threshold=32, name='Price_ocr')
if server.server == 'jp':
    PRICE_OCR_250814 = Digit([], lang='cnocr', letter=(235, 235, 255), threshold=128, name='Price_ocr')
else:
    PRICE_OCR_250814 = Digit([], letter=(255, 255, 255), threshold=128, name='Price_ocr')
TEMPLATE_MEDAL_ICON = Template('./assets/shop/cost/Medal.png')
TEMPLATE_MEDAL_ICON_2 = Template('./assets/shop/cost/Medal_2.png')
TEMPLATE_MEDAL_ICON_3 = Template('./assets/shop/cost/Medal_3.png')


class MedalShop2_250814(ShopClerk, ShopStatus):
    """Обработчик магазина медалей (новый интерфейс от 2025-08-14).

    Pages: in: page_shop (вкладка магазина медалей)
    """

    @cached_property
    def shop_filter(self):
        """Получить строку фильтра магазина медалей.

        Returns:
            str: Строка фильтра
        """
        return self.config.MedalShop2_Filter.strip()

    # Новый UI от 2025-08-14.
    def _get_medals(self):
        """Найти положение значков медалей на снимке экрана.

        С помощью шаблонного сопоставления ищет значки медалей в области магазина,
        возвращает массив координат левых верхних углов значков.

        Returns:
            np.array: [[x1, y1], [x2, y2]], координаты левых верхних углов значков медалей
        """
        area = (265, 317, 999, 635)
        # Копируем изображение для последующей отрисовки.
        image = self.image_crop(area, copy=True)
        medals = TEMPLATE_MEDAL_ICON_3.match_multi(image, similarity=0.5, threshold=5)
        medals = Points([(0., m.area[1]) for m in medals]).group(threshold=5)
        logger.attr('Количество значков медалей', len(medals))
        return medals

    def wait_until_medal_appear(self, skip_first_screenshot=True):
        """Дождаться завершения загрузки страницы магазина медалей.

        После входа в магазин медалей загрузка списка товаров требует времени;
        этот метод ожидает появления любого значка медали.

        Args:
            skip_first_screenshot: Пропускать ли первый снимок экрана
        """
        timeout = Timer(1, count=3).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            medals = self._get_medals()

            if timeout.reached():
                break
            if len(medals):
                break

    @cached_property
    def shop_grid(self):
        """Получить сетку товаров магазина медалей."""
        return self.shop_medal_grid()

    def shop_medal_grid(self):
        """Вычислить сетку магазина по расположению значков медалей.

        По количеству и положению обнаруженных значков медалей динамически вычисляет
        начало координат, интервалы и число строк сетки товаров, адаптируясь под разные серверы.

        Returns:
            ButtonGrid: Сетка товаров магазина
        """
        medals = self._get_medals()
        count = len(medals)
        if count == 0:
            logger.warning('Значки медалей не найдены; предполагается, что список товаров находится сверху')
            origin_y = 228
            delta_y = 223
            row = 2
        elif count == 1:
            y_list = medals[:, 1]
            # +317 — смещение верхней границы области обрезки (_get_medals).
            # -126 — смещение от верхней границы значка медали до верхней границы товара.
            origin_y = y_list[0] + 317 - 126
            delta_y = 223
            row = 1
        elif count == 2:
            y_list = medals[:, 1]
            y1, y2 = y_list[0], y_list[1]
            origin_y = min(y1, y2) + 317 - 126
            delta_y = abs(y1 - y2)
            row = 2
        else:
            logger.warning(f'Неожиданный результат сопоставления значков медалей: {[m for m in medals]}')
            origin_y = 228
            delta_y = 223
            row = 2

        # Создаём ButtonGrid.
        shop_grid = ButtonGrid(
            origin=(265, origin_y), delta=(169, delta_y), button_shape=(64, 64), grid_shape=(5, row), name='SHOP_GRID')
        return shop_grid

    shop_template_folder = './assets/shop/medal'

    @cached_property
    def shop_medal_items(self):
        """Загрузить шаблоны и конфигурацию товаров магазина медалей.

        Returns:
            ShopItemGrid_250814: Объект сетки товаров магазина
        """
        shop_grid = self.shop_grid
        shop_medal_items = ShopItemGrid_250814(
            shop_grid,
            templates={},
            amount_area=(60, 74, 96, 95),
            cost_area=(-12, 115, 60, 155),
            price_area=(14, 122, 85, 149),
        )
        shop_medal_items.load_template_folder(self.shop_template_folder)
        shop_medal_items.load_cost_template_folder('./assets/shop/cost')
        # Снижаем порог для стабильного сопоставления чертежей модернизации PR/DR.
        shop_medal_items.similarity = 0.85
        shop_medal_items.cost_similarity = 0.5
        shop_medal_items.price_ocr = PRICE_OCR_250814
        return shop_medal_items

    def shop_items(self) -> ShopItemGrid_250814:
        """Единый интерфейс получения сетки товаров магазина.

        Все магазины используют общее имя свойства. Переопределено для добавления
        аннотации типов под метод get_soldout_count в run().

        Returns:
            ShopItemGrid_250814: Сетка товаров магазина
        """
        return self.shop_medal_items

    def shop_currency(self):
        """OCR-распознавание количества валюты магазина медалей.

        Определяет текущий баланс медалей через проверку статуса и записывает в лог.

        Returns:
            int: Количество медалей
        """
        self._currency = self.status_get_medal()
        logger.info(f'[Магазин — медали] Медали: {self._currency}')
        return self._currency

    def shop_has_loaded(self, items):
        """Проверить, завершена ли загрузка списка товаров.

        Если присутствует товар с ценой по умолчанию 5000, магазин ещё не загружен
        и безопасная покупка невозможна.

        Args:
            items: Список товаров

        Returns:
            bool: Полностью ли загружен список товаров
        """
        for item in items:
            if int(item.price) == 5000:
                return False
        return True

    def shop_interval_clear(self):
        """Сбросить интервалы нажатий для кнопок интерфейса покупки.

        Сбрасывает состояние interval для кнопок подтверждения выбора и количества.
        """
        super().shop_interval_clear()
        self.interval_clear(SHOP_BUY_CONFIRM_SELECT)
        self.interval_clear(SHOP_BUY_CONFIRM_AMOUNT)

    def shop_buy_handle(self, item):
        """Обработать интерфейс покупки в магазине медалей.

        Распознаёт и обрабатывает интерфейсы подтверждения выбора и ввода количества.

        Args:
            item: Объект покупаемого товара

        Returns:
            bool: Обнаружен и обработан ли интерфейс покупки
        """
        if self.appear(SHOP_BUY_CONFIRM_SELECT, offset=(20, 20), interval=3):
            self.shop_buy_select_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_SELECT)
            return True
        if self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
            self.shop_buy_amount_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_AMOUNT)
            return True

        return False

    def run(self):
        """Запустить процесс покупки в магазине медалей.

        Pages: in: page_shop (вкладка магазина медалей)

        Покупает товары магазина медалей по настройкам фильтра, автоматически прокручивая страницу до конца списка.
        Распроданные товары автоматически перемещаются в конец, поэтому при их обнаружении процесс завершается досрочно.
        """
        import time
        if not self.shop_filter:
            return

        logger.hr('[Магазин — медали] Магазин медалей', level=1)
        # Выполняем покупку.
        MEDAL_SHOP_SCROLL_250814.set_top(main=self)
        time.sleep(0.5)
        while 1:
            # Распроданные товары автоматически перемещаются в конец списка; при их обнаружении продолжать не нужно.
            if self.shop_items().get_soldout_count(self.device.image):
                logger.info('Магазин медалей остановлен досрочно')
                break

            self.shop_buy()

            if MEDAL_SHOP_SCROLL_250814.at_bottom(main=self):
                logger.info('Достигнут конец магазина медалей; остановка')
                break
            else:
                MEDAL_SHOP_SCROLL_250814.next_page(main=self, page=0.66)
                del_cached_property(self, 'shop_grid')
                del_cached_property(self, 'shop_medal_items')
                continue
