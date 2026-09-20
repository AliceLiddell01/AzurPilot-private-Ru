"""Модуль базовых вспомогательных функций.

Предоставляет низкоуровневые утилиты обработки изображений (обрезка, сравнение цветов,
корректировка порогов сопоставления шаблонов), генерации случайных координат,
загрузки изображений с откатом по серверам, извлечения текста и символов.
"""

import random
import re

import cv2
import numpy as np
from PIL import Image

from module.exception import TemplateMatchError

REGEX_NODE = re.compile(r'(-?[A-Za-z]+)(-?\d+)')
TEMPLATE_MATCH_NON_NATIVE_720P = False
TEMPLATE_MATCH_NON_NATIVE_720P_THRESHOLD = 0.75
TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION = (1280, 720)


def set_template_match_non_native_720p(enabled, resolution=(1280, 720)):
    global TEMPLATE_MATCH_NON_NATIVE_720P, TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION
    TEMPLATE_MATCH_NON_NATIVE_720P = bool(enabled)
    TEMPLATE_MATCH_NON_NATIVE_720P_RESOLUTION = resolution


def lower_template_match_similarity(similarity):
    """
    Смягчает порог шаблонного поиска для снимков не в исходном разрешении 720p.

    Когда снимок экрана захвачен не в исходном разрешении 1280x720,
    ограничивает строгий порог до 0.75.

    Args:
        similarity: Порог cv2.TM_CCOEFF_NORMED в диапазоне 0~1.

    Returns:
        float: Скорректированный порог сходства.
    """
    similarity = float(similarity)
    if TEMPLATE_MATCH_NON_NATIVE_720P:
        return min(similarity, TEMPLATE_MATCH_NON_NATIVE_720P_THRESHOLD)
    return similarity


def random_normal_distribution_int(a, b, n=3):
    """
    Генерирует случайное целое число с нормальным распределением в заданном интервале.
    Использует среднее значение нескольких случайных чисел для аппроксимации нормального распределения.

    Args:
        a (int): Минимальное значение интервала.
        b (int): Максимальное значение интервала.
        n (int): Количество случайных чисел для симуляции, по умолчанию 3.

    Returns:
        int: Случайное целое число с нормальным распределением.
    """
    a = round(a)
    b = round(b)
    if a < b:
        total = 0
        for _ in range(n):
            total += random.randint(a, b)
        return round(total / n)
    else:
        return b


def random_rectangle_point(area, n=3):
    """Случайно выбирает точку внутри заданной прямоугольной области.

    Args:
        area: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        n (int): Количество случайных чисел для симуляции, по умолчанию 3.

    Returns:
        tuple[int]: Координаты (x, y).
    """
    x = random_normal_distribution_int(area[0], area[2], n=n)
    y = random_normal_distribution_int(area[1], area[3], n=n)
    return x, y


def random_rectangle_vector(vector, box, random_range=(0, 0, 0, 0), padding=15):
    """Случайно размещает вектор внутри заданной области.

    Args:
        vector: Вектор (x, y).
        box: Область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        random_range (tuple): Диапазон случайного смещения вектора (x_min, y_min, x_max, y_max).
        padding (int): Внутренний отступ.

    Returns:
        tuple[int], tuple[int]: Координаты начальной и конечной точек.
    """
    vector = np.array(vector) + random_rectangle_point(random_range)
    vector = np.round(vector).astype(int)
    half_vector = np.round(vector / 2).astype(int)
    box = np.array(box) + np.append(np.abs(half_vector) + padding, -np.abs(half_vector) - padding)
    center = random_rectangle_point(box)
    start_point = center - half_vector
    end_point = start_point + vector
    return tuple(start_point), tuple(end_point)


