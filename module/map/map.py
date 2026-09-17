"""Модуль исследования карты и организации боевых действий.

Интегрирует управление флотом, планирование маршрутов и систему приоритетов врагов,
предоставляя полную логику исследования карты и ведения боя.

Ключевые функции:
- Зачистка врагов: выбор и уничтожение врагов на карте согласно приоритетам.
- Обработка таинственных клеток: посещение таинственных клеток для получения предметов/боеприпасов.
- Битва с боссом: обнаружение и вызов босса.
- Полная зачистка карты: уничтожение всех доступных врагов на карте.

Система приоритета врагов:
- Управляет стратегией выбора врагов через конфигурацию EnemyPriority.
- Поддерживает сортировку по размеру, типу врага, расстоянию и другим параметрам.
- Обеспечивает отслеживание и прогнозирование подвижных врагов (Сирен).

Наследует Fleet, объединяя управление флотом, контроль камеры и боевую систему.
"""

import itertools
import re

from module.base.filter import Filter
from module.exception import MapEnemyMoved
from module.logger import logger
from module.map.fleet import Fleet
from module.map.map_grids import RoadGrids, SelectedGrids
from module.map_detection.grid_info import GridInfo

# Фильтр врагов
ENEMY_FILTER = Filter(regex=re.compile('^(.*?)$'), attr=('str',))


