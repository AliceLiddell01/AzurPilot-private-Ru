"""Обработчик автопоиска.

Управляет игровой функцией автопоиска (Auto Search), включая:
- Переключение вкладок боковой панели на экране подготовки флота (построение/командиры/настройки автопоиска)
- Переключение параметров автопоиска (например, флот 1 для обычных врагов / флот 2 для босса)
- Контроль переключателя автопоиска на карте
- Действия «продолжить» и «выйти» в меню автопоиска

Автопоиск — одна из ключевых функций Azur Lane, позволяющая исследовать зачищенные карты
в автоматическом режиме без ручного управления.

Наследуется от EnemySearchingHandler, далее расширяется в FastForwardHandler.
"""

import numpy as np

from module.base.button import ButtonGrid
from module.base.decorator import Config
from module.base.timer import Timer
from module.handler.assets import *
from module.handler.enemy_searching import EnemySearchingHandler
from module.logger import logger
from module.map.assets import FLEET_PREPARATION_CHECK

# Список кнопок настроек автопоиска, соответствующих 6 вариантам в интерфейсе игры
AUTO_SEARCH_SETTINGS = [
    AUTO_SEARCH_SET_MOB,       # Флот 1 сражается с обычными врагами, флот 2 — с Boss
    AUTO_SEARCH_SET_BOSS,      # Флот 1 сражается с Boss, флот 2 — с обычными врагами
    AUTO_SEARCH_SET_ALL,       # Флот 1 выполняет все вылазки, флот 2 ожидает
    AUTO_SEARCH_SET_STANDBY,   # Флот 1 ожидает, флот 2 выполняет все вылазки
    AUTO_SEARCH_SET_SUB_AUTO,  # Автоматический вызов подлодок
    AUTO_SEARCH_SET_SUB_STANDBY  # Подлодки ожидают
]
# Отображение имени настройки в индекс кнопки
dic_setting_name_to_index = {
    'fleet1_mob_fleet2_boss': 0,
    'fleet1_boss_fleet2_mob': 1,
    'fleet1_all_fleet2_standby': 2,
    'fleet1_standby_fleet2_all': 3,
    'sub_auto_call': 4,
    'sub_standby': 5,
}
# Обратное отображение индекса кнопки в имя настройки
dic_setting_index_to_name = {v: k for k, v in dic_setting_name_to_index.items()}