def random_rectangle_vector_opted(
        vector, box, random_range=(0, 0, 0, 0), padding=15, whitelist_area=None, blacklist_area=None):
    """
    Случайно размещает вектор внутри области (с фильтрацией белым/чёрным списками).

    При зависании эмулятора или игры жест свайпа может быть интерпретирован как клик
    (клик в конечной точке свайпа). Для предотвращения нежелательных нажатий выполняется
    фильтрация сгенерированных траекторий.

    Args:
        vector: Вектор (x, y).
        box: Область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        random_range (tuple): Диапазон случайного смещения вектора (x_min, y_min, x_max, y_max).
        padding (int): Внутренний отступ.
        whitelist_area: Список безопасных областей клика, траектория свайпа завершится внутри них.
        blacklist_area: Чёрный список, используемый, когда белый список не подходит для вектора.
            Исключает траектории, конечная точка которых попадает в чёрный список.

    Returns:
        tuple[int], tuple[int]: Координаты начальной и конечной точек.
    """
    vector = np.array(vector) + random_rectangle_point(random_range)
    vector = np.round(vector).astype(int)
    half_vector = np.round(vector / 2).astype(int)
    box_pad = np.array(box) + np.append(np.abs(half_vector) + padding, -np.abs(half_vector) - padding)
    box_pad = area_offset(box_pad, half_vector)
    segment = int(np.linalg.norm(vector) // 70) + 1

    def in_blacklist(end):
        if not blacklist_area:
            return False
        for x in range(segment + 1):
            point = - vector * x / segment + end
            for area in blacklist_area:
                if point_in_area(point, area, threshold=0):
                    return True
        return False

    if whitelist_area:
        for area in whitelist_area:
            area = area_limit(area, box_pad)
            if all([x > 0 for x in area_size(area)]):
                end_point = random_rectangle_point(area)
                for _ in range(10):
                    if in_blacklist(end_point):
                        continue
                    return point_limit(end_point - vector, box), point_limit(end_point, box)

    for _ in range(100):
        end_point = random_rectangle_point(box_pad)
        if in_blacklist(end_point):
            continue
        return point_limit(end_point - vector, box), point_limit(end_point, box)

    end_point = random_rectangle_point(box_pad)
    return point_limit(end_point - vector, box), point_limit(end_point, box)


def random_line_segments(p1, p2, n, random_range=(0, 0, 0, 0)):
    """Разбивает отрезок на несколько частей.

    Args:
        p1: Начальная точка (x, y).
        p2: Конечная точка (x, y).
        n: Число сегментов разбиения.
        random_range: Диапазон случайного смещения для каждой точки.

    Returns:
        list[tuple]: Список точек разбиения [(x0, y0), (x1, y1), (x2, y2)].
    """
    return [tuple((((n - index) * p1 + index * p2) / n).astype(int) + random_rectangle_point(random_range))
            for index in range(0, n + 1)]


def ensure_time(second, n=3, precision=3):
    """Гарантирует возврат валидного значения времени.

    Args:
        second (int, float, tuple): Значение времени, например 10, (10, 30), '10, 30'.
        n (int): Количество случайных чисел для моделирования, по умолчанию 3.
        precision (int): Точность знаков после запятой.

    Returns:
        float: Обработанное значение времени.
    """
    if isinstance(second, tuple):
        multiply = 10 ** precision
        result = random_normal_distribution_int(second[0] * multiply, second[1] * multiply, n) / multiply
        return round(result, precision)
    elif isinstance(second, str):
        if ',' in second:
            lower, upper = second.replace(' ', '').split(',')
            lower, upper = int(lower), int(upper)
            return ensure_time((lower, upper), n=n, precision=precision)
        if '-' in second:
            lower, upper = second.replace(' ', '').split('-')
            lower, upper = int(lower), int(upper)
            return ensure_time((lower, upper), n=n, precision=precision)
        else:
            return int(second)
    else:
        return second


def ensure_int(*args):
    """
    Преобразует все элементы в целые числа.
    Сохраняет структуру вложенных объектов.

    Args:
        *args: Произвольные аргументы.

    Returns:
        list: Список преобразованных целых чисел.
    """

    def to_int(item):
        try:
            return int(item)
        except TypeError:
            result = [to_int(i) for i in item]
            if len(result) == 1:
                result = result[0]
            return result

    return to_int(args)


def area_offset(area, offset):
    """
    Смещает область на заданное смещение.

    Args:
        area: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        offset: Смещение (x, y).

    Returns:
        tuple: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
    """
    upper_left_x, upper_left_y, bottom_right_x, bottom_right_y = area
    x, y = offset
    return upper_left_x + x, upper_left_y + y, bottom_right_x + x, bottom_right_y + y


def area_pad(area, pad=10):
    """
    Выполняет сужение области внутрь на заданную величину.

    Args:
        area: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        pad (int): Величина отступа внутрь в пикселях.

    Returns:
        tuple: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
    """
    upper_left_x, upper_left_y, bottom_right_x, bottom_right_y = area
    return upper_left_x + pad, upper_left_y + pad, bottom_right_x - pad, bottom_right_y - pad


def limit_in(x, lower, upper):
    """
    Ограничивает значение x диапазоном [lower, upper].

    Args:
        x: Ограничиваемое значение.
        lower: Нижняя граница.
        upper: Верхняя граница.

    Returns:
        int, float: Ограниченное значение.
    """
    return max(min(x, upper), lower)


def area_limit(area1, area2):
    """
    Ограничивает одну область границами другой области.

    Args:
        area1: Ограничиваемая область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        area2: Граничная область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).

    Returns:
        tuple: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
    """
    x_lower, y_lower, x_upper, y_upper = area2
    return (
        limit_in(area1[0], x_lower, x_upper),
        limit_in(area1[1], y_lower, y_upper),
        limit_in(area1[2], x_lower, x_upper),
        limit_in(area1[3], y_lower, y_upper),
    )


def area_size(area):
    """
    Вычисляет размеры области (ширину и высоту).

    Args:
        area: (верхний левый x, верхний левый y, нижний правый x, нижний правый y).

    Returns:
        tuple: (ширина, высота).
    """
    return (
        max(area[2] - area[0], 0),
        max(area[3] - area[1], 0)
    )


def point_limit(point, area):
    """
    Ограничивает точку пределами области.

    Args:
        point: Координаты точки (x, y).
        area: Ограничивающая область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).

    Returns:
        tuple: Ограниченные координаты (x, y).
    """
    return (
        limit_in(point[0], area[0], area[2]),
        limit_in(point[1], area[1], area[3])
    )


def point_in_area(point, area, threshold=5):
    """Определяет, находится ли точка внутри области.

    Args:
        point: Координаты точки (x, y).
        area: Область (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        threshold (int): Порог допуска.

    Returns:
        bool: True, если точка находится внутри области.
    """
    return area[0] - threshold < point[0] < area[2] + threshold and area[1] - threshold < point[1] < area[3] + threshold


def area_in_area(area1, area2, threshold=5):
    """Определяет, находится ли область 1 полностью внутри области 2.

    Args:
        area1: Область 1 (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        area2: Область 2 (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        threshold (int): Порог допуска.

    Returns:
        bool: True, если область 1 полностью внутри области 2.
    """
    return area2[0] - threshold <= area1[0] \
           and area2[1] - threshold <= area1[1] \
           and area1[2] <= area2[2] + threshold \
           and area1[3] <= area2[3] + threshold


def area_cross_area(area1, area2, threshold=5):
    """Определяет, пересекаются ли две области.

    Args:
        area1: Область 1 (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        area2: Область 2 (верхний левый x, верхний левый y, нижний правый x, нижний правый y).
        threshold (int): Порог допуска.

    Returns:
        bool: True, если области пересекаются.
    """
    # https://www.yiiven.cn/rect-is-intersection.html
    xa1, ya1, xa2, ya2 = area1
    xb1, yb1, xb2, yb2 = area2
    return abs(xb2 + xb1 - xa2 - xa1) <= xa2 - xa1 + xb2 - xb1 + threshold * 2 \
           and abs(yb2 + yb1 - ya2 - ya1) <= ya2 - ya1 + yb2 - yb1 + threshold * 2


def float2str(n, decimal=3):
    """Преобразует число с плавающей точкой в строку с фиксированным количеством знаков.

    Args:
        n (float): Преобразуемое число.
        decimal (int): Количество знаков после запятой.

    Returns:
        str: Отформатированная строка.
    """
    return str(round(n, decimal)).ljust(decimal + 2, "0")


def point2str(x, y, length=4):
    """Преобразует координаты точки в строку с выравниванием по правому краю.

    Args:
        x (int, float): Координата x.
        y (int, float): Координата y.
        length (int): Длина поля выравнивания.

    Returns:
        str: Строка с выравниванием по правому краю, например '( 100,  80)'.
    """
    return '(%s, %s)' % (str(int(x)).rjust(length), str(int(y)).rjust(length))


def col2name(col):
    """
    Преобразует индекс столбца (с 0) в буквенное имя в стиле Excel.

    Args:
       col (int): Номер столбца (начиная с 0).

    Returns:
        str: Буквенное обозначение столбца.

    Examples:
        0 -> A, 3 -> D, 35 -> AJ, -1 -> -A
    """

    col_neg = col < 0
    if col_neg:
        col_num = -col
    else:
        col_num = col + 1  # Преобразуем к 1-индексации
    col_str = ''

    while col_num:
        # Диапазон остатка 1..26
        remainder = col_num % 26

        if remainder == 0:
            remainder = 26

        # Преобразуем остаток в символ
        col_letter = chr(remainder + 64)

        # Накапливаем буквы столбца справа налево
        col_str = col_letter + col_str

        # Получаем следующий порядок величины
        col_num = int((col_num - 1) / 26)

    if col_neg:
        return '-' + col_str
    else:
        return col_str


def name2col(col_str):
    """
    Преобразует буквенное имя столбца в стиле A1 в индекс столбца (с 0).

    Args:
       col_str (str): Буквенное имя столбца в стиле A1.

    Returns:
        int: Индекс столбца (начиная с 0).
    """
    # Преобразуем строку имени столбца в системе счисления по основанию 26 в число
    expn = 0
    col = 0
    col_neg = col_str.startswith('-')
    col_str = col_str.strip('-').upper()

    for char in reversed(col_str):
        col += (ord(char) - 64) * (26 ** expn)
        expn += 1

    if col_neg:
        return -col
    else:
        return col - 1  # Преобразуем из 1-индексации в 0-индексацию


def node2location(node):
    """
    Преобразует обозначение узла сетки в кортеж координат. См. location2node().

    Args:
        node (str): Строковое обозначение узла сетки, например 'E3'.

    Returns:
        tuple[int]: Кортеж координат, например (4, 2).
    """
    res = REGEX_NODE.search(node)
    if res:
        x, y = res.group(1), res.group(2)
        y = int(y)
        if y > 0:
            y -= 1
        return name2col(x), y
    else:
        # Запасной вариант
        return ord(node[0]) % 32 - 1, int(node[1:]) - 1


def location2node(location):
    """
    Преобразует кортеж координат в буквенно-цифровое обозначение узла сетки в стиле Excel.
    Поддерживает отрицательные значения.

         -2   -1    0    1    2    3
    -2 -B-2 -A-2  A-2  B-2  C-2  D-2
    -1 -B-1 -A-1  A-1  B-1  C-1  D-1
     0  -B1  -A1   A1   B1   C1   D1
     1  -B2  -A2   A2   B2   C2   D2
     2  -B3  -A3   A3   B3   C3   D3
     3  -B4  -A4   A4   B4   C4   D4

    Args:
        location (tuple[int]): Кортеж координат (x, y).

    Returns:
        str: Обозначение узла сетки.
    """
    x, y = location
    if y >= 0:
        y += 1
    return col2name(x) + str(y)


def xywh2xyxy(area):
    """Преобразует формат (x, y, ширина, высота) в формат (x1, y1, x2, y2)."""
    x, y, w, h = area
    return x, y, x + w, y + h


def xyxy2xywh(area):
    """Преобразует формат (x1, y1, x2, y2) в формат (x, y, ширина, высота)."""
    x1, y1, x2, y2 = area
    return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)


