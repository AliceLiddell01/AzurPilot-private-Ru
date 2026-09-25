"""Модуль панели навигации по вкладкам. Определяет класс Navbar, определяющий активность/неактивность
вкладок по цвету и поддерживающий автоматическое переключение на указанную вкладку."""

from module.base.base import ModuleBase
from module.base.button import ButtonGrid
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2, GET_SHIP
from module.logger import logger
from module.shop.assets import SHOP_CLICK_SAFE_AREA


class Navbar:
    def __init__(self, grids, active_color=(247, 251, 181), inactive_color=(140, 162, 181), active_threshold=180,
                 inactive_threshold=180, active_count=100, inactive_count=50, name=None):
        """
        Args:
            grids (ButtonGrid): Сетка кнопок вкладок.
            active_color (tuple[int, int, int]): Цвет RGB в активном состоянии.
            inactive_color (tuple[int, int, int]): Цвет RGB в неактивном состоянии.
            active_threshold (int): Порог совпадения цвета в активном состоянии.
            inactive_threshold (int): Порог совпадения цвета в неактивном состоянии.
            active_count (int): Минимальное количество пикселей активного состояния.
            inactive_count (int): Минимальное количество пикселей неактивного состояния.
            name (str): Имя панели навигации.
        """
        self.grids = grids
        self.active_color = active_color
        self.inactive_color = inactive_color
        self.active_threshold = active_threshold
        self.inactive_threshold = inactive_threshold
        self.active_count = active_count
        self.inactive_count = inactive_count
        self.name = name if name is not None else grids._name

    def is_button_active(self, button, main):
        """
        Проверить, находится ли кнопка в активном состоянии.

        Args:
            button (Button): Проверяемая кнопка.
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            bool: Активна ли кнопка.
        """
        return main.image_color_count(
                    button, color=self.active_color, threshold=self.active_threshold, count=self.active_count)

    def is_button_inactive(self, button, main):
        """
        Проверить, находится ли кнопка в неактивном состоянии.

        Args:
            button (Button): Проверяемая кнопка.
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            bool: Неактивна ли кнопка.
        """
        return main.image_color_count(
            button, color=self.inactive_color, threshold=self.inactive_threshold, count=self.inactive_count)

    def get_info(self, main):
        """
        Получить информацию о панели навигации: индексы активного, крайнего левого и крайнего правого элементов.

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            int, int, int: Индекс активного элемента, индекс крайнего левого элемента, индекс крайнего правого элемента.
        """
        total = []
        active = []
        for index, button in enumerate(self.grids.buttons):
            if self.is_button_active(button, main=main):
                total.append(index)
                active.append(index)
            elif self.is_button_inactive(button, main=main):
                total.append(index)

        if len(active) == 0:
            # logger.warning(f'No active nav item found in {self.name}')
            active = None
        elif len(active) == 1:
            active = active[0]
        else:
            logger.warning(f'Обнаружено несколько активных элементов навигации: {self.name}, элементы: {active}')
            active = active[0]

        if len(total) < 2:
            logger.warning(f'Обнаружено слишком мало элементов навигации: {self.name}, элементы: {total}')
        if len(total) == 0:
            left, right = None, None
        else:
            left, right = min(total), max(total)

        return active, left, right

    def get_active(self, main):
        """
        Получить индекс текущего активного элемента навигации.

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            int: Индекс активного элемента.
        """
        return self.get_info(main=main)[0]

    def get_total(self, main):
        """
        Получить общее количество видимых элементов навигации.

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            int: Количество видимых элементов навигации.
        """
        _, left, right = self.get_info(main=main)
        if left is None or right is None:
            return 0
        return right - left + 1

    def _shop_obstruct_handle(self, main):
        """
        Обработать перекрывающие элементы интерфейса магазина (только при нахождении в магазине).

        Args:
            main (ModuleBase): Экземпляр базового модуля.

        Returns:
            bool: Были ли обработаны перекрывающие элементы.
        """
        # По имени определяем, относится ли панель навигации к модулю магазина
        if self.name not in ['SHOP_BOTTOM_NAVBAR', 'GUILD_SIDE_NAVBAR']:
            return False

        # Обрабатываем перекрывающие элементы магазина
        if main.appear(GET_SHIP, interval=1):
            main.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        if main.appear(GET_ITEMS_1, offset=(30, 30), interval=1):
            main.device.click(SHOP_CLICK_SAFE_AREA)
            return True
        if main.appear(GET_ITEMS_2, offset=(30, 30), interval=1):
            main.device.click(SHOP_CLICK_SAFE_AREA)
            return True

        return False

    def set(self, main, left=None, right=None, upper=None, bottom=None, skip_first_screenshot=True):
        """
        Установить панель навигации в указанную позицию относительно одного из направлений.

        Args:
            main (ModuleBase): Экземпляр базового модуля.
            left (int): Индекс элемента навигации слева, начиная с 1.
            right (int): Индекс элемента навигации справа, начиная с 1.
            upper (int): Индекс элемента навигации сверху, начиная с 1.
            bottom (int): Индекс элемента навигации снизу, начиная с 1.
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.

        Returns:
            bool: Успешно ли выполнена установка.
        """
        if left is None and right is None and upper is None and bottom is None:
            logger.warning('[UI — Навигация] Некорректный индекс: необходимо указать индекс относительно одного из направлений')
            return False
        text = ''
        if left is None and upper is not None:
            left = upper
        if right is None and bottom is not None:
            right = bottom
        for k in ['left', 'right', 'upper', 'bottom']:
            if locals().get(k, None) is not None:
                text += f'{k}={locals().get(k, None)} '
        logger.info(f'[UI — Навигация] {self.name}: установка {text.strip()}')

        interval = Timer(2, count=4)
        timeout = Timer(10, count=20).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                main.device.screenshot()

            if timeout.reached():
                logger.warning(f'[UI — Навигация] {self.name}: превышено время ожидания установки {text.strip()}')
                return False

            if self._shop_obstruct_handle(main=main):
                interval.reset()
                timeout.reset()
                continue

            active, minimum, maximum = self.get_info(main=main)
            logger.debug(
                '[UI — Навигация] Активный элемент: %s, диапазон (%s, %s)',
                active,
                minimum,
                maximum,
            )
            # При полностью чёрном снимке возвращается None
            # Active может быть None, если анимация ещё не успела загрузиться
            if active is None or minimum is None or maximum is None:
                continue

            index = minimum + left - 1 if left is not None else maximum - right + 1
            if not minimum <= index <= maximum:
                logger.warning(
                    f'[UI — Навигация] Индекс ({index}) вне диапазона элементов навигации ({minimum}, {maximum})')
                continue

            # End
            if active == index:
                return True

            if interval.reached():
                main.device.click(self.grids.buttons[index])
                interval.reset()
