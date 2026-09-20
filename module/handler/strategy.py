"""Модуль панели стратегии боя. Определяет StrategyHandler, управляющий выбором строя,
переключателями поиска/вида подлодок, манёврами уклонения и воздушными ударами."""

from module.combat.assets import GET_ITEMS_1
from module.handler.assets import *
from module.handler.info_handler import InfoHandler
from module.logger import logger
from module.template.assets import (TEMPLATE_FORMATION_1, TEMPLATE_FORMATION_2,
                                    TEMPLATE_FORMATION_3)
from module.ui.switch import Switch

# 2023-10-19: количество значков в одной строке увеличено с 2 до 3
FORMATION = Switch('Formation', offset=(100, 200))
FORMATION.add_state('line_ahead', check_button=FORMATION_1)
FORMATION.add_state('double_line', check_button=FORMATION_2)
FORMATION.add_state('diamond', check_button=FORMATION_3)

SUBMARINE_HUNT = Switch('Submarine_hunt', offset=(200, 200))
SUBMARINE_HUNT.add_state('on', check_button=SUBMARINE_HUNT_ON)
SUBMARINE_HUNT.add_state('off', check_button=SUBMARINE_HUNT_OFF)

SUBMARINE_VIEW = Switch('Submarine_view', offset=(100, 200))
SUBMARINE_VIEW.add_state('on', check_button=SUBMARINE_VIEW_ON)
SUBMARINE_VIEW.add_state('off', check_button=SUBMARINE_VIEW_OFF)

MOB_MOVE_OFFSET = (120, 200)
AIR_STRIKE_OFFSET = (120, 200)