def load_image(file, area=None):
    """
    Загружает изображение и удаляет альфа-канал, повторяя поведение pillow.

    Args:
        file (str): Путь к файлу изображения.
        area (tuple): Область обрезки.

    Returns:
        np.ndarray: Массив изображения.
    """
    # Всегда не забываем закрывать объект Image
    with Image.open(file) as f:
        if area is not None:
            f = f.crop(area)

        image = np.array(f)

    channel = image_channel(image)
    if channel == 4:
        image = cv2.cvtColor(image, cv2.COLOR_RGBA2RGB)

    return image


def save_image(image, file):
    """
    Сохраняет изображение, аналогично поведению pillow.

    Args:
        image (np.ndarray): Массив изображения.
        file (str): Путь для сохранения.
    """
    # image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    # cv2.imwrite(file, image)
    Image.fromarray(image).save(file)


def copy_image(src):
    """
    Эквивалентно image.copy(), но немного быстрее.

    Временные затраты на копирование изображения 1280*720*3:
        image.copy()      0.743ms
        copy_image(image) 0.639ms

    Args:
        src: Исходный массив изображения.

    Returns:
        np.ndarray: Копия изображения.
    """
    dst = np.empty_like(src)
    cv2.copyTo(src, None, dst)
    return dst


def crop(image, area, copy=True):
    """
    Обрезает изображение, аналогично crop в pillow, адаптировано для opencv/numpy.
    При выходе области обрезки за границы изображения дополняет чёрным цветом.

    Args:
        image (np.ndarray): Массив изображения.
        area: Область обрезки (x1, y1, x2, y2).
        copy (bool): Копировать ли результат обрезки.

    Returns:
        np.ndarray: Обрезанный массив изображения.
    """
    # map(round, area)
    x1, y1, x2, y2 = area
    x1 = round(x1)
    y1 = round(y1)
    x2 = round(x2)
    y2 = round(y2)
    # h, w = image.shape[:2]
    shape = image.shape
    h = shape[0]
    w = shape[1]
    # Верх, низ, лево, право
    # border = np.maximum((0 - y1, y2 - h, 0 - x1, x2 - w), 0)
    overflow = False
    if y1 >= 0:
        top = 0
        if y1 >= h:
            overflow = True
    else:
        top = -y1
    if y2 > h:
        bottom = y2 - h
    else:
        bottom = 0
        if y2 <= 0:
            overflow = True
    if x1 >= 0:
        left = 0
        if x1 >= w:
            overflow = True
    else:
        left = -x1
    if x2 > w:
        right = x2 - w
    else:
        right = 0
        if x2 <= 0:
            overflow = True
    # При переполнении возвращаем пустое изображение
    if overflow:
        if len(shape) == 2:
            size = (y2 - y1, x2 - x1)
        else:
            size = (y2 - y1, x2 - x1, shape[2])
        return np.zeros(size, dtype=image.dtype)
    # x1, y1, x2, y2 = np.maximum((x1, y1, x2, y2), 0)
    if x1 < 0:
        x1 = 0
    if y1 < 0:
        y1 = 0
    if x2 < 0:
        x2 = 0
    if y2 < 0:
        y2 = 0
    # Обрезка изображения
    image = image[y1:y2, x1:x2]
    # Если требуется заполнение границ
    if top or bottom or left or right:
        if len(shape) == 2:
            value = 0
        else:
            value = tuple(0 for _ in range(image.shape[2]))
        return cv2.copyMakeBorder(image, top, bottom, left, right, borderType=cv2.BORDER_CONSTANT, value=value)
    elif copy:
        return copy_image(image)
    else:
        return image


