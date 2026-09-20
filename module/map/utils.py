"""Утилитарные функции для карты.

Модуль предоставляет вспомогательные функции для системы карт кампании:
- Преобразование координат: ``location_ensure`` приводит координаты к единому формату кортежа (имя узла / кортеж / GridInfo).
- Расчёт положений камеры: ``camera_1d``, ``camera_2d`` вычисляют позиции камеры для покрытия карты.
- Определение активной области: ``get_map_active_area`` возвращает границы непустых клеток.
- Положения камеры точек появления: ``camera_spawn_point`` вычисляет ближайшие положения камеры к точкам появления.
- Случайное направление: ``random_direction`` генерирует случайный вектор направления по описанию.
- Сопоставление подвижных врагов: ``match_movable`` сопоставляет положение врагов до и после движения через матрицу расстояний.
"""

import numpy as np

from module.base.utils import node2location
from module.map_detection.grid_info import GridInfo


def location_ensure(location):
    """Привести координаты любого формата к единому формату кортежа.

    Поддерживает три формата ввода:
    - Объект с атрибутом ``location`` (например, GridInfo)
    - Строковое имя узла (например, 'D5')
    - Кортеж координат (например, (3, 4))

    Args:
        location: Координаты клетки: объект GridInfo, строка узла или кортеж.

    Returns:
        tuple[int]: Кортеж координат, например ``(4, 3)``.
    """
    if hasattr(location, 'location'):
        return location.location
    elif isinstance(location, str):
        return node2location(location)
    else:
        return location


def camera_1d(shape, sight):
    """Вычислить одномерную последовательность позиций камеры.

    На основе длины карты и поля зрения камеры генерирует список положений камеры,
    покрывающих всю строку или столбец.

    Args:
        shape (int): Размер карты по данному измерению.
        sight (list[int]): Диапазон поля зрения камеры ``[start, end]``, start может быть отрицательным.

    Returns:
        list[int]: Список позиций камеры.
    """
    start, step = abs(sight[0]), sight[1] - sight[0] + 1
    if shape <= start:
        out = shape // 2
    else:
        out = list(range(start, 26, step))
        out.append(shape - sight[1])
        out = [x for x in set(out) if x <= shape - sight[1]]
    return out


def camera_2d(area, sight):
    """Вычислить сетку положений камеры на двумерной карте для покрытия всей активной области.

    Вычисляет положения камеры отдельно по осям X и Y, затем объединяет их в двумерную сетку.

    Args:
        area (tuple[int]): Активная область карты ``(верхний_левый_X, верхний_левый_Y, нижний_правый_X, нижний_правый_Y)``.
            Например, если размер карты I9, но 1-я и 9-я строки, а также столбцы A и I пусты,
            area будет равен ``(1, 1, 8, 8)``.
        sight (tuple[int]): Поле зрения камеры ``(верхний_левый_X, верхний_левый_Y, нижний_правый_X, нижний_правый_Y)``.

    Returns:
        list[tuple]: Список координат позиций камеры ``(x, y)``.
    """
    x = camera_1d(shape=area[2] - area[0], sight=[sight[0], sight[2]])
    y = camera_1d(shape=area[3] - area[1], sight=[sight[1], sight[3]])
    out = np.array(np.meshgrid(x, y)).T.reshape(-1, 2) + area[:2]
    return [tuple(c) for c in out]


def get_map_active_area(grids):
    """Получить границы активной области карты.

    Перебирает все клетки карты, исключая море (``--``) и сушу (``++``),
    и вычисляет минимальный ограничивающий прямоугольник оставшихся активных клеток.

    Args:
        grids (dict): Словарь клеток, где ключи — кортежи координат, а значения — GridInfo
            или объекты с методом ``__str__``.

    Returns:
        tuple: Границы активной области ``(верхний_левый_X, верхний_левый_Y, нижний_правый_X, нижний_правый_Y)``.
    """

    def is_active(g):
        g = g.str if isinstance(g, GridInfo) else str(g)
        return g != '--' and g != '++'

    locations = [loca for loca, grid in grids.items() if is_active(grid)]
    bottom_right = np.max(locations, axis=0)
    upper_left = np.min(locations, axis=0)
    return np.append(upper_left, bottom_right)


def camera_spawn_point(camera_list, sp_list):
    """Вычислить ближайшие положения камеры к точкам появления (спавнам).

    Для каждой точки появления находит ближайшую по манхэттенскому расстоянию позицию камеры,
    используемую для генерации данных сканирования в точках появления.

    Args:
        camera_list (list[tuple]): Список существующих позиций камеры (CampaignMap.camera_data).
        sp_list (list[tuple]): Список координат точек появления.

    Returns:
        list[tuple]: Список позиций камеры для обнаружения точек появления (без дубликатов).
    """
    camera_sp = []
    camera_list = np.array(camera_list)
    for sp in sp_list:
        diff = np.sum(np.abs(camera_list - sp), axis=1)
        camera_sp.append(tuple(camera_list[np.argmin(diff)].tolist()))

    return list(set(camera_sp))


