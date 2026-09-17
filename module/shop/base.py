"""Базовый класс системы магазинов.

Предоставляет общий каркас для страниц магазинов, включая обнаружение предметов (OCR + сопоставление с шаблоном),
сопоставление с регулярными выражениями и сортировку фильтров, принятие решений о покупке, обработку перекрытий и т.д.
Подклассы (ShopMedal, VoucherShop и др.) переопределяют методы shop_items(),
shop_filter, shop_currency() и др. для адаптации под компоновку и тип валюты конкретного магазина.

Фильтр предметов поддерживает трёхуровневое сопоставление: group / sub_genre / tier,
охватывая ящики со снаряжением, книги навыков, чертежи модернизации, чертежи исследований, корабли и др.
"""

import re

import numpy as np

from module.base.button import ButtonGrid
from module.base.decorator import Config, cached_property
from module.base.filter import Filter
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_3, GET_SHIP
from module.logger import logger
from module.shop.assets import *
from module.shop.shop_select_globals import *
from module.statistics.item import Item, ItemGrid
from module.tactical.tactical_class import Book
from module.ui.ui import UI

FILTER_REGEX = re.compile(
    '^(array|book|box|bulin|cat'
    '|chip|coin|cube|drill|food'
    '|plate|retrofit|pr|dr|specializedcore'
    '|logger|tuning'
    '|hecombatplan|fragment|hiddenzonedatalogger'
    '|albacore|bataan|bearn|bluegill|carabiniere|casablanca|contedicavour|dukeofyork'
    '|echo|eldridge|gangut|glorious|grenville|hibiki|hunter|icarus'
    '|kawakaze|kinggeorgev|kinu|kuroshio|lagalissonniere|lemalinmuse|letemeraire|littorio'
    '|mikuma|minsk|newcastle|oyashio|quincy|ryuujou|sanjuan|sheffieldmuse'
    '|trento|u37|vincennes|z24|z26|z28|z36'
    ')'

    '(neptune|monarch|ibuki|izumo|roon|saintlouis'
    '|seattle|georgia|kitakaze|azuma|friedrich'
    '|gascogne|champagne|cheshire|drake|mainz|odin'
    '|anchorage|hakuryu|agir|august|marcopolo'
    '|plymouth|rupprecht|harbin|chkalov|brest'
    '|red|blue|yellow'
    '|general|gun|torpedo|antiair|plane|wild'
    '|dd|cl|bb|cv'
    '|iris|sardegna'
    '|abyssal|archive|obscure|unlock'
    '|combat|offense|survival)?'

    '(s[1-5]|t[1-6])?$',
    flags=re.IGNORECASE)
FILTER_ATTR = ('group', 'sub_genre', 'tier')
FILTER = Filter(FILTER_REGEX, FILTER_ATTR)


class ShopItem_250814(Item):
    """Товар магазина новой версии от 2025-08-14 с проверкой состояния распродажи.

    Определяет, распродан ли товар, по яркости пикселей. Порог 0.3:
    средняя яркость нераспроданных товаров > 0.36, распроданных < 0.2.

    Attributes:
        Без дополнительных атрибутов, наследует Item.
    """

    def predict_valid(self):
        mean = np.mean(np.max(self.image, axis=2) > 139)
        return mean > 0.3


class ShopItemGrid(ItemGrid):
    """Сетка товаров магазина, расширяющая базовый класс атрибутами для регулярного фильтра.

    Для каждого Item добавляет три атрибута: group, sub_genre, tier,
    используемых FILTER для сопоставления и сортировки. Для книг навыков выполняется
    шаблонное сопоставление для исправления ошибок распознавания цвета и ранга.
    """

    def predict(self, image, name=True, amount=True, cost=False, price=False, tag=False):
        """
        Распознать товары магазина и заполнить расширенные атрибуты, необходимые для фильтрации.

        Поверх результатов базового распознавания с помощью регулярных выражений парсит имя товара
        и добавляет каждому Item атрибуты group, sub_genre, tier для FILTER.
        Для книг навыков дополнительно выполняет сопоставление с шаблоном для коррекции цвета и ранга.

        Args:
            image: Снимок экрана магазина.
            name: Распознавать ли название товара.
            amount: Распознавать ли количество товара.
            cost: Распознавать ли тип стоимости товара.
            price: Распознавать ли цену товара.
            tag: Распознавать ли тег товара.

        Returns:
            list[Item]: Список товаров с расширенными атрибутами.
        """
        super().predict(image, name, amount, cost, price, tag)
        for item in self.items:
            # Устанавливаем значения по умолчанию
            item.group, item.sub_genre, item.tier = None, None, None

            # Быстро заполняем новые атрибуты с помощью регулярного выражения
            name = item.name
            result = re.search(FILTER_REGEX, name)
            if result:
                item.group, item.sub_genre, item.tier = \
                [group.lower()
                 if group is not None else None
                 for group in result.groups()]
            else:
                continue

            # Цвет и/или уровень книги иногда распознаются неверно
            # Выполняем повторное сопоставление шаблона через класс Book
            if item.group == 'book':
                book = Book(image, item._button)
                if item.sub_genre is not None:
                    item.sub_genre = book.genre_str
                item.tier = book.tier_str.lower()
                item.name = ''.join(
                    [part.title()
                     if part is not None
                     else ''
                     for part in [item.group, item.sub_genre, item.tier]])

        return self.items


