"""Обработчик таинственных клеток (Mystery Node).

Обрабатывает события таинственных клеток на карте:
- Получение предметов (ящики снаряжения, материалы и т. д.)
- Пополнение боеприпасов
- Получение авиационной поддержки (эскадрильи авианосцев)

Наследуется от StrategyHandler и EnemySearchingHandler,
может вызываться напрямую в цикле состояний карты.
"""

from module.base.timer import Timer
from module.base.utils import area_cross_area
from module.combat.assets import GET_ITEMS_1
from module.handler.assets import *
from module.handler.enemy_searching import EnemySearchingHandler
from module.handler.strategy import StrategyHandler
from module.logger import logger


class MysteryHandler(StrategyHandler, EnemySearchingHandler):
    """Обработчик событий таинственных клеток.

    Обрабатывает различные типы событий, возникающие при наступлении флота на клетку с вопросительным знаком.
    Таинственная клетка может дать предметы, боеприпасы или авиационную поддержку.

    Attributes:
        _get_ammo_log_timer (Timer): Таймер ограничения частоты логирования боеприпасов,
            предотвращающий спам повторяющимися логами.
        carrier_count (int): Количество полученных авиационных поддержек на текущей карте.
    """
    _get_ammo_log_timer = Timer(3)
    carrier_count = 0

    def handle_mystery(self, button=None):
        """Единая точка входа для обработки событий таинственных клеток.

        Последовательно проверяет получение предметов, пополнение боеприпасов и авиационную поддержку.

        Args:
            button (Button | None): Кнопка клика при получении предмета.
                Может быть целевой клеткой для имитации действий человека.
                Если None, используется стандартная кнопка MYSTERY_ITEM.

        Returns:
            str | bool: Строка типа события ('get_item', 'get_ammo', 'get_carrier')
                или False, если события не обнаружено.
        """
        with self.stat.new(
                genre=self.config.campaign_name, method=self.config.DropRecord_CombatRecord
        ) as drop:
            if self.handle_mystery_items(button=button, drop=drop):
                return 'get_item'
            if self.handle_mystery_ammo(drop=drop):
                return 'get_ammo'
            if self.handle_mystery_carrier(drop=drop):
                return 'get_carrier'

            return False

    def handle_mystery_items(self, button=None, drop=None):
        """Обрабатывает событие получения предметов с таинственной клетки.

        Определяет экран "Получены предметы", фиксирует дроп и закрывает окно.

        Args:
            button (Button | None): Кнопка клика. Если опция `MAP_MYSTERY_MAP_CLICK` отключена,
                используется стандартная кнопка MYSTERY_ITEM.
            drop (DropImage | None): Объект фиксации дропа.

        Returns:
            bool: Было ли обработано событие получения предметов.
        """
        if not self.config.MAP_MYSTERY_MAP_CLICK:
            button = MYSTERY_ITEM
        if button is None or area_cross_area(button.button, MYSTERY_ITEM.area, threshold=5):
            button = MYSTERY_ITEM

        if self.appear(GET_ITEMS_1, offset=5):
            logger.attr('Таинственная клетка', 'Получен предмет')
            if drop:
                drop.add(self.device.image)
            self.device.click(button)
            self.device.sleep(0.5)
            self.device.screenshot()
            self.strategy_close()
            return True

        return False

    def handle_mystery_ammo(self, drop=None):
        """Обрабатывает событие пополнения боеприпасов с таинственной клетки.

        Определяет уведомление в информационной строке о получении боеприпасов и фиксирует дроп.

        Args:
            drop (DropImage | None): Объект фиксации дропа.

        Returns:
            bool: Было ли обнаружено пополнение боеприпасов.
        """
        if self.info_bar_count():
            if self._get_ammo_log_timer.reached() and self.appear(GET_AMMO):
                logger.attr('Таинственная клетка', 'Получены боеприпасы')
                self._get_ammo_log_timer.reset()
                if drop:
                    drop.add(self.device.image)
                return True

        return False

    def handle_mystery_carrier(self, drop=None):
        """Обрабатывает событие авиационной поддержки с таинственной клетки.

        Если разрешено конфигурацией, определяет появление анимации поиска врагов (присоединение авианосцев),
        дожидается завершения анимации и фиксирует событие.

        Args:
            drop (DropImage | None): Объект фиксации дропа.

        Returns:
            bool: Была ли обработана авиационная поддержка.
        """
        if self.config.MAP_MYSTERY_HAS_CARRIER:
            if self.is_in_map() and self.enemy_searching_appear():
                logger.attr('Таинственная клетка', 'Получена поддержка авианосца')
                self.carrier_count += 1
                if drop:
                    drop.add(self.device.image)
                self.handle_in_map_with_enemy_searching()
                return True

        return False