def random_direction(direction):
    """Сгенерировать случайный вектор направления по строковому описанию.

    Фиксирует направление по указанным осям, остальные оси генерирует случайно.
    Пустая строка означает полностью случайное направление.

    Args:
        direction (str): Описание направления, например 'upper-left', 'upper-right', 'bottom-left',
            'bottom-right', 'upper', 'bottom', 'left', 'right' и др.

    Returns:
        tuple[int]: Вектор направления, например ``(-1, 1)`` означает влево-вниз.
    """
    direction = direction.lower()
    x = 1 if np.random.uniform() > 0.5 else -1
    y = 1 if np.random.uniform() > 0.5 else -1
    if 'left' in direction:
        x = -1
    elif 'right' in direction:
        x = 1
    if 'upper' in direction:
        y = -1
    elif 'bottom' in direction:
        y = 1
    return (x, y)


def combine(before, after, limit):
    """Скомбинировать варианты перестановок индексов-кандидатов.

    Генерирует все возможные комбинации индексов для алгоритма сопоставления,
    гарантируя отсутствие повторений одного и того же индекса.

    Args:
        before (list[list[int]]): Список уже построенных комбинаций.
        after (list[int]): Список индексов-кандидатов.
        limit (int): Верхний предел индекса, равный количеству кандидатов.

    Yields:
        list[int]: Список индексов после объединения.
    """
    after += [limit]
    for b in before:
        for a in after:
            index = b + [a]
            match = [m for m in index if m < limit]
            if len(set(match)) == len(match):
                yield index


def match_movable(before, spawn, after, fleets, fleet_step=2):
    """Сопоставить положение подвижных врагов (например, Сирен) до и после перемещения.

    Построением матрицы расстояний и поиском оптимальной перестановки сопоставляет
    позиции врагов до движения с позициями после движения. Используется для отслеживания
    перемещений мобильных врагов.

    Args:
        before (list[tuple]): Список позиций врагов до перемещения.
        spawn (list[tuple]): Список возможных новых точек появления врагов.
        after (list[tuple]): Список позиций врагов после перемещения.
        fleets (list[tuple]): Список позиций флотов.
        fleet_step (int): Максимальный шаг перемещения флота/врага, по умолчанию 2.

    Returns:
        tuple[list[tuple], list[tuple]]: Пары успешно сопоставленных позиций
            ``(matched_before, matched_after)``.

    Examples:
        >>> before = [(0, 2), (0, 0), (1, 0), (2, 4), (7, 19)]
        >>> after = [(7, 9), (0, 3), (0, 1), (1, 1), (2, 5)]
        >>> match_movable(before, [], after, [])
        ([(0, 2), (0, 0), (1, 0), (2, 4)], [(0, 3), (0, 1), (1, 1), (2, 5)])
    """
    base_weight = -10000
    encourage_weight = -100
    before_len = len(before)
    after_len = len(after)
    before = before + spawn
    after = after + fleets
    x = len(after)
    y = len(before)
    distance = np.ones((y, x), dtype=int) * base_weight
    for i1, g1 in enumerate(before):
        for i2, g2 in enumerate(after):
            distance[i1, i2] = fleet_step - sum(abs(np.subtract(g1, g2)))

    distance[distance < 0] = base_weight
    distance[before_len:, :] += encourage_weight
    distance[:, after_len:] += encourage_weight
    distance = np.maximum(distance, base_weight)
    # print(distance)
    # [[-100    1    1    0 -100]
    #  [-100 -100    1    0 -100]
    #  [-100 -100    0    1 -100]
    #  [-100 -100 -100 -100    1]
    #  [-100 -100 -100 -100 -100]]

    permutations = [[]]
    for row in distance:
        match = np.where(row >= encourage_weight)[0].tolist()
        permutations = list(combine(permutations, match, limit=x))
        if not len(permutations):
            permutations = [[x]]

    if len(permutations) == 0 or len(permutations[0]) == 0:
        return [], []
    else:
        permutations = np.array(permutations)
        permutations = permutations[np.argsort(np.sum(permutations, axis=1))]
        distance = np.pad(distance, ((0, 0), (0, 1)), mode='constant', constant_values=base_weight)
        index_x = permutations
        index_y = list(range(y)) * int(index_x.shape[0])
        match = distance[index_y, index_x.ravel()].reshape(-1, y)
        match = np.sum(match, axis=1)
        best_match = permutations[int(np.argmax(match))]
        before = [before[index] for index, match in enumerate(best_match) if match < x]
        after = [after[index] for index in best_match if index < x]
        return before, after
