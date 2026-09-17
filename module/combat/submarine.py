"""Модуль управления вызовом подводных лодок.

Управляет операциями вызова подводных лодок в бою.

Режимы вызова подводных лодок:
- do_not_use: не использовать подводные лодки
- hunt_only: только режим охоты (автоматическая атака врагов в радиусе действия)
- boss_only: вызов подводных лодок только в битве с боссом
- hunt_and_boss: использовать подводные лодки и в охоте, и в битве с боссом

Вызов подводных лодок расходует боезапас; при исчерпании боезапаса вызов невозможен.
Момент вызова определяется конфигурацией роли подводных лодок в настройках автопоиска.

Наследует от ModuleBase, используется в составе Combat.
"""

from module.base.base import ModuleBase
from module.base.timer import Timer
from module.combat.assets import *
from module.logger import logger


class SubmarineCall(ModuleBase):
    """Менеджер вызова подводных лодок.

    Контролирует время вызова и состояние подводных лодок в бою.

    Attributes:
        submarine_call_flag (bool): Были ли вызваны подводные лодки в текущем бою.
        submarine_call_timer (Timer): Таймер проверки вызова подводных лодок.
        submarine_call_click_timer (Timer): Таймер интервала клика по кнопке вызова подводных лодок.
    """
    submarine_call_flag = False
    submarine_call_timer = Timer(5)
    submarine_call_click_timer = Timer(1)

    def submarine_call_reset(self):
        """Сбрасывает состояние вызова подводных лодок; вызывается после battle_execute."""
        self.submarine_call_timer.reset()
        self.submarine_call_flag = False

    def handle_submarine_call(self, submarine='do_not_use', call=False):
        """Обрабатывает вызов подводных лодок.

        Returns:
            bool: Была ли выполнена операция вызова.
        """
        if self.submarine_call_flag:
            return False
        if call and submarine == 'boss_only':
            pass
        else:
            if submarine in ['do_not_use', 'hunt_only', 'boss_only', 'hunt_and_boss']:
                self.submarine_call_flag = True
                return False
        if self.submarine_call_timer.reached():
            logger.info('Сработал таймер вызова подлодки')
            self.submarine_call_flag = True
            return False

        if not self.appear(SUBMARINE_AVAILABLE_CHECK_1) or not self.appear(SUBMARINE_AVAILABLE_CHECK_2):
            return False

        if self.appear(SUBMARINE_CALLED):
            logger.info('Подлодка уже вызвана')
            self.submarine_call_flag = True
            return False
        elif self.submarine_call_click_timer.reached():
            if not self.appear_then_click(SUBMARINE_READY):
                logger.info('Неверный значок подлодки')
                self.device.click(SUBMARINE_READY)
            logger.info('Вызов подлодки')
            self.submarine_call_click_timer.reset()
            return True
