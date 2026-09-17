"""Операции над множествами клеток карты.

Модуль предоставляет два ключевых класса коллекций: ``SelectedGrids`` и ``RoadGrids``,
предназначенных для пакетного поиска, фильтрации, сортировки и операций над множествами клеток карты.

``SelectedGrids`` — упорядоченная коллекция клеток с поддержкой фильтрации по атрибутам,
индексированного поиска, левого соединения (left join), теоретико-множественных операций
(объединение, пересечение, разность) и различных стратегий сортировки.
``RoadGrids`` представляет комбинации клеток-препятствий на маршрутах для выявления блокировок.
"""

import operator
import typing as t


class SelectedGrids:
    """Упорядоченная коллекция клеток карты.

    Инкапсулирует список объектов клеток, предоставляя расширенные методы выборки,
    фильтрации, сортировки и операций над множествами.
    Поддерживает стандартные протоколы Python: итерацию, индексацию, проверку вхождения и т. д.

    Attributes:
        grids (list): Список объектов клеток.
        indexes (dict): Кэш предварительно вычисленных индексов, создаваемых методом ``create_index()``.
    """

    def __init__(self, grids):
        self.grids = grids
        self.indexes: t.Dict[tuple, SelectedGrids] = {}

    def __iter__(self):
        """Итерировать по всем клеткам коллекции.

        Yields:
            Объект клетки.
        """
        return iter(self.grids)

    def __getitem__(self, item):
        """Получить клетку по индексу или срезу.

        Args:
            item (int | slice): Целочисленный индекс возвращает отдельную клетку, срез возвращает новый объект SelectedGrids.

        Returns:
            GridInfo | SelectedGrids: Отдельная клетка или подмножество клеток.
        """
        if isinstance(item, int):
            return self.grids[item]
        else:
            return SelectedGrids(self.grids[item])

    def __contains__(self, item):
        """Проверить наличие клетки в коллекции.

        Args:
            item: Объект клетки.

        Returns:
            bool: Присутствует ли клетка в коллекции.
        """
        return item in self.grids

    def __str__(self):
        """Вернуть строковое представление всех клеток коллекции.

        Returns:
            str: Список строковых представлений клеток через запятую.
        """
        # return str([str(grid) for grid in self])
        return '[' + ', '.join([str(grid) for grid in self]) + ']'

    def __len__(self):
        """Вернуть количество клеток в коллекции.

        Returns:
            int: Количество клеток.
        """
        return len(self.grids)

    def __bool__(self):
        """Проверить, содержит ли коллекция элементы.

        Returns:
            bool: Содержит ли коллекция хотя бы одну клетку.
        """
        return self.count > 0

    # def __getattr__(self, item):
    #     return [grid.__getattribute__(item) for grid in self.grids]

    @property
    def location(self):
        """Получить координаты всех клеток коллекции.

        Returns:
            list[tuple]: Список координат, где каждый элемент — ``(x, y)``.
        """
        return [grid.location for grid in self.grids]

    @property
    def cost(self):
        """Получить стоимость пути для всех клеток коллекции.

        Returns:
            list[int]: Список стоимостей.
        """
        return [grid.cost for grid in self.grids]

    @property
    def weight(self):
        """Получить веса всех клеток коллекции.

        Returns:
            list[int]: Список весов.
        """
        return [grid.weight for grid in self.grids]

    @property
    def count(self):
        """Получить количество клеток в коллекции.

        Returns:
            int: Количество клеток.
        """
        return len(self.grids)

    def select(self, **kwargs):
        """Отфильтровать клетки по значениям атрибутов.

        Возвращает новую коллекцию, содержащую только те клетки, у которых все указанные атрибуты
        совпадают с заданными значениями (по типу и значению).

        Args:
            **kwargs: Пары имя_атрибута=значение (например, ``is_enemy=True``, ``may_boss=True``).

        Returns:
            SelectedGrids: Подмножество клеток, удовлетворяющих условию.
        """
        def matched(obj):
            flag = True
            for k, v in kwargs.items():
                obj_v = obj.__getattribute__(k)
                if type(obj_v) != type(v) or obj_v != v:
                    flag = False
            return flag

        return SelectedGrids([grid for grid in self.grids if matched(grid)])

    def create_index(self, *attrs):
        """Создать индекс по указанным атрибутам.

        Группирует клетки по значениям заданных атрибутов и сохраняет индекс для ускорения
        последующих запросов через ``indexed_select``.

        Args:
            *attrs: Имена индексируемых атрибутов.

        Returns:
            dict: Словарь индекса (кортеж значений атрибутов -> SelectedGrids).
        """
        indexes = {}
        # index_keys = [(grid.__getattribute__(attr) for attr in attrs) for grid in self.grids]
        for grid in self.grids:
            k = tuple(grid.__getattribute__(attr) for attr in attrs)
            try:
                indexes[k].append(grid)
            except KeyError:
                indexes[k] = [grid]

        indexes = {k: SelectedGrids(v) for k, v in indexes.items()}
        self.indexes = indexes
        return indexes

    def indexed_select(self, *values):
        """Выбрать клетки по предварительно вычисленному индексу.

        Args:
            *values: Значения ключей индекса в порядке, соответствующем вызову ``create_index``.

        Returns:
            SelectedGrids: Набор совпавших клеток (или пустая коллекция при отсутствии совпадений).
        """
        return self.indexes.get(values, SelectedGrids([]))

    def left_join(self, right, on_attr, set_attr, default=None):
        """Выполнить левое соединение (left join) с правой коллекцией.

        Сопоставляет клетки левой (self) и правой коллекций по атрибутам ``on_attr``,
        после чего копирует значения атрибутов ``set_attr`` из правой клетки в левую.

        Args:
            right (SelectedGrids): Правая присоединяемая коллекция.
            on_attr (list[str]): Список имён атрибутов для условия соединения.
            set_attr (list[str]): Список имён атрибутов, копируемых из правой клетки в левую.
            default: Значение по умолчанию, если в правой коллекции нет совпадения.

        Returns:
            SelectedGrids: self с обновлёнными атрибутами.
        """
        right.create_index(*on_attr)
        for grid in self:
            attr_value = tuple([grid.__getattribute__(attr) for attr in on_attr])
            right_grid = right.indexed_select(*attr_value).first_or_none()
            if right_grid is not None:
                for attr in set_attr:
                    grid.__setattr__(attr, right_grid.__getattribute__(attr))
            else:
                for attr in set_attr:
                    grid.__setattr__(attr, default)

        return self

    def filter(self, func):
        """Отфильтровать клетки с помощью функции.

        Args:
            func (callable): Функция-предикат, принимающая объект клетки и возвращающая bool.

        Returns:
            SelectedGrids: Подмножество клеток, удовлетворяющих условию.
        """
        return SelectedGrids([grid for grid in self if func(grid)])

    def set(self, **kwargs):
        """Пакетно установить атрибуты для всех клеток коллекции.

        Args:
            **kwargs: Пары имя_атрибута=значение для установки.
        """
        for grid in self:
            for key, value in kwargs.items():
                grid.__setattr__(key, value)

    def get(self, attr):
        """Получить значения указанного атрибута для всех клеток коллекции.

        Args:
            attr (str): Имя атрибута.

        Returns:
            list: Список значений атрибута по всем клеткам.
        """
        return [grid.__getattribute__(attr) for grid in self.grids]

    def call(self, func, **kwargs):
        """Вызвать указанный метод для каждой клетки коллекции и вернуть список результатов.

        Args:
            func (str): Имя метода.
            **kwargs: Именованные аргументы, передаваемые в метод.

        Returns:
            list: Список результатов вызовов метода.
        """
        return [grid.__getattribute__(func)(**kwargs) for grid in self]

    def first_or_none(self):
        """Получить первую клетку коллекции или None, если коллекция пуста.

        Returns:
            GridInfo | None: Первая клетка или None.
        """
        try:
            return self.grids[0]
        except IndexError:
            return None

    def add(self, grids):
        """Объединить с другой коллекцией (дедупликация через ``__hash__``).

        Args:
            grids (SelectedGrids): Присоединяемая коллекция клеток.

        Returns:
            SelectedGrids: Объединённая коллекция клеток.
        """
        return SelectedGrids(list(set(self.grids + grids.grids)))

    def add_by_eq(self, grids):
        """Объединить с другой коллекцией (дедупликация через ``__eq__``, а не ``__hash__``).

        Используется вместо ``add()``, когда у объектов клеток не реализован корректный ``__hash__``.

        Args:
            grids (SelectedGrids): Присоединяемая коллекция клеток.

        Returns:
            SelectedGrids: Объединённая коллекция клеток.
        """
        new = []
        for grid in self.grids + grids.grids:
            if grid not in new:
                new.append(grid)

        return SelectedGrids(new)

    def intersect(self, grids):
        """Найти пересечение с другой коллекцией (сравнение через ``__hash__``).

        Args:
            grids (SelectedGrids): Вторая коллекция клеток.

        Returns:
            SelectedGrids: Пересечение коллекций клеток.
        """
        return SelectedGrids(list(set(self.grids).intersection(set(grids.grids))))

    def intersect_by_eq(self, grids):
        """Найти пересечение с другой коллекцией (сравнение через ``__eq__``, а не ``__hash__``).

        Args:
            grids (SelectedGrids): Вторая коллекция клеток.

        Returns:
            SelectedGrids: Пересечение коллекций клеток.
        """
        new = []
        for grid in self.grids:
            if grid in grids.grids:
                new.append(grid)

        return SelectedGrids(new)

    def delete(self, grids):
        """Удалить указанные клетки из коллекции.

        Args:
            grids (SelectedGrids): Коллекция удаляемых клеток.

        Returns:
            SelectedGrids: Коллекция после удаления.
        """
        g = [grid for grid in self.grids if grid not in grids]
        return SelectedGrids(g)

    def sort(self, *args):
        """Отсортировать клетки по указанным атрибутам.

        Args:
            *args (str): Имена атрибутов для сортировки в порядке убывания приоритета.

        Returns:
            SelectedGrids: Отсортированная коллекция клеток.
        """
        if not self:
            return self
        if len(args):
            grids = sorted(self.grids, key=operator.attrgetter(*args))
            return SelectedGrids(grids)
        else:
            return self

    def sort_by_camera_distance(self, camera):
        """Отсортировать клетки по манхэттенскому расстоянию до камеры.

        Args:
            camera (tuple): Координаты камеры ``(x, y)``.

        Returns:
            SelectedGrids: Коллекция клеток, отсортированная по возрастанию расстояния.
        """
        import numpy as np
        if not self:
            return self
        location = np.array(self.location)
        diff = np.sum(np.abs(location - camera), axis=1)
        # grids = [x for _, x in sorted(zip(diff, self.grids))]
        grids = tuple(np.array(self.grids)[np.argsort(diff)])
        return SelectedGrids(grids)

    def sort_by_clock_degree(self, center=(0, 0), start=(0, 1), clockwise=True):
        """Отсортировать клетки по полярному углу.

        Принимает center за начало координат, направление start за 0 градусов и сортирует клетки по углу.
        По умолчанию сортировка по часовой стрелке.

        Args:
            center (tuple): Координаты центра (начала координат).
            start (tuple): Координаты начального направления (соответствует углу theta=0).
            clockwise (bool): True для сортировки по часовой стрелке, False — против часовой стрелки.

        Returns:
            SelectedGrids: Коллекция клеток, отсортированная по углу.
        """
        import numpy as np
        if not self:
            return self
        vector = np.subtract(self.location, center)
        theta = np.arctan2(vector[:, 1], vector[:, 0]) / np.pi * 180
        vector = np.subtract(start, center)
        theta = theta - np.arctan2(vector[1], vector[0]) / np.pi * 180
        if not clockwise:
            theta = -theta
        theta[theta < 0] += 360
        grids = tuple(np.array(self.grids)[np.argsort(theta)])
        return SelectedGrids(grids)