class ShopItemGrid_250814(ShopItemGrid):
    """Сетка товаров магазина новой версии от 2025-08-14, использующая ShopItem_250814 в качестве класса товара.

    Добавляет функцию подсчёта распроданных товаров, разделяя доступные и распроданные товары по яркости пикселей.
    """

    item_class = ShopItem_250814

    def get_soldout_count(self, image):
        """
        Подсчитать количество распроданных товаров в магазине.

        Обходит все позиции сетки и по проверке валидности ShopItem_250814
        различает распроданные и доступные товары. У распроданных товаров яркость пикселей ниже,
        и is_valid возвращает False.

        Args:
            image: Снимок экрана магазина.

        Returns:
            int: Количество распроданных товаров.
        """
        super().predict(image, name, amount, cost, price, tag)
        for item in self.items:
            # Устанавливаем значения по умолчанию
            item.group, item.sub_genre, item.tier = None, None, None

            # Быстро заполняем новые атрибуты с помощью регулярного выражения
            name = item.name
            result = re.search(FILTER_REGEX, name)
            if result:
                item.group, item.sub_genre, item.tier = \
                [group.lower()
                 if group is not None else None
                 for group in result.groups()]
            else:
                continue

            # Цвет и/или уровень книги иногда распознаются неверно
            # Выполняем повторное сопоставление шаблона через класс Book
            if item.group == 'book':
                book = Book(image, item._button)
                if item.sub_genre is not None:
                    item.sub_genre = book.genre_str
                item.tier = book.tier_str.lower()
                item.name = ''.join(
                    [part.title()
                     if part is not None
                     else ''
                     for part in [item.group, item.sub_genre, item.tier]])

        return self.items


class ShopItemGrid_250814(ShopItemGrid):
    """Новая версия сетки товаров магазина (2025-08-14), использующая ShopItem_250814 в качестве класса товара.

    Добавляет подсчёт распроданных товаров через различие в яркости пикселей между доступными и распроданными товарами.
    """

    item_class = ShopItem_250814

    def get_soldout_count(self, image):
        """
        Подсчитать количество распроданных товаров в магазине.

        Обходит все ячейки сетки, определяя доступность товаров через валидность
        ShopItem_250814. У распроданных товаров яркость пикселей ниже, поэтому
        is_valid возвращает False.

        Args:
            image: Снимок экрана магазина.

        Returns:
            int: Количество распроданных товаров.
        """
        count = 0
        for button in self.grids.buttons:
            item = self.item_class(image, button)
            if not item.is_valid:
                count += 1
        logger.attr('Количество распроданных товаров', count)
        return count