def resize(image, size):
    """
    Изменяет размер изображения аналогично pillow image.resize(), используя opencv.
    По умолчанию в pillow используется интерполяция PIL.Image.NEAREST.

    Args:
        image (np.ndarray): Массив изображения.
        size: Целевой размер (ширина, высота).

    Returns:
        np.ndarray: Массив изображения после изменения размера.
    """
    return cv2.resize(image, size, interpolation=cv2.INTER_NEAREST)


def image_channel(image):
    """Возвращает число каналов изображения.

    Args:
        image (np.ndarray): Массив изображения.

    Returns:
        int: 0 для полутонового, 3 для RGB-изображения.
    """
    return image.shape[2] if len(image.shape) == 3 else 0


def image_size(image):
    """Возвращает размеры изображения.

    Args:
        image (np.ndarray): Массив изображения.

    Returns:
        int, int: Ширина и высота.
    """
    shape = image.shape
    return shape[1], shape[0]


def image_paste(image, background, origin):
    """
    Вставляет изображение на фоновое изображение.
    Метод не возвращает значение, а модифицирует массив background на месте.

    Args:
        image: Вставляемый массив изображения.
        background: Фоновый массив изображения.
        origin: Координаты верхнего левого угла вставки (x, y).
    """
    x, y = origin
    w, h = image_size(image)
    background[y:y + h, x:x + w] = image


def rgb2gray(image):
    """
    Преобразует RGB-изображение в полутоновое (градации серого).
    gray = ( MAX(r, g, b) + MIN(r, g, b)) / 2

    Args:
        image (np.ndarray): Форма (height, width, channel).

    Returns:
        np.ndarray: Полутоновое изображение, форма (height, width).
    """
    # r, g, b = cv2.split(image)
    # return cv2.add(
    #     cv2.multiply(cv2.max(cv2.max(r, g), b), 0.5),
    #     cv2.multiply(cv2.min(cv2.min(r, g), b), 0.5)
    # )
    if image.ndim == 2:
        return image

    r, g, b = cv2.split(image)
    maximum = cv2.max(r, g)
    cv2.min(r, g, dst=r)
    cv2.max(maximum, b, dst=maximum)
    cv2.min(r, b, dst=r)
    # minimum = r
    cv2.convertScaleAbs(maximum, alpha=0.5, dst=maximum)
    cv2.convertScaleAbs(r, alpha=0.5, dst=r)
    cv2.add(maximum, r, dst=maximum)
    return maximum


def _template_match_image_info(image):
    """Вернуть безопасное описание массива для диагностики шаблонного поиска."""
    shape = getattr(image, 'shape', 'неизвестно')
    dtype = getattr(image, 'dtype', 'неизвестно')
    if isinstance(image, np.ndarray):
        if image.ndim == 2:
            channels = 1
        elif image.ndim == 3:
            channels = image.shape[2]
        else:
            channels = 'неизвестно'
    else:
        channels = 'неизвестно'
    return f'shape={shape}, dtype={dtype}, channels={channels}'