class StrategyHandler(InfoHandler):
    fleet_1_formation_fixed = False
    fleet_2_formation_fixed = False

    def strategy_open(self, skip_first_screenshot=True):
        logger.info('[Стратегия — панель] Открытие панели стратегии')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(STRATEGY_OPENED, offset=200):
                break

            if self.appear(IN_MAP, interval=5) and not self.appear(STRATEGY_OPENED, offset=200):
                self.device.click(STRATEGY_OPEN)
                continue

            # Обрабатываем пропущенную таинственную клетку
            if self.appear_then_click(GET_ITEMS_1, offset=5):
                continue

    def strategy_close(self, skip_first_screenshot=True):
        logger.info('[Стратегия — панель] Закрытие панели стратегии')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(STRATEGY_OPENED, offset=200, interval=5):
                continue

            if not self.appear(STRATEGY_OPENED, offset=200):
                break

    def strategy_set_execute(self, formation=None, sub_view=None, sub_hunt=None):
        """
        Выполняет установку настроек стратегии (построение флота, вид подлодок, охота подлодок).

        Args:
            formation (str): 'line_ahead', 'double_line', 'diamond' или None (без изменений).
            sub_view (bool): Включить ли отображение зоны подлодок.
            sub_hunt (bool): Включить ли свободную охоту подлодок.

        Pages:
            in: STRATEGY_OPENED
        """
        logger.info(f'[Стратегия — настройка] Значения: строй={formation}, отображение подлодок={sub_view}, охота подлодок={sub_hunt}')

        if formation is not None:
            FORMATION.set(formation, main=self)
        # Отключаем эту функцию до исправления бага значка в зоне подлодок
        # При использовании подлодок не включать MAP_HAS_DYNAMIC_RED_BORDER

        # Проверка отображения подлодок восстановлена; см. SwitchWithHandler.

        # Баг игры был исправлен неизвестно когда; использование SwitchWithHandler удалено
        if sub_view is not None:
            if SUBMARINE_VIEW.appear(main=self):
                SUBMARINE_VIEW.set('on' if sub_view else 'off', main=self)
            else:
                logger.warning('[Стратегия — настройка] Значок отображения подлодок не появился после настройки')
        if sub_hunt is not None:
            if SUBMARINE_HUNT.appear(main=self):
                SUBMARINE_HUNT.set('on' if sub_hunt else 'off', main=self)
            else:
                logger.warning('[Стратегия — настройка] Значок охоты подлодок не появился после настройки')

    def handle_strategy(self, index):
        """
        Обрабатывает настройки стратегии флота.

        Args:
            index (int): Индекс флота.

        Returns:
            bool: Были ли внесены изменения.
        """
        if self.__getattribute__(f'fleet_{index}_formation_fixed'):
            return False
        expected_formation = self.config.__getattribute__(f'Fleet_Fleet{index}Formation')
        if self._strategy_get_from_map_buff() == expected_formation and not self.config.Submarine_Fleet:
            logger.info('[Стратегия — настройка] Проверка панели стратегии пропущена')
            self.__setattr__(f'fleet_{index}_formation_fixed', True)
            return False

        self.strategy_open()
        self.strategy_set_execute(
            formation=expected_formation,
            sub_view=False,
            sub_hunt=bool(self.config.Submarine_Fleet) and self.config.Submarine_Mode in ['hunt_only', 'hunt_and_boss']
        )
        self.strategy_close()
        self.__setattr__(f'fleet_{index}_formation_fixed', True)
        return True

    def _strategy_get_from_map_buff(self):
        """
        Получает текущий строй из значка усилений на карте.

        Returns:
            str: Название строя.
        """
        image = self.image_crop(MAP_BUFF, copy=False)
        if TEMPLATE_FORMATION_2.match(image):
            buff = 'double_line'
        elif TEMPLATE_FORMATION_1.match(image):
            buff = 'line_ahead'
        elif TEMPLATE_FORMATION_3.match(image):
            buff = 'diamond'
        else:
            buff = 'unknown'

        logger.attr('Бонус карты', buff)
        return buff

    def is_in_strategy_submarine_move(self):
        """
        Проверяет, находится ли экран в интерфейсе подтверждения перемещения подлодок.

        Returns:
            bool: Находится ли в интерфейсе подтверждения перемещения подлодок.
        """
        return self.appear(SUBMARINE_MOVE_CONFIRM, offset=(20, 20))

    def strategy_submarine_move_enter(self, skip_first_screenshot=True):
        """
        Переходит в интерфейс перемещения подлодок.

        Pages:
            in: STRATEGY_OPENED, SUBMARINE_MOVE_ENTER
            out: SUBMARINE_MOVE_CONFIRM
        """
        logger.info('Вход в режим перемещения подлодки')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(SUBMARINE_MOVE_ENTER, offset=200, interval=5):
                self.device.click(SUBMARINE_MOVE_ENTER)

            if self.appear(SUBMARINE_MOVE_CONFIRM, offset=(20, 20)):
                break

    def strategy_submarine_move_confirm(self, skip_first_screenshot=True):
        """
        Подтверждает перемещение подлодок.

        Pages:
            in: SUBMARINE_MOVE_CONFIRM
            out: STRATEGY_OPENED, SUBMARINE_MOVE_ENTER
        """
        logger.info('Подтверждение перемещения подлодки')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(SUBMARINE_MOVE_CONFIRM, offset=(20, 20), interval=5):
                pass
            if self.handle_popup_confirm('SUBMARINE_MOVE'):
                pass

            if self.appear(SUBMARINE_MOVE_ENTER, offset=200):
                break

    def strategy_submarine_move_cancel(self, skip_first_screenshot=True):
        """
        Отменяет перемещение подлодок.

        Pages:
            in: SUBMARINE_MOVE_CONFIRM
            out: STRATEGY_OPENED, SUBMARINE_MOVE_ENTER
        """
        logger.info('Отмена перемещения подлодки')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(SUBMARINE_MOVE_CANCEL, offset=(20, 20), interval=5):
                pass
            if self.handle_popup_confirm('SUBMARINE_MOVE'):
                pass

            if self.appear(SUBMARINE_MOVE_ENTER, offset=200):
                break

    def is_in_strategy_mob_move(self):
        """
        Проверяет, находится ли экран в интерфейсе перемещения обычного флота.

        Returns:
            bool: Находится ли в интерфейсе перемещения флота.
        """
        return self.appear(MOB_MOVE_CANCEL, offset=(20, 20))

    def strategy_has_mob_move(self):
        """
        Проверяет наличие опции перемещения обычного флота.

        Pages:
            in: STRATEGY_OPENED
            out: STRATEGY_OPENED
        """
        if self.match_template_color(MOB_MOVE_ENTER, offset=MOB_MOVE_OFFSET):
            return True
        else:
            return False

    def strategy_mob_move_enter(self, skip_first_screenshot=True):
        """
        Переходит в интерфейс перемещения обычного флота.

        Pages:
            in: STRATEGY_OPENED, MOB_MOVE_ENTER
            out: MOB_MOVE_CANCEL
        """
        logger.info('Вход в режим перемещения флота')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(MOB_MOVE_CANCEL, offset=(20, 20)):
                break

            if self.appear_then_click(MOB_MOVE_ENTER, offset=MOB_MOVE_OFFSET, interval=5):
                continue

    def strategy_mob_move_cancel(self, skip_first_screenshot=True):
        """
        Отменяет перемещение обычного флота.

        Pages:
            in: MOB_MOVE_CANCEL
            out: STRATEGY_OPENED, MOB_MOVE_ENTER
        """
        logger.info('Отмена перемещения флота')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(MOB_MOVE_ENTER, offset=MOB_MOVE_OFFSET):
                break

            if self.appear_then_click(MOB_MOVE_CANCEL, offset=(20, 20), interval=5):
                continue

    def is_in_strategy_air_strike(self):
        return self.appear(AIR_STRIKE_CONFIRM, offset=(20, 20))

    def strategy_has_air_strike(self):
        """
        Pages:
            in: STRATEGY_OPENED
            out: STRATEGY_OPENED
        """
        if self.match_template_color(AIR_STRIKE_ENTER, offset=(150, 200)):
            return True
        else:
            return False

    def strategy_air_strike_enter(self, skip_first_screenshot=True):
        """
        Pages:
            in: STRATEGY_OPENED, AIR_STRIKE_ENTER
            out: AIR_STRIKE_CONFIRM
        """
        logger.info('Вход в режим воздушного удара')
        for _ in self.loop(skip_first=skip_first_screenshot):
            if self.appear(AIR_STRIKE_CONFIRM, offset=(20, 20)):
                break
            if self.appear_then_click(AIR_STRIKE_ENTER, offset=(150, 200), interval=5):
                continue

    def strategy_air_strike_cancel(self, skip_first_screenshot=True):
        """
        Pages:
            in: AIR_STRIKE_CONFIRM
            out: STRATEGY_OPENED, AIR_STRIKE_ENTER
        """
        logger.info('Отмена воздушного удара')
        for _ in self.loop(skip_first=skip_first_screenshot):
            if self.appear(AIR_STRIKE_ENTER, offset=(150, 200)):
                break
            if self.appear_then_click(AIR_STRIKE_CANCEL, offset=(20, 20), interval=5):
                continue