class Map(Fleet):
    """Координатор исследования карты и ведения боя.

    Управляет зачисткой врагов, обработкой таинственных клеток и битвой с боссом.
    Интеллектуально выбирает следующую цель с помощью системы приоритета врагов.
    """
    def clear_chosen_enemy(self, grid, expected=''):
        """
        Args:
            grid (GridInfo): Целевая клетка.
            expected (str): Ожидаемый тип результата.

        Returns:
            int: Был ли устранён враг.
        """
        logger.info('[Карта — стратегия] Вес размера целевого флота: %s' % (self.config.EnemyPriority_EnemyScaleBalanceWeight))
        logger.info('[Карта — бой] Устранение вражеского флота: %s' % grid)
        expected = f'combat_{expected}' if expected else 'combat'
        battle_count = self.battle_count
        self.show_fleet()
        if self.emotion.is_calculate and self.config.Campaign_UseFleetLock:
            self.emotion.wait(fleet_index=self.fleet_current_index)
        self.goto(grid, expected=expected)

        self.full_scan()
        self.find_path_initial()
        self.map.show_cost()
        return self.battle_count >= battle_count

    def clear_chosen_mystery(self, grid):
        """
        Args:
            grid (GridInfo): Целевая клетка.
        """
        logger.info('[Карта — бой] Зачистка таинственной клетки: %s' % grid)
        self.show_fleet()
        self.goto(grid, expected='mystery')
        # self.mystery_count += 1
        self.map.show_cost()

    def pick_up_ammo(self, grid=None):
        """
        Args:
            grid (GridInfo): Клетка с боеприпасами; если None, выбирается автоматически.
        """
        if grid is None:
            grid = self.map.select(may_ammo=True)
            if not grid:
                logger.info('[Карта — боеприпасы] На карте нет точки боеприпасов')
                return False
            grid = grid[0]

        if self.ammo_count > 0 and grid.is_accessible:
            logger.info('[Карта — боеприпасы] Получение боеприпасов: %s' % grid)
            self.goto(grid, expected='')
            self.ensure_no_info_bar()

            # self.ammo_count -= 5 - self.battle_count
            recover = 5 - self.fleet_ammo
            recover = 3 if recover > 3 else recover
            logger.attr('Получение боеприпасов', recover)

            self.ammo_count -= recover
            self.fleet_ammo += recover

    def clear_mechanism(self, grids=None):
        """
        Args:
            grids (SelectedGrids): Клетки переключателей механизмов. Если None, выбираются все переключатели.

        Returns:
            bool: Всегда возвращает False, так как враги не уничтожались.
        """
        if not self.config.MAP_HAS_LAND_BASED:
            return False

        if not grids:
            grids = self.map.select(is_mechanism_trigger=True, is_mechanism_block=False)
        else:
            grids = grids.select(is_mechanism_trigger=True, is_mechanism_block=False)
        grids = self.select_grids(grids, is_accessible=True, sort=('weight', 'cost'))

        for grid in grids:
            logger.info(f'[Карта — механизм] Активация механизма: {grid}')
            self.goto(grid)
            self.map.show_cost()
            logger.info(f'[Карта — механизм] Освобождён переключатель: {grid.mechanism_trigger}')
            logger.info(f'[Карта — механизм] Убрано препятствие: {grid.mechanism_block}')
            raise MapEnemyMoved

        logger.info('[Карта — механизм] Все механизмы обработаны')
        return False

    @staticmethod
    def select_grids(grids, nearby=False, is_accessible=True, scale=(), genre=(), strongest=False, weakest=False,
                     sort=('weight', 'cost'), ignore=None):
        """
        Args:
            grids (SelectedGrids): Набор клеток для фильтрации.
            nearby (bool): Выбирать ли только соседние клетки.
            is_accessible (bool): Выбирать ли только доступные клетки.
            scale (tuple[int], list[int]): Масштаб врага (кортеж — неупорядоченный выбор, список — упорядоченный).
            genre (tuple[str], list[str]): Тип врага: light, main, carrier, treasure (регистронезависимо).
            strongest (bool): Отдавать ли приоритет сильнейшим врагам.
            weakest (bool): Отдавать ли приоритет слабейшим врагам.
            sort (tuple(str)): Критерии сортировки.
            ignore (SelectedGrids): Клетки, которые следует игнорировать.

        Returns:
            SelectedGrids: Отфильтрованный набор клеток.
        """
        if nearby:
            grids = grids.select(is_nearby=True)
        if is_accessible:
            grids = grids.select(is_accessible=True)
        if ignore is not None:
            grids = grids.delete(grids=ignore)
        if len(scale):
            enemy = SelectedGrids([])
            for enemy_scale in scale:
                enemy = enemy.add(grids.select(enemy_scale=enemy_scale))
                if isinstance(scale, list) and enemy:
                    break
            grids = enemy
        if len(genre):
            enemy = SelectedGrids([])
            for enemy_genre in genre:
                # enemy_genre should be camel case
                enemy_genre = enemy_genre[0].upper() + enemy_genre[1:] if enemy_genre[0].islower() else enemy_genre
                enemy = enemy.add(grids.select(enemy_genre=enemy_genre))
                if isinstance(genre, list) and enemy:
                    break
            grids = enemy
        if strongest:
            for scale in [3, 2, 1, 0]:
                enemy = grids.select(enemy_scale=scale)
                if enemy:
                    grids = enemy
                    break
        if weakest:
            for scale in [1, 2, 3, 0]:
                enemy = grids.select(enemy_scale=scale)
                if enemy:
                    grids = enemy
                    break

        if grids:
            grids = grids.sort(*sort)

        return grids

    @staticmethod
    def show_select_grids(grids, **kwargs):
        length = 3
        keys = list(kwargs.keys())
        for index in range(0, len(keys), length):
            text = [f'{key}={kwargs[key]}' for key in keys[index:index + length]]
            text = ', '.join(text)
            logger.info(text)

        logger.info(f'[Карта] Клетки: {grids}')

    def clear_all_mystery(self, **kwargs):
        """Собрать все доступные таинственные события.

        Returns:
            bool: Всегда возвращает False, так как враги не уничтожались.
        """
        kwargs['sort'] = ('cost',)
        while 1:
            grids = self.map.select(is_mystery=True)
            grids = self.select_grids(grids, **kwargs)

            if not grids:
                break

            logger.hr('Зачистка всех таинственных клеток')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_mystery(grids[0])

        return False

    def clear_enemy(self, **kwargs):
        """Уничтожить одного врага. Если подходящих врагов нет, действие не выполняется.

        Returns:
            bool: Был ли устранён враг.
        """
        grids = self.map.select(is_enemy=True, is_boss=False)

        target = self.config.EnemyPriority_EnemyScaleBalanceWeight
        if target == 'S3_enemy_first':
            kwargs['strongest'] = True
        elif target == 'S1_enemy_first':
            kwargs['weakest'] = True
        elif self.config.MAP_CLEAR_ALL_THIS_TIME:
            kwargs['strongest'] = True
        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Устранение вражеского флота')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_roadblocks(self, roads, **kwargs):
        """Устранить препятствия на маршруте.

        Args:
            roads (list[RoadGrids]): Список маршрутов.

        Returns:
            bool: Был ли устранён враг.
        """
        grids = SelectedGrids([])
        for road in roads:
            grids = grids.add(road.roadblocks())

        target = self.config.EnemyPriority_EnemyScaleBalanceWeight
        if target == 'S3_enemy_first':
            kwargs['strongest'] = True
        elif target == 'S1_enemy_first':
            kwargs['weakest'] = True
        elif self.config.MAP_CLEAR_ALL_THIS_TIME:
            kwargs['strongest'] = True
        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Устранение препятствия')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_potential_roadblocks(self, roads, **kwargs):
        """Устранить потенциальные препятствия, избегая ситуации с единственной пустой клеткой.

        Args:
            roads (list[RoadGrids]): Список маршрутов.

        Returns:
            bool: Был ли устранён враг.
        """
        grids = SelectedGrids([])
        for road in roads:
            grids = grids.add(road.potential_roadblocks())

        target = self.config.EnemyPriority_EnemyScaleBalanceWeight
        if target == 'S3_enemy_first':
            kwargs['strongest'] = True
        elif target == 'S1_enemy_first':
            kwargs['weakest'] = True
        elif self.config.MAP_CLEAR_ALL_THIS_TIME:
            kwargs['strongest'] = True
        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Обход возможного препятствия')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_first_roadblocks(self, roads, **kwargs):
        """Гарантировать наличие хотя бы одной зачищенной клетки на каждом препятствии.

        Args:
            roads (list[RoadGrids]): Список маршрутов.

        Returns:
            bool: Был ли устранён враг.
        """
        grids = SelectedGrids([])
        for road in roads:
            grids = grids.add(road.first_roadblocks())

        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Устранение первого препятствия')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_grids_for_faster(self, grids, **kwargs):
        """Зачистить часть клеток для сокращения расстояния перемещения.

        Args:
            grids (SelectedGrids): Набор клеток для зачистки.

        Returns:
            bool: Был ли устранён враг.
        """

        grids = grids.select(is_enemy=True)
        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Ускоренная зачистка клетки')
            self.show_select_grids(grids, **kwargs)
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_boss(self):
        """Уничтожить босса. Метод устарел, хотя по-прежнему работает на простых картах.
        Для сложных карт рекомендуется использовать brute_clear_boss.

        Returns:
            bool: Успешно ли уничтожен босс.
        """
        grids = self.map.select(is_boss=True, is_accessible=True)
        grids = grids.add(self.map.select(may_boss=True, is_caught_by_siren=True))
        logger.info('[Карта — босс] Кандидаты в боссы: %s' % grids)
        if not grids.count:
            grids = grids.add(self.map.select(may_boss=True, is_enemy=True, is_accessible=True))
            logger.warning('[Карта — босс] Босс не обнаружен; используются возможные клетки босса')
            logger.info('[Карта — босс] Возможный босс: %s' % self.map.select(may_boss=True))
            logger.info('[Карта — босс] Возможный босс, распознанный как вражеский флот: %s' % self.map.select(may_boss=True, is_enemy=True))

        if grids:
            self.submarine_move_near_boss(grids[0])
            logger.hr('Устранение босса')
            grids = grids.sort('weight', 'cost')
            logger.info('[Карта] Клетки: %s' % str(grids))
            self.clear_chosen_enemy(grids[0], expected='boss')

        logger.warning('[Карта — босс] Босс не обнаружен; проверяю все точки его появления')
        return self.clear_potential_boss()

    def capture_clear_boss(self):
        """Уничтожить босса с учётом карт с захватом. Метод устарел, хотя работает на простых картах.
        Для сложных карт рекомендуется использовать brute_clear_boss.

        Returns:
            bool: Успешно ли уничтожен босс.
        """

        grids = self.map.select(is_boss=True, is_accessible=True)
        grids = grids.add(self.map.select(may_boss=True, is_caught_by_siren=True))
        logger.info('[Карта — босс] Кандидаты в боссы: %s' % grids)
        if not grids.count:
            grids = grids.add(self.map.select(may_boss=True, is_enemy=True, is_accessible=True))
            logger.warning('[Карта — босс] Босс не обнаружен; используются возможные клетки босса')
            logger.info('[Карта — босс] Возможный босс: %s' % self.map.select(may_boss=True))
            logger.info('[Карта — босс] Возможный босс, распознанный как вражеский флот: %s' % self.map.select(may_boss=True, is_enemy=True))

        if grids:
            logger.hr('Устранение босса')
            grids = grids.sort('weight', 'cost')
            logger.info('[Карта] Клетки: %s' % str(grids))
            self.clear_chosen_enemy(grids[0])

        logger.warning('[Карта — Boss] Обнаружен флот, захваченный Сиреной; отступление')
        self.withdraw()

    def clear_potential_boss(self):
        """Посетить все точки появления босса, если сам босс не был обнаружен.
        """
        grids = self.map.select(may_boss=True, is_accessible=True).sort('weight', 'cost')
        logger.info('[Карта — босс] Возможный босс: %s' % grids)
        battle_count = self.battle_count
        is_single_boss = self.map.select(may_boss=True).count == 1
        if is_single_boss:
            expected = 'boss'
        else:
            expected = ''

        for grid in grids:
            logger.hr('Устранение возможного босса')
            grids = grids.sort('weight', 'cost')
            logger.info('[Карта] Клетки: %s' % str(grid))
            self.fleet_boss.clear_chosen_enemy(grid, expected=expected)
            if self.battle_count > battle_count:
                logger.info('[Карта — босс] Предположение о позиции босса подтвердилось')
                return True
            else:
                logger.info('[Карта — босс] Предположение о позиции босса не подтвердилось')

        grids = self.map.select(may_boss=True, is_accessible=False).sort('weight', 'cost')
        logger.info('[Карта — босс] Возможный босс: %s' % grids)

        for grid in grids:
            logger.hr('Устранение препятствия перед возможным боссом')
            roadblocks = self.brute_find_roadblocks(grid, fleet=self.fleet_boss_index)
            roadblocks = roadblocks.sort('weight', 'cost')
            logger.info('[Карта] Клетки: %s' % str(roadblocks))
            self.fleet_1.clear_chosen_enemy(roadblocks[0], expected=expected)
            return True

        return False

    def brute_clear_boss(self):
        """Уничтожить босса методом полного перебора препятствий.
        Примечание: метод задействует оба флота.
        """
        boss = self.map.select(is_boss=True)
        if boss:
            logger.info('[Карта — босс] Принудительное устранение босса')
            grids = self.brute_find_roadblocks(boss[0], fleet=self.fleet_boss_index)
            if grids:
                if self.brute_fleet_meet():
                    return True
                logger.info('[Карта — босс] Принудительное устранение препятствия перед боссом')
                grids = grids.sort('weight', 'cost')
                logger.info('[Карта] Клетки: %s' % str(grids))
                self.clear_chosen_enemy(grids[0])
                return True
            else:
                return self.fleet_boss.clear_boss()
        elif self.map.select(may_boss=True, is_caught_by_siren=True):
            logger.info('[Карта — босс] Босс появился на клетке флота')
            self.fleet_2.switch_to()
            return self.clear_chosen_enemy(self.map.select(may_boss=True, is_caught_by_siren=True)[0])
        else:
            logger.warning('[Карта — босс] Босс не обнаружен; проверяю все точки его появления')
            return self.clear_potential_boss()

    def brute_fleet_meet(self):
        """Устранить препятствия между флотами методом поиска путей.
        """
        if self.fleet_boss_index != 2 or not self.fleet_2_location:
            return False
        grids = self.brute_find_roadblocks(self.map[self.fleet_2_location], fleet=1)
        if grids:
            logger.info('[Карта — босс] Принудительное устранение препятствия между флотами')
            grids = grids.sort('weight', 'cost')
            logger.info('[Карта] Клетки: %s' % str(grids))
            self.clear_chosen_enemy(grids[0])
            return True
        else:
            return False

    def clear_siren(self, **kwargs):
        """Уничтожить Сирену.

        Returns:
            bool: Был ли устранён враг.
        """
        if not self.config.MAP_HAS_SIREN and not self.config.MAP_HAS_FORTRESS:
            return False

        if self.config.FLEET_2:
            kwargs['sort'] = ('weight', 'cost_2')
        grids = self.map.select(is_siren=True)
        if self.config.MAP_HAS_FORTRESS:
            grids = grids.add(self.map.select(is_fortress=True))
        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Устранение Сирены')
            self.show_select_grids(grids, **kwargs)
            if grids[0].is_fortress:
                expected = 'fortress'
            else:
                expected = 'siren'
            self.clear_chosen_enemy(grids[0], expected=expected)
            return True

        return False

    def clear_any_enemy(self, **kwargs):
        """Уничтожить любого врага.

        Returns:
            bool: Был ли устранён враг.
        """
        grids = self.map.select(is_enemy=True, is_boss=False)

        if self.config.MAP_HAS_SIREN:
            grids = grids.add(self.map.select(is_siren=True))
        if self.config.MAP_HAS_FORTRESS:
            grids = grids.add(self.map.select(is_fortress=True))

        grids = self.select_grids(grids, **kwargs)

        if grids:
            logger.hr('Устранение вражеского флота')
            self.show_select_grids(grids, **kwargs)
            grid = grids[0]
            if grid.is_fortress:
                expected = 'fortress'
            elif grid.is_siren:
                expected = 'siren'
            else:
                expected = ''
            self.clear_chosen_enemy(grid, expected=expected)
            return True

        return False

    def fleet_2_step_on(self, grids, roadblocks):
        """Второй флот встаёт на клетку для снижения частоты засад на пути другого флота.
        Тот же эффект достигается вызовом 'self.fleet_2.goto(grid)',
        но путь может быть заблокирован врагами — данный метод обрабатывает эту ситуацию.

        Args:
            grids (SelectedGrids): Набор целевых клеток.
            roadblocks (list[RoadGrids]): Список маршрутов с препятствиями.

        Returns:
            bool: Был ли устранён враг.
        """
        if not self.config.FLEET_2:
            return False
        for grid in grids:
            if self.fleet_at(grid=grid, fleet=2):
                return False
        # if grids.count == len([grid for grid in grids if grid.is_enemy or grid.is_cleared]):
        #     logger.info('Fleet 2 step on, no need')
        #     return False
        all_cleared = grids.select(is_cleared=True).count == grids.count

        logger.info('[Карта — флот] Второй флот посещает клетку')
        for grid in grids:
            if grid.is_enemy or (not all_cleared and grid.is_cleared):
                continue
            if self.check_accessibility(grid=grid, fleet=2):
                logger.info('[Карта — флот] Второй флот посещает клетку %s' % grid)
                self.fleet_2.goto(grid)
                self.fleet_1.switch_to()
                return False

        logger.info('[Карта — флот] Второй флот встретил препятствие по пути к клетке')
        clear = self.fleet_1.clear_roadblocks(roadblocks)
        self.fleet_1.clear_all_mystery()
        return clear

    def fleet_2_break_siren_caught(self):
        if self.fleet_boss_index != 2:
            return False
        if not self.config.MAP_HAS_SIREN or not self.config.MAP_HAS_MOVABLE_ENEMY:
            return False
        if not self.map.select(is_caught_by_siren=True):
            logger.info('[Карта — флот] Ни один флот не захвачен Сиреной')
            return False
        if not self.fleet_2_location or not self.map[self.fleet_2_location].is_caught_by_siren:
            logger.warning('[Карта — флот] Сирена захватила флот, но не второй')
            for grid in self.map:
                grid.is_caught_by_siren = False
            return False

        logger.info(f'[Карта — флот] Освобождение второго флота из захвата Сирены: {self.fleet_2_location}')
        self.fleet_2.switch_to()
        self.ensure_edge_insight()
        self.clear_chosen_enemy(self.map[self.fleet_2_location])
        self.fleet_1.switch_to()
        for grid in self.map:
            grid.is_caught_by_siren = False
        return True

    def fleet_2_push_forward(self):
        """Переместить второй флот на клетку с меньшим весом.
        Снижает вероятность блокировки флота босса врагами, особенно на узких картах глав 7–9.

        Подробнее:
        Планирование маршрута минимизации боёв в главе 9
        https://wiki.biligame.com/blhx/9%E7%AB%A0%E9%81%93%E4%B8%AD%E6%88%98%E6%9C%80%E5%B0%8F%E5%8C%96%E8%B7%AF%E7%BA%BF%E8%A7%84%E5%88%92

        Returns:
            bool: Успешно ли выполнено продвижение.
        """
        if self.fleet_boss_index != 2:
            return False

        logger.info('[Карта — флот] Продвижение второго флота')
        grids = self.map.select(is_land=False).sort('weight', 'cost')
        if self.map[self.fleet_2_location].weight <= grids[0].weight:
            logger.info('[Карта — флот] Второй флот продвинут к цели')
            self.fleet_1.switch_to()
            return False

        fleets = SelectedGrids([self.map[self.fleet_1_location], self.map[self.fleet_2_location]])
        grids = grids.select(is_accessible_2=True, is_sea=True).delete(fleets)
        if not grids:
            logger.info('[Карта — флот] Второй флот невозможно продвинуть')
            return False
        if self.map[self.fleet_2_location].weight <= grids[0].weight:
            logger.info('[Карта — флот] Второй флот продвинут на ближайшую клетку')
            return False

        logger.info(f'[Карта] Клетки: {grids}')
        logger.info(f'[Карта — флот] Продвижение: {grids[0]}')
        self.fleet_2.goto(grids[0])
        self.fleet_1.switch_to()
        return True

    def fleet_2_rescue(self, grid):
        """Использовать флот зачистки для спасения флота босса.

        Args:
            grid (GridInfo): Целевая клетка, обычно точка появления босса.

        Returns:
            bool: Был ли устранён враг.
        """
        if self.fleet_boss_index != 2:
            return False

        grids = self.brute_find_roadblocks(grid, fleet=2)
        if not grids:
            return False
        logger.info('[Карта — флот] Спасение второго флота')
        grids = self.select_grids(grids)
        if not grids:
            return False

        self.clear_chosen_enemy(grids[0])
        return True

    def fleet_2_protect(self):
        """Флот зачистки перемещается вокруг флота босса, уничтожая приближающихся Сирен.

        Returns:
            bool: Был ли устранён враг.
        """
        if not self.config.FLEET_2 or not self.config.MAP_HAS_MOVABLE_ENEMY:
            return False

        # When having 2 fleet
        for n in range(20):
            if not self.map.select(is_siren=True):
                return False

            nearby = self.map.select(cost_2=1).add(self.map.select(cost_2=2))
            approaching = SelectedGrids([])
            if self.config.MAP_HAS_MOVABLE_ENEMY:
                approaching = approaching.add(nearby.select(is_siren=True))
            if self.config.MAP_HAS_MOVABLE_NORMAL_ENEMY:
                approaching = approaching.add(nearby.select(is_enemy=True))
            if approaching:
                grids = self.select_grids(approaching, sort=('cost_2', 'cost_1'))
                self.clear_chosen_enemy(grids[0], expected='siren')
                return True
            else:
                grids = nearby.delete(self.map.select(is_fleet=True))
                grids = self.select_grids(grids, sort=('cost_2', 'cost_1'))
                self.goto(grids[0])
                continue

        logger.warning('[Карта — флот] Защита второго флота: поблизости нет Сирен')
        return False

    def clear_filter_enemy(self, string, preserve=0):
        """Уничтожить врагов согласно фильтру.
        Если EnemyPriority_EnemyScaleBalanceWeight != default_mode, фильтр врагов игнорируется.
        Если MAP_HAS_MOVABLE_NORMAL_ENEMY, фильтр врагов игнорируется.

        Args:
            string (str): Строка фильтра врагов, упорядоченная от лёгких к сложным.
            preserve (int): Количество простейших врагов, сохраняемых для боя без боеприпасов (0 — бить всех).

        Returns:
            bool: Был ли устранён враг.
        """
        if self.config.MAP_HAS_MOVABLE_NORMAL_ENEMY:
            if self.clear_any_enemy(sort=('cost_2',)):
                return True
            return False

        if self.config.EnemyPriority_EnemyScaleBalanceWeight == 'S3_enemy_first':
            string = '3L > 3M > 3E > 3C > 2L > 2M > 2E > 2C > 1L > 1M > 1E > 1C'
            preserve = 0
        elif self.config.EnemyPriority_EnemyScaleBalanceWeight == 'S1_enemy_first':
            string = '1L > 1M > 1E > 1C > 2L > 2M > 2E > 2C > 3L > 3M > 3E > 3C'

        ENEMY_FILTER.load(string)
        grids = self.map.select(is_enemy=True, is_accessible=True)
        if not grids:
            return False

        grids = ENEMY_FILTER.apply(grids.sort('weight', 'cost').grids)
        logger.info(f'[Карта — бой] Отбор вражеских флотов: {grids}, сохранить={preserve}')
        if preserve:
            grids = grids[preserve:]

        if grids:
            logger.hr('Устранение отобранного вражеского флота')
            self.clear_chosen_enemy(grids[0])
            return True

        return False

    def clear_bouncing_enemy(self):
        """Уничтожить врага, перемещающегося по фиксированному маршруту.
        Отключается после уничтожения одного врага, так как на карте присутствует только один такой враг.

        Returns:
            bool: Был ли устранён враг.
        """
        if not self.config.MAP_HAS_BOUNCING_ENEMY:
            return False

        route = None
        for a_route in self.map.bouncing_enemy_data:
            if a_route.select(may_bouncing_enemy=True, is_accessible=True):
                route = a_route
                break
        if route is None:
            return False

        logger.hr('Устранение перемещающегося флота')
        logger.info(f'[Карта — бой] Устранение перемещающегося флота: {route}')
        self.show_fleet()
        prev = self.battle_count
        for n, grid in enumerate(itertools.cycle(route)):
            if self.emotion.is_calculate and self.config.Campaign_UseFleetLock:
                self.emotion.wait(fleet_index=self.fleet_current_index)
            self.goto(grid, expected='combat_nothing')

            if self.battle_count > prev:
                logger.info('[Карта — бой] Перемещающийся флот устранён')
                route.select(may_bouncing_enemy=True).set(may_bouncing_enemy=False)
                self.full_scan()
                self.find_path_initial()
                self.map.show_cost()
                return True
            if n >= 12:
                logger.warning('[Карта — бой] Не удалось устранить перемещающийся флот после 12 попыток')
                return False

        return False