def _template_match_channels(image, role):
    """Проверить поддерживаемое представление массива для matchTemplate."""
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
        raise TemplateMatchError(
            f'Неподдерживаемое представление {role} шаблонного поиска: '
            f'{_template_match_image_info(image)}'
        )

    channels = 1 if image.ndim == 2 else image.shape[2]
    if channels not in (1, 3):
        raise TemplateMatchError(
            f'Неподдерживаемое число каналов {role} шаблонного поиска: '
            f'{_template_match_image_info(image)}'
        )
    return channels


def _template_match_gray(image, cached, role):
    """Получить одноканальное представление массива, используя кэш при наличии."""
    if callable(cached):
        cached = cached()
    if cached is not None:
        gray = cached
    elif image.ndim == 2:
        gray = image
    elif image.shape[2] == 1:
        gray = image[:, :, 0]
    else:
        gray = rgb2gray(image)

    channels = _template_match_channels(gray, f'одноканальный {role}')
    if channels != 1:
        raise TemplateMatchError(
            f'Кэшированное одноканальное представление {role} некорректно: '
            f'{_template_match_image_info(gray)}'
        )
    if gray.shape[:2] != image.shape[:2]:
        raise TemplateMatchError(
            f'Форма кэшированного одноканального представления {role} не совпадает с исходной: '
            f'кэш {_template_match_image_info(gray)}; исходный {_template_match_image_info(image)}'
        )
    return gray


def _template_match_depth(image, template):
    """Согласовать depth только при необходимости и в поддерживаемый OpenCV тип."""
    if image.dtype == template.dtype and image.dtype in (np.dtype('uint8'), np.dtype('float32')):
        return image, template
    return image.astype(np.float32, copy=False), template.astype(np.float32, copy=False)


def template_match(
    image,
    template,
    method=cv2.TM_CCOEFF_NORMED,
    *,
    image_gray=None,
    template_gray=None,
    name=None,
):
    """Выполнить matchTemplate с единым контрактом каналов и depth.

    При несовпадении каналов цветная сторона приводится к grayscale. Для
    кэшированного представления шаблона используется переданный ``template_gray``;
    это сохраняет исходный RGB-массив доступным вызывающему коду. Значение
    ``image_gray``/``template_gray`` может быть callable-провайдером: тогда кэш
    вычисляется только при несовпадении каналов.
    """
    label = f' {name}' if name else ''
    try:
        image_channels = _template_match_channels(image, 'изображения')
        template_channels = _template_match_channels(template, 'шаблона')

        if image_channels != template_channels:
            if image_channels == 1 and template_channels == 3:
                template = _template_match_gray(template, template_gray, 'шаблона')
            elif image_channels == 3 and template_channels == 1:
                image = _template_match_gray(image, image_gray, 'изображения')
            else:
                raise TemplateMatchError(
                    f'Несовместимые каналы шаблонного поиска{label}: '
                    f'изображение {_template_match_image_info(image)}; '
                    f'шаблон {_template_match_image_info(template)}'
                )

        image, template = _template_match_depth(image, template)
    except TemplateMatchError:
        raise
    except (TypeError, ValueError) as error:
        raise TemplateMatchError(
            f'Не удалось подготовить шаблонный поиск{label}: '
            f'изображение {_template_match_image_info(image)}; '
            f'шаблон {_template_match_image_info(template)}'
        ) from error

    try:
        return cv2.matchTemplate(image, template, method)
    except cv2.error as error:
        raise TemplateMatchError(
            f'Шаблонный поиск{label} завершился ошибкой OpenCV: '
            f'изображение {_template_match_image_info(image)}; '
            f'шаблон {_template_match_image_info(template)}'
        ) from error


def rgb2hsv(image):
    """
    Преобразует цветовое пространство RGB в цветовое пространство HSV.
    HSV включает тон, насыщенность и яркость.

    Args:
        image (np.ndarray): Форма (height, width, channel).

    Returns:
        np.ndarray: Тон (0~360), насыщенность (0~100), яркость (0~100).
    """
    image = cv2.cvtColor(image, cv2.COLOR_RGB2HSV).astype(float)
    cv2.multiply(image, (360 / 180, 100 / 255, 100 / 255), dst=image)
    return image


def rgb2yuv(image):
    """
    Преобразует цветовое пространство RGB в YUV.

    Args:
        image (np.ndarray): Форма (height, width, channel).

    Returns:
        np.ndarray: Изображение YUV.
    """
    image = cv2.cvtColor(image, cv2.COLOR_RGB2YUV)
    return image


def rgb2luma(image):
    """
    Преобразует RGB в канал Y (яркость) цветового пространства YUV.

    Args:
        image (np.ndarray): Форма (height, width, channel).

    Returns:
        np.ndarray: Канал яркости, форма (height, width).
    """
    if image.ndim == 2:
        return image

    image = cv2.cvtColor(image, cv2.COLOR_RGB2YUV)
    luma, _, _ = cv2.split(image)
    return luma


def get_color(image, area):
    """Вычисляет средний цвет указанной области изображения.

    Args:
        image (np.ndarray): Снимок экрана.
        area (tuple): (верхний левый x, верхний левый y, нижний правый x, нижний правый y).

    Returns:
        tuple: (r, g, b) среднее значение цвета.
    """
    temp = crop(image, area, copy=False)
    color = cv2.mean(temp)
    return color[:3]


class ImageNotSupported(Exception):
    """Исключение, возникающее, когда над изображением невозможно выполнить вычислительную операцию."""
    pass


