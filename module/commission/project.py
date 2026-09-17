"""Модуль парсинга информации о комиссиях (поручениях).

Отвечает за извлечение всех атрибутов отдельной комиссии из снимка интерфейса поручений,
включая OCR-распознавание названия, сопоставление типа комиссии, определение длительности выполнения,
статуса и извлечение изображения суффикса.

Основной класс Commission инкапсулирует всю информацию о поручении и использует декоратор
@Config.when для раздельной реализации логики парсинга под серверы CN, EN, JP и TW.

В модуле также определён экземпляр фильтра COMMISSION_FILTER, используемый для фильтрации
и сортировки списка комиссий на основе пользовательских правил (например, 'daily_resource-01:30').

Зависимости:
    - module.base.filter: механизм фильтрации по регулярным выражениям
    - module.ocr.ocr: OCR-распознавание текста (Duration, Ocr)
    - module.commission.project_data: словари названий комиссий для каждого сервера
"""

from datetime import timedelta

from module.base.decorator import Config
from module.base.filter import Filter
from module.base.utils import *
from module.commission.project_data import *
from module.config.time_source import now as current_time
from module.logger import logger
from module.ocr.ocr import Duration, Ocr
from module.reward.assets import *

COMMISSION_FILTER = Filter(
    regex=re.compile(
        r'(major|daily|extra|urgent|night)?'
        r'-?'
        r'(resource|chip|event|drill|part|cube|oil|book|retrofit|box|gem|ship)?'
        r'-?'
        r'(\d\d?:\d\d)?'
        r'(\d\d?.\d\d?|\d\d?)?'
    ),
    attr=('category_str', 'genre_str', 'duration_hm', 'duration_hour'),
    preset=('shortest', 'expire')
)


def crop_suffix_image(image, area):
    """Обрезает изображение суффикса с римской цифрой справа от названия комиссии.

    Args:
        image: Снимок экрана игры.
        area: Область названия комиссии.

    Returns:
        Вырезанное изображение суффикса (чёрный текст на белом фоне) либо None, если текст не обнаружен.
    """
    name_image = crop(image, area)
    name_image = extract_letters(name_image, letter=(255, 255, 255), threshold=128).astype(np.uint8)

    line = cv2.reduce(name_image[5:-5, :], 0, cv2.REDUCE_AVG).flatten()
    columns = np.where(line < 250)[0]
    if not len(columns):
        return None

    # Идём влево от крайнего правого символа, стараясь целиком захватить суффикс с римской цифрой.
    threshold = 250
    look_back = 10
    for i in range(columns[-1], 0, -1):
        if line[i] > threshold:
            if columns[-1] - i > look_back:
                look_back = columns[-1] - i
                break

    left = columns[-1] - look_back
    right = columns[-1] + 1
    x1, y1 = area[0:2]
    suffix_area = area_offset((left - 3, -3, right + 3, name_image.shape[0] + 3), (x1, y1))
    image = crop(image, suffix_area)
    image = extract_letters(image, letter=(255, 255, 255), threshold=128).astype(np.uint8)
    return image


def image_hash(image):
    """Вычисляет MD5-хеш изображения для вывода в журнал.

    Args:
        image: Входное изображение.

    Returns:
        MD5-хеш изображения в виде строки либо пустая строка, если изображение равно None.
    """
    if image is None:
        return ''

    import hashlib
    return hashlib.md5(image.tobytes()).hexdigest()