class RoadGrids:
    """Комбинация клеток-препятствий на пути.

    Представляет точки препятствий на маршруте, где каждой точке может соответствовать несколько
    клеток-кандидатов (например, выбор из двух альтернативных клеток врагов).
    Поддерживает обнаружение дорожных заторов и комбинацию маршрутов.

    Attributes:
        grids (list[SelectedGrids]): Список групп клеток-препятствий, где каждый элемент — набор клеток-кандидатов.
    """

    def __init__(self, grids):
        """
        Args:
            grids (list):
        """
        self.grids = []
        for grid in grids:
            if isinstance(grid, list):
                self.grids.append(SelectedGrids(grids=grid))
            else:
                self.grids.append(SelectedGrids(grids=[grid]))

    def __str__(self):
        """Вернуть строковое представление препятствий маршрута.

        Returns:
            str: Строка с точками препятствий, разделёнными дефисом ' - '.
        """
        return str(' - '.join([str(grid) for grid in self.grids]))

    def roadblocks(self):
        """Получить подтверждённые клетки дорожных заторов (препятствий).

        Когда все клетки в точке препятствия заняты врагами, точка считается подтверждённым затором.

        Returns:
            SelectedGrids: Коллекция подтверждённых клеток-препятствий.
        """
        grids = []
        for block in self.grids:
            if block.count == block.select(is_enemy=True).count:
                grids += block.grids
        return SelectedGrids(grids)

    def potential_roadblocks(self):
        """Получить потенциальные клетки дорожных заторов.

        Когда в точке препятствия остаётся ровно одна клетка без врага (для прохода требуется победить одного врага),
        и при этом в точке нет флота или зачищенных клеток, возвращаются клетки врагов этой точки.

        Returns:
            SelectedGrids: Коллекция клеток врагов в потенциальных заторах.
        """
        grids = []
        for block in self.grids:
            if any([grid.is_fleet for grid in block]):
                continue
            if any([grid.is_cleared for grid in block]):
                continue
            if block.count - block.select(is_enemy=True).count == 1:
                grids += block.select(is_enemy=True).grids
        return SelectedGrids(grids)

    def first_roadblocks(self):
        """Получить первые клетки дорожных заторов, требующие устранения.

        Возвращает клетки врагов во всех незачищенных точках препятствий, где отсутствует флот.

        Returns:
            SelectedGrids: Коллекция клеток врагов, требующих устранения.
        """
        grids = []
        for block in self.grids:
            if any([grid.is_fleet for grid in block]):
                continue
            if any([grid.is_cleared for grid in block]):
                continue
            if block.select(is_enemy=True).count >= 1:
                grids += block.select(is_enemy=True).grids
        return SelectedGrids(grids)

    def combine(self, road):
        """Вычислить декартово произведение точек препятствий двух маршрутов.

        Объединяет каждую пару точек препятствий из self и road, формируя все возможные комбинации.

        Args:
            road (RoadGrids): Препятствия второго маршрута.

        Returns:
            RoadGrids: Скомбинированный набор препятствий.
        """
        out = RoadGrids([])
        for select_1 in self.grids:
            for select_2 in road.grids:
                select = select_1.add(select_2)
                out.grids.append(select)

        return out