def get_bbox(image, threshold=0):
    """
    Получает внешнюю ограничивающую рамку содержимого изображения.
    Реализация getbbox() из pillow на базе opencv.

    Args:
        image (np.ndarray): Массив изображения.
        threshold (int): Цветовой порог.
            color > threshold считается содержимым, color <= threshold считается фоном.

    Returns:
        tuple[int, int, int, int]: Область ограничивающей рамки (x1, y1, x2, y2).

    Raises:
        ImageNotSupported: Вызывается при ошибке вычисления ограничивающей рамки.
    """
    channel = image_channel(image)
    # Преобразуем в градации серого
    if channel == 3:
        # RGB
        mask = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        cv2.threshold(mask, threshold, 255, cv2.THRESH_BINARY, dst=mask)
    elif channel == 0:
        # Изображение в градациях серого
        _, mask = cv2.threshold(image, threshold, 255, cv2.THRESH_BINARY)
    elif channel == 4:
        # RGBA
        mask = cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
        cv2.threshold(mask, threshold, 255, cv2.THRESH_BINARY, dst=mask)
    else:
        raise ImageNotSupported(f'shape={image.shape}')

    # Поиск ограничивающего прямоугольника
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_y, min_x = mask.shape
    max_x = 0
    max_y = 0
    # Полностью черное изображение
    if not contours:
        raise ImageNotSupported(f'Cannot get bbox from a pure black image')
    for contour in contours:
        # x, y, w, h
        x1, y1, x2, y2 = cv2.boundingRect(contour)
        x2 += x1
        y2 += y1
        if x1 < min_x:
            min_x = x1
        if y1 < min_y:
            min_y = y1
        if x2 > max_x:
            max_x = x2
        if y2 > max_y:
            max_y = y2
    if min_x < max_x and min_y < max_y:
        return min_x, min_y, max_x, max_y
    else:
        # В штатной ситуации возникать не должно
        raise ImageNotSupported(f'Empty bbox {(min_x, min_y, max_x, max_y)}')


def get_bbox_reversed(image, threshold=255):
    """
    Получает внешнюю ограничивающую рамку содержимого изображения (обратный порог).
    Реализация getbbox() из pillow на базе opencv.

    Args:
        image (np.ndarray): Массив изображения.
        threshold (int): Цветовой порог.
            color < threshold считается содержимым, color >= threshold считается фоном.

    Returns:
        tuple[int, int, int, int]: Область ограничивающей рамки (x1, y1, x2, y2).

    Raises:
        ImageNotSupported: Вызывается при ошибке вычисления ограничивающей рамки.
    """
    channel = image_channel(image)
    # Преобразуем в градации серого
    if channel == 3:
        # RGB
        mask = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        cv2.threshold(mask, 0, threshold, cv2.THRESH_BINARY, dst=mask)
    elif channel == 0:
        # Изображение в градациях серого
        mask = cv2.threshold(image, 0, threshold, cv2.THRESH_BINARY)
    elif channel == 4:
        # RGBA
        mask = cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
        cv2.threshold(mask, 0, threshold, cv2.THRESH_BINARY, dst=mask)
    else:
        raise ImageNotSupported(f'shape={image.shape}')

    # Поиск ограничивающего прямоугольника
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_y, min_x = mask.shape
    max_x = 0
    max_y = 0
    # Полностью черное изображение
    if not contours:
        raise ImageNotSupported(f'Cannot get bbox from a pure black image')
    for contour in contours:
        # x, y, w, h
        x1, y1, x2, y2 = cv2.boundingRect(contour)
        x2 += x1
        y2 += y1
        if x1 < min_x:
            min_x = x1
        if y1 < min_y:
            min_y = y1
        if x2 > max_x:
            max_x = x2
        if y2 > max_y:
            max_y = y2
    if min_x < max_x and min_y < max_y:
        return min_x, min_y, max_x, max_y
    else:
        # В штатной ситуации возникать не должно
        raise ImageNotSupported(f'Empty bbox {(min_x, min_y, max_x, max_y)}')


def color_similarity(color1, color2):
    """Вычисляет степень различия между двумя цветами.

    Args:
        color1 (tuple): Цвет 1 (r, g, b).
        color2 (tuple): Цвет 2 (r, g, b).

    Returns:
        int: Степень различия цветов.
    """
    # print(color1, color2)
    # diff = np.array(color1).astype(int) - np.array(color2).astype(int)
    # diff = np.max(np.maximum(diff, 0)) - np.min(np.minimum(diff, 0))
    diff_r = color1[0] - color2[0]
    diff_g = color1[1] - color2[1]
    diff_b = color1[2] - color2[2]

    max_positive = 0
    max_negative = 0
    if diff_r > max_positive:
        max_positive = diff_r
    elif diff_r < max_negative:
        max_negative = diff_r
    if diff_g > max_positive:
        max_positive = diff_g
    elif diff_g < max_negative:
        max_negative = diff_g
    if diff_b > max_positive:
        max_positive = diff_b
    elif diff_b < max_negative:
        max_negative = diff_b

    diff = max_positive - max_negative
    return diff


