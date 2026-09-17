"""Модуль статической информации о ячейках карты. Определяет класс GridInfo, хранящий фиксированные
свойства ячеек карты (суша, море, точки возрождения, точки появления врагов и т.д.), полученные из WIKI по Azur Lane."""

from module.base.utils import location2node


class GridInfo:
    """Класс сбора базовой информации о ячейках сетки в map_v1.

    Базовые сведения о картах доступны в Azur Lane WIKI: http://wiki.biligame.com/blhx.
    Например, страница http://wiki.biligame.com/blhx/7-2 описывает детали кампании 7-2,
    включая позиции босса и точки появления врагов.

    Ячейки содержат следующие фиксированные свойства, доступные из WIKI:
    | Символ     | Имя свойства             | Описание                               |
    |------------|--------------------------|----------------------------------------|
    | ++         | is_land                  | Суша, флот не может войти              |
    | --         | is_sea                   | Море                                   |
    | __         | is_submarine_spawn_point | Точка возрождения подлодки             |
    | SP         | is_spawn_point           | Флот может появиться здесь             |
    | ME         | may_enemy                | Враг может появиться здесь             |
    | MB         | may_boss                 | Босс может появиться здесь             |
    | MM         | may_mystery              | Таинственное событие может появиться   |
    | MA         | may_ammo                 | Флот может пополнить боезапас здесь    |
    | MS         | may_siren                | Точка появления Сирены/элитного врага  |
    """
    is_os = False

    # is_sea --
    is_land = False  # ++
    is_spawn_point = False  # SP
    is_submarine_spawn_point = False  # __

    may_enemy = False  # ME
    may_boss = False  # MB
    may_mystery = False  # MM
    may_ammo = False  # MA
    may_siren = False  # MS
    may_ambush = False

    is_enemy = False  # example: 0L 1M 2C 3T 3E
    is_boss = False  # BO
    is_mystery = False  # MY
    is_ammo = False  # AM
    is_fleet = False  # FL
    is_current_fleet = False
    is_submarine = False  # ss
    is_siren = False  # SI
    is_portal = False
    portal_link = ()
    is_maze = False
    maze_round = (0, 1, 2)
    maze_nearby = None  # SelectedGrids

    enemy_scale = 0
    enemy_genre = None  # Light, Main, Carrier, Treasure, Enemy(неизвестно)

    is_cleared = False
    is_caught_by_siren = False
    is_carrier = False  # Является ли авианосцем, появившимся из таинственного события
    is_movable = False  # Является ли подвижным противником
    is_mechanism_trigger = False  # Активирован ли механизм
    is_mechanism_block = False  # Заблокировано ли механизмом
    mechanism_trigger = None  # SelectedGrids
    mechanism_block = None  # SelectedGrids
    mechanism_wait = 2  # Время ожидания анимации разблокировки механизма в секундах
    is_fortress = False  # Механическая крепость
    is_flare = False
    is_missile_attack = False
    may_bouncing_enemy = False
    cost = 9999
    cost_1 = 9999
    cost_2 = 9999
    connection = None
    weight = 1

    location = None

    def decode(self, text):
        text = text.upper()
        dic = {
            '++': 'is_land',
            'SP': 'is_spawn_point',
            '__': 'is_submarine_spawn_point',
            'ME': 'may_enemy',
            'MB': 'may_boss',
            'MM': 'may_mystery',
            'MA': 'may_ammo',
            'MS': 'may_siren',
        }
        valid = text in dic
        for k, v in dic.items():
            self.__setattr__(v, valid and bool(k == text))

        self.may_ambush = not (self.may_enemy or self.may_boss or self.may_mystery or self.may_mystery)
        # if self.may_siren:
        #     self.may_enemy = True
        # if self.may_boss:
        #     self.may_enemy = True

    def encode(self):
        dic = {
            '++': 'is_land',
            'BO': 'is_boss',
        }
        for key, value in dic.items():
            if self.__getattribute__(value):
                return key

        if self.is_siren:
            if not self.enemy_genre:
                return 'SU'
            # Формат enemy_genre похож на "Siren_xxx"
            name = self.enemy_genre[6:]
            if '_' in name:
                _, _, name = name.partition('_')
            name = name[:2]
            length = len(name)
            if length == 2:
                return name.upper()
            if length == 1:
                return f'{name.upper()} '
            return 'SU'

        if self.is_enemy:
            return '%s%s' % (
                self.enemy_scale if self.enemy_scale else 0,
                self.enemy_genre[0].upper() if self.enemy_genre else 'E')

        dic = {
            'FL': 'is_current_fleet',
            'Fc': 'is_caught_by_siren',
            'Fl': 'is_fleet',
            'ss': 'is_submarine',
            'MY': 'is_mystery',
            'AM': 'is_ammo',
            'FR': 'is_fortress',
            'MI': 'is_missile_attack',
            'BE': 'may_bouncing_enemy',
            '==': 'is_cleared',
        }
        for key, value in dic.items():
            if self.__getattribute__(value):
                return key

        return '--'

    def __str__(self):
        return location2node(self.location)

    __repr__ = __str__

    def __hash__(self):
        return hash(self.location)

    def __eq__(self, other):
        return self.location == other.location

    @property
    def str(self):
        return self.encode()

    @property
    def is_sea(self):
        return False if self.is_land or self.is_enemy or self.is_siren or self.is_fortress or self.is_boss else True

    @property
    def may_carrier(self):
        return self.is_sea and not self.may_enemy

    @property
    def is_accessible(self):
        return self.cost < 9999

    @property
    def is_accessible_1(self):
        return self.cost_1 < 9999

    @property
    def is_accessible_2(self):
        return self.cost_2 < 9999

    @property
    def is_nearby(self):
        return self.cost < 20

    def merge(self, info, mode='normal'):
        """Объединить распознанную информацию о ячейке с текущей ячейкой.

        Args:
            info (GridInfo): Информация о ячейке для объединения.
            mode (str): Режим сканирования ('init', 'normal', 'carrier', 'movable').

        Returns:
            bool: Успешно ли выполнено объединение.
        """
        # Подлодка может появиться в любом месте, поэтому слияние информации не делится на успешное и неуспешное
        # Но желательно как можно раньше обнаружить подлодку в точке появления
        if info.is_submarine:
            if self.is_submarine_spawn_point:
                self.is_submarine = True
            else:
                pass
        if info.is_caught_by_siren:
            if self.is_sea:
                self.is_fleet = True
                self.is_caught_by_siren = True
            else:
                return False
        if info.is_fleet:
            if self.is_sea:
                self.is_fleet = True
                if info.is_current_fleet:
                    self.is_current_fleet = True
                if mode == 'init' and info.is_enemy:
                    # При начальном сканировании разрешаем клетке одновременно быть is_fleet и is_enemy
                    # Чтобы fixup_submarine_fleet мог получить информацию
                    pass
                else:
                    return True
            else:
                return False
        if info.is_boss:
            if not self.is_land and self.may_boss:
                self.is_boss = True
                return True
            else:
                return False
        if info.is_siren:
            if not self.is_land and self.may_siren:
                self.is_siren = True
                self.enemy_scale = 0
                self.enemy_genre = info.enemy_genre
                return True
            elif (mode == 'movable' or self.is_movable) and not self.is_land:
                self.is_siren = True
                self.enemy_scale = 0
                self.enemy_genre = info.enemy_genre
                return True
            else:
                return False
        if info.is_enemy:
            if self.is_fortress:
                # Крепость может быть обычным противником
                return True
            elif not self.is_land and (self.may_enemy or self.is_carrier or mode == 'decoy'):
                self.is_enemy = True
                if info.enemy_scale and not self.enemy_scale:
                    self.enemy_scale = info.enemy_scale
                if info.enemy_scale == 3 and self.enemy_scale == 2:
                    # Но разрешаем 3 заменять 2
                    self.enemy_scale = info.enemy_scale
                if info.enemy_genre and not (info.enemy_genre == 'Enemy' and self.enemy_genre):
                    self.enemy_genre = info.enemy_genre
                return True
            elif mode == 'carrier' and not self.is_land and self.may_carrier:
                self.is_enemy = True
                self.is_carrier = True
                if info.enemy_scale:
                    self.enemy_scale = info.enemy_scale
                if info.enemy_genre and not (info.enemy_genre == 'Enemy' and self.enemy_genre):
                    self.enemy_genre = info.enemy_genre
                return True
            elif (mode == 'movable' or self.is_movable) and not self.is_land:
                self.is_enemy = True
                if info.enemy_scale:
                    self.enemy_scale = info.enemy_scale
                if info.enemy_genre and not (info.enemy_genre == 'Enemy' and self.enemy_genre):
                    self.enemy_genre = info.enemy_genre
                return True
            else:
                return False
        if info.is_mystery:
            if self.may_mystery:
                self.is_mystery = info.is_mystery
                return True
            else:
                return False
        if info.is_ammo:
            if self.may_ammo:
                self.is_ammo = info.is_ammo
                return True
            else:
                return False
        if info.is_missile_attack:
            if self.may_siren:
                self.is_siren = True
                return True
            elif self.may_enemy:
                self.is_enemy = True
                return True
            # Разрешаем ошибочный прогноз
            # else:
            #     return False

        return True

    def wipe_out(self):
        """Вызывается при переходе флота на ячейку для очистки информации о врагах и событиях."""
        self.is_enemy = False
        self.enemy_scale = 0
        self.enemy_genre = None
        self.is_mystery = False
        self.is_boss = False
        self.is_ammo = False
        self.is_siren = False
        self.is_fortress = False
        self.is_caught_by_siren = False
        self.is_carrier = False
        self.is_movable = False
        if self.is_mechanism_trigger:
            self.mechanism_trigger.set(is_mechanism_trigger=False)
            self.mechanism_block.set(is_mechanism_block=False)

    def reset(self):
        """Вызывается при входе на карту для сброса всех динамических состояний ячейки."""
        self.wipe_out()
        self.is_fleet = False
        self.is_current_fleet = False
        self.is_submarine = False
        self.is_cleared = False
        self.is_mechanism_trigger = False
        self.is_mechanism_block = False
        self.mechanism_trigger = None
        self.mechanism_block = None
        self.may_bouncing_enemy = False

    def covered_grid(self):
        """Получить относительные координаты перекрытых ячеек сетки.

        Returns:
            list[tuple]: Список относительных координат перекрытых ячеек.
        """
        if self.is_current_fleet:
            return [(0, -1), (0, -2)]
        if self.is_fleet or self.is_siren or self.is_mystery:
            return [(0, -1)]

        return []

    def distance_to(self, other):
        """Вычислить манхэттенское расстояние до другой ячейки сетки.

        Args:
            other (GridInfo): Целевая ячейка.

        Returns:
            int: Манхэттенское расстояние.
        """
        l1 = self.location
        l2 = other.location
        return abs(l1[0] - l2[0]) + abs(l1[1] - l2[1])