class AutoSearchHandler(EnemySearchingHandler):
    """Обработчик функциональности автопоиска.

    Управляет операциями автопоиска на экране подготовки флота и на карте.
    Разметка интерфейса немного различается между серверами (расположение и размер боковых кнопок),
    поэтому адаптация реализована через декораторы @Config.when.

    Attributes:
        _auto_search_offset (tuple): Смещение сопоставления настроек автопоиска.
        _auto_search_menu_offset (tuple): Смещение меню автопоиска,
            сдвигается влево на 213px при появлении MULTIPLE_SORTIE.
    """
    @Config.when(SERVER='en')
    def _fleet_sidebar(self):
        if FLEET_PREPARATION_CHECK.match(self.device.image, offset=(20, 80)):
            offset = np.subtract(FLEET_PREPARATION_CHECK.button, FLEET_PREPARATION_CHECK._button)[1]
        else:
            offset = 0
        logger.attr('_fleet_sidebar_offset', offset)
        return ButtonGrid(
            origin=(1178, 171 + offset), delta=(0, 53),
            button_shape=(98, 42), grid_shape=(1, 3), name='FLEET_SIDEBAR')

    @Config.when(SERVER=None)
    def _fleet_sidebar(self):
        if FLEET_PREPARATION_CHECK.match(self.device.image, offset=(20, 80)):
            offset = np.subtract(FLEET_PREPARATION_CHECK.button, FLEET_PREPARATION_CHECK._button)[1]
        else:
            offset = 0
        logger.attr('_fleet_sidebar_offset', offset)
        return ButtonGrid(
            origin=(1185, 155 + offset), delta=(0, 111),
            button_shape=(53, 104), grid_shape=(1, 3), name='FLEET_SIDEBAR')

    def _fleet_preparation_get(self):
        """
        Получает индекс активной вкладки боковой панели на экране подготовки флота.

        Returns:
            int:
                1 — флот (построение)
                2 — коты-командиры
                3 — настройки автопоиска
        """
        current = 0
        total = 0
        sidebar = self._fleet_sidebar()

        for idx, button in enumerate(sidebar.buttons):
            if self.image_color_count(button, color=(99, 235, 255), threshold=221, count=50):
                current = idx + 1
                total = idx + 1
                continue
            if self.image_color_count(button, color=(255, 255, 255), threshold=221, count=100):
                total = idx + 1
            else:
                break

        if not current:
            logger.warning('[Обработчик — автопоиск] Нет активной боковой панели флота')
        logger.attr('Боковая панель флота', f'{current}/{total}')
        return current

    def fleet_preparation_sidebar_ensure(self, index):
        """
        Гарантирует переключение на указанную вкладку боковой панели экрана подготовки.

        Args:
            index (int):
                1 — флот (построение)
                2 — коты-командиры
                3 — настройки автопоиска

        Returns:
            bool: Удалось ли успешно переключить вкладку (до 3 попыток).
        """
        if index <= 0 or index > 5:
            logger.warning(f'[Обработчик — автопоиск] Не удалось установить индекс боковой панели: {index}; допустимый диапазон — от 1 до 5')
            return False

        interval = Timer(1, count=2)
        sidebar = self._fleet_sidebar()
        for _ in self.loop(timeout=3):
            current = self._fleet_preparation_get()
            if current == index:
                return True
            if interval.reached():
                self.device.click(sidebar[0, index - 1])
                interval.reset()
                continue
        else:
            logger.warning('[Обработчик — автопоиск] Не удалось переключить боковую панель')
            return False

    def _auto_search_set_click(self, setting):
        """
        Нажимает на опцию настройки автопоиска.

        Args:
            setting (str): Имя целевой настройки.

        Returns:
            bool: Выбрана ли уже нужная настройка.
        """
        active = []

        for index, button in enumerate(AUTO_SEARCH_SETTINGS):
            if self.image_color_count(button.button, color=(156, 255, 82), threshold=221, count=20):
                active.append(index)

        if not active:
            logger.warning('[Обработчик — автопоиск] Активная настройка автопоиска не найдена')
            return False

        logger.attr('Настройка автопоиска', ', '.join([dic_setting_index_to_name[index] for index in active]))

        if setting not in dic_setting_name_to_index:
            logger.warning(f'[Обработчик — автопоиск] Неизвестная настройка автопоиска: {setting}')
        target_index = dic_setting_name_to_index[setting]

        if target_index in active:
            logger.info('[Обработчик — автопоиск] Правильная настройка автопоиска уже выбрана')
            return True
        else:
            self.device.click(AUTO_SEARCH_SETTINGS[target_index])
            return False

    def auto_search_setting_ensure(self, setting, skip_first_screenshot=True):
        """
        Гарантирует переключение настройки автопоиска на указанный вариант.

        Args:
            setting (str):
                fleet1_mob_fleet2_boss, fleet1_boss_fleet2_mob, fleet1_all_fleet2_standby,
                fleet1_standby_fleet2_all, sub_auto_call, sub_standby
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            bool: Удалось ли успешно переключить настройку (до 5 попыток).
        """
        counter = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            if self._auto_search_set_click(setting):
                return True
            else:
                if counter >= 5:
                    logger.warning('[Обработчик — автопоиск] Не удалось переключить настройку автопоиска')
                    return False
                counter += 1
                self.device.sleep((0.3, 0.5))
                continue

    _auto_search_offset = (5, 5)
    # При появлении MULTIPLE_SORTIE смещаем область на 213 px влево
    _auto_search_menu_offset = (250, 30)

    def is_auto_search_running(self):
        """
        Проверяет, запущен ли автопоиск.

        Returns:
            bool: Включён ли автопоиск.
        """
        return self.appear(AUTO_SEARCH_MAP_OPTION_ON, offset=self._auto_search_offset) \
               and self.appear(AUTO_SEARCH_MAP_OPTION_ON)

    def handle_auto_search_map_option(self):
        """
        Гарантирует включение опции автопоиска на карте.

        Returns:
            bool: Было ли выполнено нажатие.
        """
        if self.appear(AUTO_SEARCH_MAP_OPTION_OFF, offset=self._auto_search_offset) \
                and self.appear_then_click(AUTO_SEARCH_MAP_OPTION_OFF, interval=2):
            return True

        return False

    def is_in_auto_search_menu(self):
        """
        Проверяет, находится ли экран в интерфейсе меню автопоиска.

        Returns:
            bool: Находится ли в меню автопоиска.
        """
        return AUTO_SEARCH_MENU_CONTINUE.match_luma(self.device.image, offset=self._auto_search_menu_offset)

    def handle_auto_search_continue(self):
        return self.appear_then_click(AUTO_SEARCH_MENU_CONTINUE, offset=self._auto_search_menu_offset, interval=2)

    def handle_auto_search_exit(self, drop=None):
        """
        Обрабатывает действие выхода из меню автопоиска.

        Args:
            drop (DropImage): Объект фиксации дропа.

        Returns:
            bool: Было ли выполнено действие выхода.
        """
        if self.appear(AUTO_SEARCH_MENU_EXIT, offset=self._auto_search_menu_offset, interval=2):
            # Здесь реализация довольно грубая
            if drop:
                drop.handle_add(main=self, before=4)
            self.device.click(AUTO_SEARCH_MENU_EXIT)
            self.interval_reset(AUTO_SEARCH_MENU_EXIT)
            return True
        else:
            return False

    def ensure_auto_search_exit(self, skip_first_screenshot=True):
        """
        Pages:
            in: is_in_auto_search_menu
            out: page_campaign, page_event или page_sp
        """
        if not self.is_in_auto_search_menu():
            return False

        with self.stat.new(
                genre=self.config.campaign_name, method=self.config.DropRecord_CombatRecord
        ) as drop:
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()

                if self.handle_auto_search_exit(drop=drop):
                    continue

                # Условие завершения
                if self.is_in_stage():
                    break

        return True