def color_similar(color1, color2, threshold=10):
    """
    Определяет, схожи ли два цвета, если допуск меньше или равен порогу.
    Допуск = Max(положительная разность rgb) + Max(-отрицательная разность rgb)
    Соответствует методу расчета допуска в Photoshop.

    Args:
        color1 (tuple): Цвет 1 (r, g, b).
        color2 (tuple): Цвет 2 (r, g, b).
        threshold (int): Порог допуска, по умолчанию 10.

    Returns:
        bool: True, если два цвета схожи.
    """
    # print(color1, color2)
    # diff = np.array(color1).astype(int) - np.array(color2).astype(int)
    # diff = np.max(np.maximum(diff, 0)) - np.min(np.minimum(diff, 0))
    diff_r = color1[0] - color2[0]
    diff_g = color1[1] - color2[1]
    diff_b = color1[2] - color2[2]

    max_positive = 0
    max_negative = 0
    if diff_r > max_positive:
        max_positive = diff_r
    elif diff_r < max_negative:
        max_negative = diff_r
    if diff_g > max_positive:
        max_positive = diff_g
    elif diff_g < max_negative:
        max_negative = diff_g
    if diff_b > max_positive:
        max_positive = diff_b
    elif diff_b < max_negative:
        max_negative = diff_b

    diff = max_positive - max_negative
    return diff <= threshold


def color_similar_1d(image, color, threshold=10):
    """Определяет, схожи ли цвета в одномерном массиве изображения с указанным цветом.

    Args:
        image (np.ndarray): Одномерный массив.
        color: Целевой цвет (r, g, b).
        threshold (int): Порог допуска, по умолчанию 10.

    Returns:
        np.ndarray: Булев массив.
    """
    diff = image.astype(int) - color
    diff = np.max(np.maximum(diff, 0), axis=1) - np.min(np.minimum(diff, 0), axis=1)
    return diff <= threshold


def color_similarity_2d(image, color):
    """Вычисляет степень различия каждого пикселя двумерного изображения с указанным цветом.

    Args:
        image: Двумерный массив изображения.
        color: Целевой цвет (r, g, b).

    Returns:
        np.ndarray: Массив степеней различия, тип uint8.
    """
    # r, g, b = cv2.split(cv2.subtract(image, (*color, 0)))
    # positive = cv2.max(cv2.max(r, g), b)
    # r, g, b = cv2.split(cv2.subtract((*color, 0), image))
    # negative = cv2.max(cv2.max(r, g), b)
    # return cv2.subtract(255, cv2.add(positive, negative))
    if isinstance(color, tuple) and len(color) == 3:
        color = (*color, 0)
    diff = cv2.subtract(image, color)
    r, g, b = cv2.split(diff)
    cv2.max(r, g, dst=r)
    cv2.max(r, b, dst=r)
    positive = r
    cv2.subtract(color, image, dst=diff)
    r, g, b = cv2.split(diff)
    cv2.max(r, g, dst=r)
    cv2.max(r, b, dst=r)
    negative = r
    cv2.add(positive, negative, dst=positive)
    cv2.subtract(255, positive, dst=positive)
    return positive


def image_color_count(image, color, threshold=221, count=50):
    """Определяет, превышает ли количество пикселей, схожих с заданным цветом, пороговое значение.

    Args:
        image (np.ndarray): Массив изображения.
        color (tuple): Цвет RGB.
        threshold (int): Порог сходства, 255 означает полное совпадение; чем меньше значение, тем мягче проверка.
        count (int): Порог количества пикселей.

    Returns:
        bool: True, если количество схожих пикселей больше count.
    """
    mask = color_similarity_2d(image, color=color)
    cv2.inRange(mask, threshold, 255, dst=mask)
    sum_ = cv2.countNonZero(mask)
    return sum_ > count


def extract_letters(image, letter=(255, 255, 255), threshold=128):
    """Устанавливает цвет букв в чёрный, а цвет фона в белый.

    Args:
        image (np.ndarray): Массив изображения, форма (height, width, channel).
        letter (tuple): RGB-цвет букв.
        threshold (int): Порог различия цветов.

    Returns:
        np.ndarray: Полутоновое изображение, форма (height, width).
    """
    # r, g, b = cv2.split(cv2.subtract(image, (*letter, 0)))
    # positive = cv2.max(cv2.max(r, g), b)
    # r, g, b = cv2.split(cv2.subtract((*letter, 0), image))
    # negative = cv2.max(cv2.max(r, g), b)
    # return cv2.multiply(cv2.add(positive, negative), 255.0 / threshold)
    diff = cv2.subtract(image, letter)
    r, g, b = cv2.split(diff)
    cv2.max(r, g, dst=r)
    cv2.max(r, b, dst=r)
    positive = r
    cv2.subtract(letter, image, dst=diff)
    r, g, b = cv2.split(diff)
    cv2.max(r, g, dst=r)
    cv2.max(r, b, dst=r)
    negative = r
    cv2.add(positive, negative, dst=positive)
    if threshold != 255:
        cv2.convertScaleAbs(positive, alpha=255.0 / threshold, dst=positive)
    return positive


def extract_white_letters(image, threshold=128):
    """Устанавливает цвет букв в чёрный, а цвет фона в белый.
    Эта функция подавляет цветные пиксели (не являющиеся градациями серого).

    Args:
        image (np.ndarray): Массив изображения, форма (height, width, channel).
        threshold (int): Порог различия цветов.

    Returns:
        np.ndarray: Полутоновое изображение, форма (height, width).
    """
    # minimum = cv2.min(cv2.min(r, g), b)
    # maximum = cv2.max(cv2.max(r, g), b)
    # return cv2.multiply(cv2.add(maximum, cv2.subtract(maximum, minimum)), 255.0 / threshold)
    r, g, b = cv2.split(cv2.subtract((255, 255, 255), image))
    maximum = cv2.max(r, g)
    cv2.min(r, g, dst=r)
    cv2.max(maximum, b, dst=maximum)
    cv2.min(r, b, dst=r)
    # minimum = r

    cv2.convertScaleAbs(maximum, alpha=0.5, dst=maximum)
    cv2.convertScaleAbs(r, alpha=0.5, dst=r)
    cv2.subtract(maximum, r, dst=r)
    cv2.add(maximum, r, dst=maximum)
    if threshold != 255:
        cv2.convertScaleAbs(maximum, alpha=255.0 / threshold, dst=maximum)
    return maximum


