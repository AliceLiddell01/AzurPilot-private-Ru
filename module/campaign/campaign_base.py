"""
Базовый модуль выполнения кампании.

Предоставляет ключевую логику выполнения задач кампании, включая:
- Диспетчеризацию функций боя (выбор стратегии боя по данным карты)
- Полную оркестрацию кампании (вход на карту, инициализация, цикл выполнения боёв, обработка исключений)
- Поддержку режима автопоиска (AutoSearch)

Этот модуль является фундаментом для всех задач кампании (основная, события, военный архив и т. д.),
объединяя возможности CampaignUI (UI-навигация), Map (управление картой) и AutoSearchCombat (автобой в автопоиске).
"""

from module.base.decorator import Config, cached_property
from module.campaign.campaign_ui import CampaignUI
from module.combat.auto_search_combat import AutoSearchCombat
from module.exception import CampaignEnd, MapEnemyMoved, ScriptError
from module.logger import logger
from module.map.map import Map
from module.map.map_base import CampaignMap


class CampaignBase(CampaignUI, Map, AutoSearchCombat):
    """Базовый класс выполнения кампании, объединяющий навигацию по UI, операции на карте и бой в автопоиске.

    Управляет полным циклом выполнения задачи кампании: от входа на карту до последовательного проведения каждого боя,
    вплоть до завершения кампании или возникновения исключения. Через декоратор `@Config.when` реализует
    условную диспетчеризацию различных стратегий боя, поддерживая стандартный режим, режим полной зачистки и режим неполных данных карты.

    Механизм поиска функций боя: динамически ищет соответствующую функцию боя по текущему `battle_count`
    (например, battle_0, battle_1 и т. д.), а при отсутствии откатывается к `battle_default`.

    Attributes:
        FUNCTION_NAME_BASE (str): Префикс имени функции боя, по умолчанию 'battle_'.
        MAP (CampaignMap): Объект данных карты текущей кампании, содержащий сетку, позиции врагов,
            точки появления и т. д. Определяется файлом карты подкласса.
    """
    FUNCTION_NAME_BASE = 'battle_'
    MAP: CampaignMap

    def battle_default(self):
        """Стратегия боя по умолчанию: зачистка всех врагов.

        Используется как резервная стратегия при неудаче поиска специализированной функции боя, пытаясь уничтожить врагов на карте.

        Returns:
            bool: True, если бой успешно проведён; False, если боёв не выполнено.
        """
        if self.clear_enemy():
            return True

        logger.warning('[Кампания — основное] Бой не выполнен')
        return False

    def battle_boss(self):
        """Стратегия боя с боссом: принудительное уничтожение босса.

        Уничтожает босса напрямую грубой силой, игнорируя оптимизацию пути.

        Returns:
            bool: True, если бой успешно проведён; False, если боёв не выполнено.
        """
        if self.brute_clear_boss():
            return True

        logger.warning('[Кампания — основное] Бой не выполнен')
        return False

    @Config.when(POOR_MAP_DATA=True, MAP_CLEAR_ALL_THIS_TIME=False)
    def battle_function(self):
        """Функция боя: режим неполных данных карты.

        Стратегия боя при неполных данных карты. В первую очередь атакует босса,
        затем уничтожает элитных врагов, и в последнюю очередь — обычных врагов.
        Приоритетно освобождает второй флот при захвате сиренами и зачищает загадочные клетки (?/Mystery).

        Returns:
            bool: True, если бой успешно проведён; False, если боёв не выполнено.
        """
        logger.info('[Кампания — основное] Используется функция: battle_with_poor_map_data')
        if self.fleet_2_break_siren_caught():
            return True
        self.clear_all_mystery()

        if self.battle_count >= 3:
            self.pick_up_ammo()

        if self.map.select(is_boss=True):
            if self.brute_clear_boss():
                return True
        else:
            if self.clear_siren():
                return True
            return self.clear_enemy()

        return False

    @Config.when(MAP_CLEAR_ALL_THIS_TIME=True)
    def battle_function(self):
        """Функция боя: режим полной зачистки.

        Уничтожает всех врагов на карте (включая элиту, обычных врагов и крепости) перед атакой босса.
        Применяется для этапов, требующих полной зачистки ради трёх звёзд или 100% прохождения.

        Returns:
            bool: True, если бой успешно проведён; False, если боёв не выполнено.
        """
        logger.info('[Кампания — основное] Используется функция: clear_all')
        if self.fleet_2_break_siren_caught():
            return True
        self.clear_all_mystery()

        if self.battle_count >= 3:
            self.pick_up_ammo()

        remain = self.map.select(is_enemy=True) \
            .add(self.map.select(is_siren=True)) \
            .add(self.map.select(is_fortress=True)) \
            .delete(self.map.select(is_boss=True))
        logger.info(f'[Кампания — основное] Осталось вражеских флотов: {remain}')
        if remain.count > 0:
            if self.config.MAP_HAS_MOVABLE_NORMAL_ENEMY:
                if self.clear_any_enemy(sort=('cost_2',)):
                    return True
                return self.battle_default()
            else:
                if self.clear_bouncing_enemy():
                    return True
                if self.clear_siren():
                    return True
                self.clear_mechanism()
                return self.battle_default()
        else:
            result = self.battle_boss()
            return result

    @Config.when(MAP_CLEAR_ALL_THIS_TIME=False, POOR_MAP_DATA=False)
    def battle_function(self):
        """Функция боя: стандартный режим.

        Динамически находит соответствующую функцию боя на основе текущего `battle_count`.
        Порядок поиска: battle_N -> battle_(N-1) -> ... -> battle_default.
        Позволяет файлам карт задавать собственные стратегии для конкретных шагов боя (например, battle_0 атакует босса,
        battle_1 уничтожает конкретных врагов и т. д.).

        Returns:
            bool: True, если бой успешно проведён; False, если боёв не выполнено.
        """
        func = self.FUNCTION_NAME_BASE + 'default'
        for extra_battle in range(10):
            if hasattr(self, self.FUNCTION_NAME_BASE + str(self.battle_count - extra_battle)):
                func = self.FUNCTION_NAME_BASE + str(self.battle_count - extra_battle)
                break

        logger.info(f'[Кампания — основное] Используется функция: {func}')
        func = self.__getattribute__(func)

        result = func()

        return result

    def execute_a_battle(self):
        """Выполняет один бой.

        Вызывает `battle_function()` для проведения боя, обрабатывает исключение `MapEnemyMoved`
        (изменение состояния карты из-за перемещения врага). Если бой не был успешно проведён и включена
        обработка ошибок, отступает; в противном случае выбрасывает `ScriptError`.

        Returns:
            bool: True, если бой успешно выполнен.

        Raises:
            ScriptError: Выбрасывается, если бой не выполнен и отключена обработка ошибок.
        """
        logger.hr(f'{self.FUNCTION_NAME_BASE}{self.battle_count}', level=2)
        prev = self.battle_count
        result = False
        for _ in range(10):
            try:
                result = self.battle_function()
                break
            except MapEnemyMoved:
                if self.battle_count > prev:
                    result = True
                    break
                else:
                    continue

        if not result:
            logger.warning('[Кампания — основное] Ошибка сценария: бой не выполнен')
            if self.config.Error_HandleError:
                logger.warning('[Кампания — основное] Ошибка сценария: бой не выполнен; отступаю')
                self.withdraw()
            else:
                raise ScriptError('Бой не выполнен.')

        return result

    def run(self):
        """Выполняет полный рабочий процесс кампании.

        Процесс:
        1. Получение информации о карте и вход на карту
        2. Инициализация карты (блокировка флота, инициализация данных карты)
        3. Цикл проведения боёв (до 20 боёв) до завершения кампании
        4. Обработка ошибок: если функции боя исчерпаны, отступает или выбрасывает исключение в зависимости от конфигурации

        В режиме автопоиска инициализация карты пропускается и сразу начинается цикл боёв в автопоиске.

        Returns:
            bool: True при штатном завершении кампании.

        Raises:
            ScriptError: Выбрасывается при исчерпании функций боя, если отключена обработка ошибок.
        """
        logger.hr(self.ENTRANCE, level=2)

        # Входим на карту.
        self.map_get_info()
        logger.attr('Число боёв на карте', self._map_battle)
        self.emotion.check_reduce(self._map_battle)
        self.ENTRANCE.area = self.ENTRANCE.button
        self.enter_map(self.ENTRANCE, mode=self.config.Campaign_Mode)

        # Инициализируем карту.
        if not self.map_is_auto_search:
            self.handle_map_fleet_lock()
            self.map_init(self.MAP)
        else:
            self.map = self.MAP
            self.battle_count = 0
            self.fleet_alive_multiple = self.config.Fleet_Fleet2 != 0
            self.lv_reset()
            self.lv_get()

        # Выполняем бои.
        for _ in range(20):
            try:
                if not self.map_is_auto_search:
                    self.execute_a_battle()
                else:
                    self.auto_search_execute_a_battle()
            except CampaignEnd:
                logger.hr('Кампания завершена')
                return True

        # Обрабатываем ошибки.
        logger.warning('[Кампания — основное] Функции боя исчерпаны')
        if self.config.Error_HandleError:
            logger.warning('[Кампания — основное] Ошибка сценария: функции боя исчерпаны; отступаю')
            try:
                self.withdraw()
            except CampaignEnd:
                pass
        else:
            raise ScriptError('Функции боя исчерпаны.')

    @cached_property
    @Config.when(MAP_CLEAR_ALL_THIS_TIME=False)
    def _map_battle(self):
        """
        Получает число боёв на текущей карте (только до появления босса).

        Returns:
            int: Число боёв на текущей карте.
        """
        for data in self.MAP.spawn_data:
            if 'boss' in data:
                if 'battle' in data:
                    return data['battle'] + 1
                else:
                    logger.warning('[Кампания — основное] В данных точек появления отсутствует счётчик боёв')

        logger.warning('[Кампания — основное] В данных точек появления не найдены данные босса')
        return 0

    @cached_property
    @Config.when(MAP_CLEAR_ALL_THIS_TIME=True)
    def _map_battle(self):
        """
        Получает общее число боёв на текущей карте (режим полной зачистки, учитываются все враги).

        Returns:
            int: Общее число боёв на текущей карте.
        """
        battle_count = 0
        for data in self.MAP.spawn_data:
            if 'battle' in data:
                for k, v in data.items():
                    if k != 'battle':
                        battle_count += v
            else:
                logger.warning('[Кампания — основное] В данных точек появления отсутствует счётчик боёв')

        return battle_count

    def auto_search_execute_a_battle(self):
        """Выполняет один бой с использованием режима автопоиска.

        Перемещает флот через автопоиск и проводит бой; используется для этапов с включённым автопоиском.
        После боя автоматически увеличивает `battle_count`.
        """
        logger.hr(f'{self.FUNCTION_NAME_BASE}{self.battle_count}', level=2)
        self.auto_search_moving()
        self.auto_search_combat(fleet_index=self.fleet_show_index,
                                battle=(self.battle_count, self._map_battle))
        self.battle_count += 1