class ShopBase(UI):
    """
    Базовый класс системы магазинов.

    Предоставляет общий каркас для обнаружения, фильтрации и принятия решений о покупке товаров.
    Подклассы переопределяют методы shop_items(), shop_filter, shop_currency() и др.
    для адаптации под компоновку и тип валюты конкретного магазина.

    Pages: in: page_shop
    """
    _currency = 0
    shop_template_folder = ''

    @cached_property
    def shop_filter(self):
        """
        Получить строку фильтра товаров магазина.

        Подклассы переопределяют это свойство для задания правил фильтрации конкретного магазина.
        Формат определяется FILTER_REGEX и поддерживает трёхуровневое сопоставление: group/sub_genre/tier.

        Returns:
            str: Строка фильтра магазина; пустая строка означает отсутствие фильтрации.
        """
        return ''

    @cached_property
    @Config.when(SERVER=None)
    def shop_grid(self):
        """
        Получить расположение сетки товаров магазина.

        Компоновка нового интерфейса от 2025-08-14: сетка 5x2.
        Конфигурация серверов может переопределять это свойство для адаптации под разные компоновки.

        Returns:
            ButtonGrid: Сетка товаров магазина, каждая ячейка 64x64 пикселей.
        """
        shop_grid = ButtonGrid(
            origin=(265, 238), delta=(169, 223), button_shape=(64, 64), grid_shape=(5, 2), name='SHOP_GRID')
        return shop_grid

    def shop_items(self):
        """
        Получить объект сетки товаров текущего магазина.

        Базовый класс возвращает None; подклассы обязаны переопределить для возврата реального экземпляра ShopItemGrid.

        Returns:
            ShopItemGrid | None: Сетка товаров магазина, по умолчанию None.
        """
        return None

    def shop_currency(self):
        """
        Получить текущее количество имеющейся валюты магазина.

        Подклассы могут переопределять этот метод для считывания фактического значения через OCR или иные способы.

        Returns:
            int: Текущее количество валюты.
        """
        return self._currency

    def shop_has_loaded(self, items):
        """
        Пользовательский шаг проверки завершения загрузки для подклассов магазинов.

        Например, ShopMedal изначально отображает товары и цены по умолчанию
        и требует ожидания загрузки фактических данных. Базовый класс возвращает True.

        Args:
            items: Список текущих обнаруженных товаров.

        Returns:
            bool: Полностью ли загрузился магазин.
        """
        return True

    def shop_detect_items(self, image=None):
        """
        Распознать товары магазина на изображении (для целей тестирования).

        Выполняет распознавание товаров на указанном снимке экрана и выводит результаты построчно в лог.
        Поддерживает режим извлечения шаблонов (SHOP_EXTRACT_TEMPLATE).

        Args:
            image: Снимок экрана магазина; если None, используется текущий снимок с устройства.

        Returns:
            list[Item]: Список обнаруженных товаров; пустой список, если товары не найдены.
        """
        if image is None:
            image = self.device.image

        # Получаем ShopItemGrid
        shop_items = self.shop_items()
        if shop_items is None:
            logger.warning('Ожидался ShopItemGrid, но получен None')
            return []

        if self.config.SHOP_EXTRACT_TEMPLATE:
            if self.shop_template_folder:
                logger.info(f'Извлечение шаблонов товаров в {self.shop_template_folder}')
                shop_items.extract_template(image, self.shop_template_folder)
            else:
                logger.warning('SHOP_EXTRACT_TEMPLATE включён, но shop_template_folder не задан; извлечение пропущено')

        shop_items.predict(
            image,
            name=True,
            amount=False,
            cost=True,
            price=True,
            tag=False
        )

        # Записываем итоговые результаты распознавания товаров
        items = shop_items.items
        grids = shop_items.grids
        if len(items):
            min_row = grids[0, 0].area[1]
            row = [str(item) for item in items if item.button[1] == min_row]
            logger.info(f'[Магазин] Ряд 1: {row}')
            row = [str(item) for item in items if item.button[1] != min_row]
            logger.info(f'[Магазин] Ряд 2: {row}')
            return items
        else:
            logger.info('Товары магазина не найдены')
            return []

    def shop_obstruct_handle(self):
        """
        Убрать перекрытия из области обзора магазина (при их наличии).

        Обрабатывает всплывающие окна получения корабля, получения предметов, подтверждения блокировки и др.,
        выполняя клик по безопасной зоне для закрытия перекрывающего слоя. Используется в цикле снимков shop_get_items.

        Returns:
            bool: Присутствовало и было ли обработано перекрытие.
        """
        # Обрабатываем перекрывающие магазин элементы
        if self.appear(GET_SHIP, interval=1):
            logger.info(f'Перекрытие магазина: {GET_SHIP} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        # Блокировка нового полученного корабля
        if self.handle_popup_confirm('SHOP_OBSTRUCT'):
            return True
        if self.appear(GET_ITEMS_1, interval=1):
            logger.info(f'Перекрытие магазина: {GET_ITEMS_1} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_3, interval=1):
            logger.info(f'Перекрытие магазина: {GET_ITEMS_3} -> {SHOP_CLICK_SAFE_AREA}')
            self.device.click(SHOP_CLICK_SAFE_AREA)
            return True

        return False

    def shop_get_items(self, skip_first_screenshot=True):
        """
        Получить все товары на текущей странице магазина.

        Через цикл скриншотов и распознавания ожидает полной загрузки товаров, попутно закрывая перекрывающие окна.
        Завершается, когда количество распознанных товаров стабилизировалось и shop_has_loaded вернул True.

        Args:
            skip_first_screenshot: Пропускать ли первый снимок экрана, повторно используя снимок предыдущего цикла состояний.

        Returns:
            list[Item]: Список загруженных товаров; пустой список, если товаров нет.
        """
        # Получаем ShopItemGrid
        shop_items = self.shop_items()
        if shop_items is None:
            logger.warning('Ожидался ShopItemGrid, но получен None')
            return []

        # Повторяем распознавание, чтобы убедиться, что товары загрузились и читаются корректно
        record = 0
        timeout = Timer(3, count=9).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.shop_obstruct_handle():
                timeout.reset()
                continue

            if self.config.SHOP_EXTRACT_TEMPLATE:
                if self.shop_template_folder:
                    logger.info(f'Извлечение шаблонов товаров в {self.shop_template_folder}')
                    shop_items.extract_template(self.device.image, self.shop_template_folder)
                else:
                    logger.warning('SHOP_EXTRACT_TEMPLATE включён, но shop_template_folder не задан; извлечение пропущено')

            shop_items.predict(
                self.device.image,
                name=True,
                amount=False,
                cost=True,
                price=True,
                tag=False
            )

            if timeout.reached():
                logger.warning('Тайм-аут загрузки товаров; продолжаем, предполагая, что загрузка завершена')
                break

            # Проверяем незагруженные товары: игра загружает их довольно медленно
            items = shop_items.items
            known = len([item for item in items if item.is_known_item])
            logger.attr('Обнаружено товаров', known)
            if known == 0 or known != record:
                record = known
                continue
            else:
                record = known

            # Условие завершения
            if self.shop_has_loaded(items):
                break

        # Записываем итоговые результаты распознавания товаров
        items = shop_items.items
        grids = shop_items.grids
        if len(items):
            min_row = grids[0, 0].area[1]
            row = [str(item) for item in items if item.button[1] == min_row]
            logger.info(f'[Магазин] Ряд 1: {row}')
            row = [str(item) for item in items if item.button[1] != min_row]
            logger.info(f'[Магазин] Ряд 2: {row}')
            return items
        else:
            logger.info('Товары магазина не найдены')
            return []

    def shop_check_item(self, item):
        """
        Проверить, удовлетворяет ли товар условиям покупки (достаточно ли валюты).

        Переопределяется в подклассах для реализации особой логики проверки товаров,
        такой как дополнительные лимиты запаса, приоритеты и т.д. Вызывается фильтром FILTER.apply.

        Args:
            item: Проверяемый товар.

        Returns:
            bool: Можно ли купить товар.
        """
        if item.price > self._currency:
            return False
        return True

    def shop_check_custom_item(self, item):
        """
        Переопределяется в подклассах для реализации кастомной логики проверки товаров вне ограничений строки фильтра.

        Выполняется в shop_get_item_to_buy с приоритетом перед проверкой фильтром;
        подходит для особых товаров, которые нельзя сопоставить регулярным выражением по имени.

        Args:
            item: Проверяемый товар.

        Returns:
            bool: Можно ли купить товар.
        """
        return False

    def shop_get_item_to_buy(self, items):
        """
        Выбрать следующий товар для покупки из списка товаров.

        Сначала проверяет кастомные товары (без поддержки шаблонов/фильтров),
        затем применяет строку фильтра для сопоставления и сортировки, возвращая товар с наивысшим приоритетом.

        Args:
            items: Список товаров, полученный из shop_get_items.

        Returns:
            Item: Товар для покупки; None, если подходящих товаров нет.
        """
        # Сначала просматриваем пользовательские товары, поскольку для них нет поддержки шаблонов или фильтра
        for item in items:
            if self.shop_check_custom_item(item):
                return item

        # Затем загружаем выбор, применяем фильтр и возвращаем первый товар из результата
        FILTER.load(self.shop_filter)
        filtered = FILTER.apply(items, self.shop_check_item)

        if not filtered:
            return None
        logger.attr('Сортировка товаров', ' > '.join([str(item) for item in filtered]))

        return filtered[0]