def crop_to_text(image, threshold=120, padding=2):
    """Обрезает ширину и высоту изображения, плотно подгоняя к текстовому содержимому.

    Специально предназначено для полутоновых изображений после предобработки OCR (вывод extract_letters),
    где текстовые пиксели имеют низкие значения, а фоновые — 255.
    Находит крайние строки и столбцы (левый, правый, верхний, нижний), содержащие текст,
    после чего обрезает изображение до этого диапазона с сохранением небольшого безопасного отступа.

    Args:
        image (np.ndarray): Полутоновое изображение, форма (height, width).
            Значения пикселей 0~255, меньшие значения представляют текст.
        threshold (int): Пиксели со значением < threshold считаются текстом.
            По умолчанию 120, надёжно захватывает сглаженные края шрифтов.
        padding (int): Дополнительные пиксели с каждой стороны в качестве безопасного отступа.
            По умолчанию 2. Если текст обрезается, увеличьте это значение.

    Returns:
        np.ndarray: Обрезанное изображение.
            Если текст не обнаружен, возвращает исходное изображение.
    """
    # Создаем маску пикселей текста (значение < threshold)
    # Детекция текста на полутоновом (2D) или многоканальном (3D) изображении
    mask = np.any(image < threshold, axis=2) if image.ndim == 3 else image < threshold

    # Поиск строк и столбцов, содержащих текст
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)

    if not rows.any() or not cols.any():
        return image

    # Граничные индексы
    row_idx = np.where(rows)[0]
    col_idx = np.where(cols)[0]

    h, w = image.shape[:2]
    top = max(row_idx[0] - padding, 0)
    bottom = min(row_idx[-1] + padding + 1, h)
    left = max(col_idx[0] - padding, 0)
    right = min(col_idx[-1] + padding + 1, w)

    return image[top:bottom, left:right]


def color_mapping(image, max_multiply=2):
    """Отображает значения цвета в диапазон 0-255.
    Минимальный цвет отображается в 0, максимальный в 255, максимальный коэффициент умножения цвета — 2.

    Args:
        image (np.ndarray): Массив изображения.
        max_multiply (int, float): Максимальный коэффициент умножения.

    Returns:
        np.ndarray: Преобразованный массив изображения.
    """
    image = image.astype(float)
    low, high = np.min(image), np.max(image)
    multiply = min(255 / (high - low), max_multiply)
    add = (255 - multiply * (low + high)) / 2
    # image = cv2.add(cv2.multiply(image, multiply), add)
    cv2.multiply(image, multiply, dst=image)
    cv2.add(image, add, dst=image)
    image[image > 255] = 255
    image[image < 0] = 0
    return image.astype(np.uint8)


def image_left_strip(image, threshold, length):
    """Обрезает левую часть изображения.
    Например, в строке `DAILY:200/200` удаляет префикс `DAILY:`, оставляя только `200/200`.

    Args:
        image (np.ndarray): Массив изображения, форма (height, width).
        threshold (int): Порог яркости (0-255).
            Первый столбец с яркостью ниже этого значения считается левым краем.
        length (int): Длина обрезки начиная с левого края.

    Returns:
        np.ndarray: Обрезанное изображение.
    """
    brightness = np.mean(image, axis=0)
    match = np.where(brightness < threshold)[0]

    if len(match):
        left = match[0] + length
        total = image.shape[1]
        if left < total:
            image = image[:, left:]
    return image


def red_overlay_transparency(color1, color2, red=247):
    """Вычисляет прозрачность красного наложения.

    Args:
        color1: Исходный цвет.
        color2: Изменённый цвет.
        red (int): Значение красного компонента (0-255). По умолчанию 247.

    Returns:
        float: Коэффициент прозрачности (от 0 до 1).
    """
    return (color2[0] - color1[0]) / (red - color1[0])


def color_bar_percentage(image, area, prev_color, reverse=False, starter=0, threshold=30):
    """Вычисляет процент заполнения цветной полосы прогресса.

    Args:
        image (np.ndarray): Массив изображения.
        area (tuple): Область полосы прогресса (x1, y1, x2, y2).
        prev_color (tuple): Цвет полосы прогресса (r, g, b).
        reverse (bool): Заполняется ли полоса справа налево. По умолчанию False.
        starter (int): Начальный индекс столбца. По умолчанию 0.
        threshold (int): Порог сходства цвета. По умолчанию 30.

    Returns:
        float: Процент заполнения (от 0 до 1).
    """
    image = crop(image, area, copy=False)
    image = image[:, ::-1, :] if reverse else image
    length = image.shape[1]
    prev_index = starter

    for _ in range(1280):
        bar = color_similarity_2d(image, color=prev_color)
        index = np.where(np.any(bar > 255 - threshold, axis=0))[0]
        if not index.size:
            return prev_index / length
        else:
            index = index[-1]
        if index <= prev_index:
            return index / length
        prev_index = index

        prev_row = bar[:, prev_index] > 255 - threshold
        if not prev_row.size:
            return prev_index / length
        # Отступаем назад на 5 пикселей для получения среднего цвета
        left = max(prev_index - 5, 0)
        mask = np.where(bar[:, left:prev_index + 1] > 255 - threshold)
        prev_color = np.mean(image[:, left:prev_index + 1][mask], axis=0)

    return 0.