class Commission:
    """Информация об отдельной комиссии.

    Инкапсулирует все атрибуты, извлечённые из снимка экрана интерфейса поручений: название, тип, статус, длительность и др.
    Поддерживает серверы CN, EN, JP, TW, распределяя специфичную логику парсинга через декоратор `@Config.when`.
    """

    # Кнопка входа в детали комиссии
    button: Button
    # Название комиссии, распознанное OCR
    name: str
    # Успешно ли разобрано название комиссии
    valid: bool
    # Вырезанное изображение суффикса: чёрный текст на белом фоне; None при отсутствии суффикса
    suffix_image: np.ndarray
    # Хеш изображения суффикса только для логирования; пустая строка при отсутствии суффикса
    suffix_hash: str
    # Название типа комиссии, определённое в project_data.py
    # Значения: major_comm, daily_resource, urgent_cube, ...
    genre: str
    # Состояние комиссии
    # Значения: finished, running, pending
    status: str
    # Длительность выполнения комиссии
    duration: timedelta
    # Время до истечения; задаётся только для срочных комиссий, иначе None
    expire: timedelta
    # Категория для фильтра
    # Значения: major|daily|extra|urgent|night
    category_str: str
    # Тип для фильтра
    # Значения: resource|chip|event|drill|part|cube|oil|book|retrofit|box|gem|ship
    genre_str: str
    # Длительность в часах, например 0.5, 1, 1.16, 2.5
    duration_hour: str
    # Длительность в формате HH:MM, например 1:30, 1:45, 2:00, 8:00, 12:00
    duration_hm: str

    def __init__(self, image, y, config):
        """Парсит информацию о комиссии из снимка экрана.

        Определяет область обрезки элемента комиссии по координате y, вызывает commission_parse
        для распознавания атрибутов и рассчитывает поля категории и длительности для фильтра.

        Args:
            image: Снимок экрана игры.
            y: Y-координата нижней границы полосы комиссии.
            config: Объект конфигурации AzurPilot.
        """
        self.config = config
        self.y = y
        self.area = (188, y - 119, 1199, y)
        self.image = image
        self.valid = True
        self.commission_parse()

        if not self.duration.total_seconds():
            self.valid = False

        self.create_time = current_time()
        self.repeat_count = 1
        self.category_str = 'unknown'
        self.genre_str = 'unknown'
        self.duration_hour = 'unknown'
        self.duration_hm = 'unknown'
        if self.valid:
            self.category_str, self.genre_str = self.genre.split('_', 1)
            self.duration_hour = str(int(self.duration.total_seconds() / 36) / 100).strip('.0')
            self.duration_hm = str(self.duration).rsplit(':', 1)[0]

    @Config.when(SERVER='en')
    def commission_parse(self):
        """Парсит информацию о комиссии (сервер EN).

        На EN-сервере названия поручений длиннее, а область OCR отличается от CN.
        Исправляет распространённые ошибки OCR (например, DALY -> DAILY).

        Распознаваемые данные: название, суффикс, длительность, время до истечения, статус.
        """
        # Распознавание названия: на EN-сервере названия длиннее, поэтому используем более широкую область обрезки
        area = area_offset((131, 23, 430, 53), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='COMMISSION')
        ocr = Ocr(button, lang='azur_lane')
        self.button = button
        result = ocr.ocr(self.image).upper()
        # Исправляем типичные ошибки OCR
        result = result.replace('DALY', 'DAILY')
        result = result.replace('NVB', 'NYB')
        result = result.replace('PYEIN', 'VEIN').replace('YEIN', 'VEIN')
        self.name = result
        self.genre = self.commission_name_parse(self.name)

        # Распознавание изображения суффикса
        self.suffix_image = crop_suffix_image(self.image, self.button.area)
        self.suffix_hash = image_hash(self.suffix_image)

        # Длительность выполнения
        area = area_offset((290, 68, 390, 95), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='DURATION')
        ocr = Duration(button)
        self.duration = ocr.ocr(self.image)

        # Время до истечения — только у срочных комиссий
        area = area_offset((-49, 68, -45, 84), self.area[0:2])
        button = Button(area=area, color=(189, 65, 66),
                        button=area, name='IS_URGENT')
        if button.appear_on(self.image, threshold=30):
            area = area_offset((-49, 67, 45, 94), self.area[0:2])
            button = Button(area=area, color=(), button=area, name='EXPIRE')
            ocr = Duration(button)
            self.expire = ocr.ocr(self.image)
        else:
            self.expire = timedelta(seconds=0)

        # Распознавание состояния по цветовым каналам RGB
        area = area_offset((179, 71, 187, 93), self.area[0:2])
        dic = {
            0: 'finished',
            1: 'running',
            2: 'pending'
        }
        color = np.array(get_color(self.image, area))
        if self.genre == 'daily_event':
            color -= [50, 30, 20]
        self.status = dic[int(np.argmax(color))]

    @Config.when(SERVER='jp')
    def commission_parse(self):
        """Парсит информацию о комиссии (сервер JP).

        OCR на сервере JP использует японскую модель и исправляет ошибки распознавания аббревиатур фракций.
        Распознаваемые данные: название, суффикс, длительность, время до истечения, статус.
        """
        # Распознавание названия
        area = area_offset((176, 23, 420, 53), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='COMMISSION')
        ocr = Ocr(button, letter=(201, 201, 201), lang='jp')
        self.button = button
        result = ocr.ocr(self.image).upper()
        # Исправляем сокращения фракций: NB -> NYB, BW -> BIW
        result = result.replace('NB', 'BYB').replace('BW', 'BIW')
        self.name = result
        self.genre = self.commission_name_parse(self.name)

        # Распознавание изображения суффикса
        self.suffix_image = crop_suffix_image(self.image, self.button.area)
        self.suffix_hash = image_hash(self.suffix_image)

        # Длительность выполнения
        area = area_offset((290, 68, 390, 95), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='DURATION')
        ocr = Duration(button)
        self.duration = ocr.ocr(self.image)

        # Время до истечения — только у срочных комиссий
        area = area_offset((-49, 68, -45, 84), self.area[0:2])
        button = Button(area=area, color=(189, 65, 66),
                        button=area, name='IS_URGENT')
        if button.appear_on(self.image, threshold=30):
            area = area_offset((-49, 67, 45, 94), self.area[0:2])
            button = Button(area=area, color=(), button=area, name='EXPIRE')
            ocr = Duration(button)
            self.expire = ocr.ocr(self.image)
        else:
            self.expire = timedelta(seconds=0)

        # Распознавание состояния по цветовым каналам RGB
        area = area_offset((179, 71, 187, 93), self.area[0:2])
        dic = {
            0: 'finished',
            1: 'running',
            2: 'pending'
        }
        color = np.array(get_color(self.image, area))
        if self.genre == 'daily_event':
            color -= [50, 30, 20]
        self.status = dic[int(np.argmax(color))]

    @Config.when(SERVER='tw')
    def commission_parse(self):
        """Парсит информацию о комиссии (сервер TW).

        OCR на традиционном китайском для TW исправляет специфические ошибки распознавания символов.
        Распознаваемые данные: название, суффикс, длительность, время до истечения, статус.
        """
        # Распознавание названия
        area = area_offset((176, 23, 420, 53), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='COMMISSION')
        ocr = Ocr(button, lang='tw', threshold=256)
        self.button = button
        result = ocr.ocr(self.image).upper()
        # В обучающем наборе нет иероглифа "艦"; он заменён на "鑑"/"盤", после чего исправляется здесь
        result = result.replace('鑑', '艦').replace('盤', '艦')
        # Исправляем "支援土蒙爾島" -> "支援土豪爾島"
        result = result.replace('土蒙爾', '土豪爾')
        # Исправляем "资源原" -> "资源"
        result = result.replace('源原', '源')
        self.name = result
        self.genre = self.commission_name_parse(self.name)

        # Распознавание изображения суффикса
        self.suffix_image = crop_suffix_image(self.image, self.button.area)
        self.suffix_hash = image_hash(self.suffix_image)

        # Длительность выполнения
        area = area_offset((290, 68, 390, 95), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='DURATION')
        ocr = Duration(button)
        self.duration = ocr.ocr(self.image)

        # Время до истечения — только у срочных комиссий
        area = area_offset((-49, 68, -45, 84), self.area[0:2])
        button = Button(area=area, color=(189, 65, 66),
                        button=area, name='IS_URGENT')
        if button.appear_on(self.image, threshold=30):
            area = area_offset((-49, 67, 45, 94), self.area[0:2])
            button = Button(area=area, color=(), button=area, name='EXPIRE')
            ocr = Duration(button)
            self.expire = ocr.ocr(self.image)
        else:
            self.expire = timedelta(seconds=0)

        # Распознавание состояния по цветовым каналам RGB
        area = area_offset((179, 71, 187, 93), self.area[0:2])
        dic = {
            0: 'finished',
            1: 'running',
            2: 'pending'
        }
        color = np.array(get_color(self.image, area))
        if self.genre == 'daily_event':
            color -= [50, 30, 20]
        self.status = dic[int(np.argmax(color))]

    @Config.when(SERVER=None)
    def commission_parse(self):
        """Парсит информацию о комиссии (сервер CN, стандартный fallback).

        На сервере CN также вырезается изображение суффикса справа от названия для сопоставления по сходству.
        Распознаваемые данные: название, суффикс, длительность, время до истечения, статус.
        """
        # Распознавание названия
        area = area_offset((176, 23, 420, 53), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='COMMISSION')
        ocr = Ocr(button, lang='cnocr', threshold=256)
        self.button = button
        result = ocr.ocr(self.image).upper()
        # Исправляем "资源原" -> "资源"
        result = result.replace('源原', '源')
        self.name = result
        self.genre = self.commission_name_parse(self.name)

        # Распознавание изображения суффикса
        self.suffix_image = crop_suffix_image(self.image, self.button.area)
        self.suffix_hash = image_hash(self.suffix_image)

        # Длительность выполнения
        area = area_offset((290, 68, 390, 95), self.area[0:2])
        button = Button(area=area, color=(), button=area, name='DURATION')
        ocr = Duration(button)
        self.duration = ocr.ocr(self.image)

        # Время до истечения — только у срочных комиссий
        area = area_offset((-49, 68, -45, 84), self.area[0:2])
        button = Button(area=area, color=(189, 65, 66),
                        button=area, name='IS_URGENT')
        if button.appear_on(self.image, threshold=30):
            area = area_offset((-49, 67, 45, 94), self.area[0:2])
            button = Button(area=area, color=(), button=area, name='EXPIRE')
            ocr = Duration(button)
            self.expire = ocr.ocr(self.image)
        else:
            self.expire = timedelta(seconds=0)

        # Распознавание состояния по цветовым каналам RGB
        area = area_offset((179, 71, 187, 93), self.area[0:2])
        dic = {
            0: 'finished',
            1: 'running',
            2: 'pending'
        }
        color = np.array(get_color(self.image, area))
        if self.genre == 'daily_event':
            color -= [50, 30, 20]
        self.status = dic[int(np.argmax(color))]

    def __str__(self):
        """Возвращает читаемое строковое представление комиссии, включая название, тип, статус и длительность."""
        name = f'{self.name} | {self.suffix_hash}' if self.suffix_hash else self.name
        if not self.valid:
            return f'{name} (Invalid)'
        info = {'Genre': self.genre, 'Status': self.status, 'Duration': self.duration}
        if self.expire:
            info['Expire'] = self.expire
        if self.repeat_count > 1:
            info['Repeat'] = self.repeat_count
        info = ', '.join([f'{k}: {v}' for k, v in info.items()])
        return f'{name} ({info})'

    def __eq__(self, other):
        """Определяет, являются ли две комиссии одной и той же.

        Выполняет комплексное сравнение по типу, статусу, суффиксу, длительности (допускается погрешность 120 секунд),
        времени до истечения и счётчику повторений. Для срочных комиссий с ящиками также сопоставляются теги фракций (NYB/BIW).

        Args:
            other: Сравниваемый объект комиссии.

        Returns:
            bool: Являются ли комиссии одинаковыми.
        """
        if not isinstance(other, Commission):
            return False
        threshold = timedelta(seconds=120)
        if not self.valid or not other.valid:
            return False
        if self.genre != other.genre or self.status != other.status:
            return False
        if self.category_str == 'daily':
            if not self.suffix_match(other):
                return False
        if self.genre == 'urgent_box':
            for tag in ['NYB', 'BIW']:
                if tag in self.name.upper() and tag not in other.name.upper():
                    return False
                if tag not in self.name.upper() and tag in other.name.upper():
                    return False
        if (other.duration < self.duration - threshold) or (other.duration > self.duration + threshold):
            return False
        if (not self.expire and other.expire) or (self.expire and not other.expire):
            return False
        if self.expire and other.expire:
            if (other.expire < self.expire - threshold) or (other.expire > self.expire + threshold):
                return False
        if self.repeat_count != other.repeat_count:
            return False
        if self.genre in ['extra_oil', 'night_oil'] and not self.suffix_match(other):
            return False

        return True

    def __hash__(self):
        """Возвращает хеш-значение комиссии на основе её типа и названия."""
        return hash(f'{self.genre}_{self.name}')

    def suffix_match(self, other, similarity=0.75):
        """Определяет, совпадают ли изображения суффиксов двух комиссий.

        Args:
            other: Сравниваемый объект комиссии.
            similarity: Порог схожести в диапазоне от 0 до 1.

        Returns:
            bool: Совпадают ли суффиксы.
        """
        if self.suffix_image is None and other.suffix_image is None:
            return True
        if self.suffix_image is None or other.suffix_image is None:
            return False

        def match(image, template):
            template = crop(template, (3, 3, template.shape[1] - 3, template.shape[0] - 3), copy=False)
            if image.shape[0] < template.shape[0] or image.shape[1] < template.shape[1]:
                return 0.0

            res = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
            _, sim, _, _ = cv2.minMaxLoc(res)
            return sim

        sim = max(
            match(self.suffix_image, other.suffix_image),
            match(other.suffix_image, self.suffix_image)
        )
        return sim >= similarity

    def parse_time(self, string):
        """Преобразует строку времени в объект timedelta.

        Args:
            string: Строка времени формата '01:00:00', '05:47:10', '17:50:51'.

        Returns:
            Экземпляр timedelta либо None при ошибке парсинга.
        """
        # OCR часто распознаёт 0 как D; исправляем это здесь
        string = string.replace('D', '0')
        result = re.search(r'(\d+):(\d+):(\d+)', string)
        if not result:
            logger.warning(f'Некорректная строка времени: {string}')
            self.valid = False
            return None
        else:
            result = [int(s) for s in result.groups()]
            return timedelta(hours=result[0], minutes=result[1], seconds=result[2])

    @Config.when(SERVER='en')
    def commission_name_parse(self, string):
        """Сопоставляет название комиссии с её типом (сервер EN).

        Сначала проверяет, является ли комиссия событием, затем перебирает словарь EN-названий по ключевым словам.

        Args:
            string: Название комиссии, например 'DAILY RESOURCE EXTRACTION'.

        Returns:
            Строка с типом комиссии (например, 'urgent_gem') либо пустая строка, если тип не распознан.
        """
        if self.is_event_commission():
            return 'daily_event'
        for key, value in dictionary_en.items():
            for keyword in value:
                if keyword in string:
                    return key

        logger.warning(f'Неизвестный тип названия: {string}')
        self.valid = False
        return ''

    @Config.when(SERVER='jp')
    def commission_name_parse(self, string):
        """Сопоставляет название комиссии с её типом (сервер JP).

        Использует расстояние Левенштейна для нечёткого сопоставления с допуском до 2 ошибочных символов OCR.
        Сначала проверяет, является ли комиссия событием, затем перебирает словарь JP-названий.

        Args:
            string: Название комиссии, например 'Short-distance Practice'.

        Returns:
            Строка с типом комиссии (например, 'extra_drill') либо пустая строка, если тип не распознан.
        """
        if self.is_event_commission():
            return 'daily_event'
        import jellyfish
        min_key = ''
        min_distance = 100
        # Удаляем ASCII-символы, оставляя для сопоставления только японские символы
        string = re.sub(r'[\x00-\x7F]', '', string)
        for key, value in dictionary_jp.items():
            for keyword in value:
                distance = jellyfish.levenshtein_distance(keyword, string)
                if distance < min_distance:
                    min_key = key
                    min_distance = distance
        if min_distance < 3:
            return min_key

        logger.warning(f'Неизвестный тип названия: {string}')
        self.valid = False
        return ''

    @Config.when(SERVER='tw')
    def commission_name_parse(self, string):
        """Сопоставляет название комиссии с её типом (сервер TW).

        Сначала проверяет, является ли комиссия событием, затем перебирает словарь TW-названий по ключевым словам.

        Args:
            string: Название комиссии, например 'Daily Resource Extraction'.

        Returns:
            Строка с типом комиссии (например, 'daily_resource') либо пустая строка, если тип не распознан.
        """
        if self.is_event_commission():
            return 'daily_event'
        for key, value in dictionary_tw.items():
            for keyword in value:
                if keyword in string:
                    return key

        logger.warning(f'Неизвестный тип названия: {string}')
        self.valid = False
        return ''

    @Config.when(SERVER=None)
    def commission_name_parse(self, string):
        """Сопоставляет название комиссии с её типом (сервер CN, стандартный fallback).

        Сначала проверяет, является ли комиссия событием, затем перебирает словарь CN-названий по ключевым словам.

        Args:
            string: Название комиссии, например 'NYB VIP Escort'.

        Returns:
            Строка с типом комиссии (например, 'urgent_gem') либо пустая строка, если тип не распознан.
        """
        if self.is_event_commission():
            return 'daily_event'
        for key, value in dictionary_cn.items():
            for keyword in value:
                if keyword in string:
                    return key

        logger.warning(f'Неизвестный тип названия: {string}')
        self.valid = False
        return ''

    def is_event_commission(self):
        """Определяет, относится ли комиссия к событию.

        Проверяет цвет области в левой части полосы комиссии. Различные события используют разные цветовые маркеры;
        в настоящее время в качестве критерия используется розово-жёлтый градиент события курорта (2023.04.27).

        Returns:
            bool: Является ли комиссия ивентовой.
        """
        # Текущая комиссия события: розово-жёлтый градиент (стиль повтора Resort / события Idol Master)
        area = area_offset((5, 5, 30, 30), self.area[0:2])
        if color_similar(color1=get_color(self.image, area), color2=(235, 173, 161), threshold=30):
            return True

        return False

    def convert_to_night(self):
        """Преобразует комиссию категории extra в категорию night."""
        if self.valid and self.category_str == 'extra':
            self.category_str = 'night'
            self.genre = f'{self.category_str}_{self.genre_str}'

    def convert_to_running(self):
        """Устанавливает статус комиссии в running и сбрасывает время создания на текущее."""
        if self.valid:
            self.status = 'running'
            self.create_time = current_time()

    @property
    def finish_time(self):
        """Ожидаемое время завершения комиссии.

        Returns:
            Время завершения выполняющейся комиссии либо None для остальных состояний.
        """
        if self.valid and self.status == 'running':
            return (self.create_time + self.duration).replace(microsecond=0)
        else:
            return None

    @staticmethod
    def beautify_name(name):
        """Преобразует символы ASCII римских цифр в конце названия в специальные символы Юникода.

        Заменяет I/II/III/IV/V/VI на соответствующие символы римских цифр Юникода (Ⅰ~Ⅵ).

        Args:
            name: Исходное название, возможно оканчивающееся на ASCII-римские цифры.

        Returns:
            Преобразованное название.
        """
        name = name.strip()
        name = re.sub(r'VI$', 'Ⅵ', name)
        name = re.sub(r'IV$', 'Ⅳ', name)
        name = re.sub(r'V$', 'Ⅴ', name)
        name = re.sub(r'III$', 'Ⅲ', name)
        name = re.sub(r'II$', 'Ⅱ', name)
        name = re.sub(r'I$', 'Ⅰ', name)
        return name

