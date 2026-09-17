"""Модуль управления ручным режимом боя.

Управляет перемещением флота и тактикой действий в ручном бою.

В ручном режиме боя требуется управление движением флота через виртуальный джойстик.
Поддерживаемые режимы действий:
- stand_still_in_the_middle: флот остаётся в центре экрана (подходит для этапов ПВО)
- другие пользовательские шаблоны перемещения

Ручной режим обычно используется для:
- этапов, требующих точного контроля позиции флота;
- высокосложных этапов, непроходимых на автобое;
- специальных тактических задач (например, боёв подлодок).

Наследует от ModuleBase, используется в составе Combat.
"""

from module.base.base import ModuleBase
from module.combat.assets import *


class CombatManual(ModuleBase):
    """Менеджер режима ручного боя.

    Управляет перемещением флота и операциями в ручном бою.

    Attributes:
        auto_mode_checked (bool): Проверен ли автоматический режим.
        auto_mode_switched (bool): Был ли только что выполнен переход из автоматического режима.
        manual_executed (bool): Было ли выполнено действие ручного управления.
    """
    auto_mode_checked = False
    auto_mode_switched = False
    manual_executed = False

    def combat_manual_reset(self):
        self.manual_executed = False

    def handle_combat_stand_still_in_the_middle(self, auto):
        """Обрабатывает режим удержания позиции в центре экрана.

        Args:
            auto (str): Режим боя.

        Returns:
            bool: Было ли выполнено действие.
        """
        if auto != 'stand_still_in_the_middle':
            return False
        # При переключении с автоматического на ручной режим флот обычно уже в центре, опускать его не нужно
        # Иначе флот будет перемещён вниз
        if self.auto_mode_switched:
            return False

        self.device.long_click(MOVE_DOWN, duration=0.8)
        return True

    def handle_combat_stand_still_bottom_left(self, auto):
        """Обрабатывает режим укрытия флота в левом нижнем углу.

        Args:
            auto (str): Режим боя.

        Returns:
            bool: Было ли выполнено действие.
        """
        if auto != 'hide_in_bottom_left':
            return False

        self.device.long_click(MOVE_LEFT_DOWN, duration=(3.5, 5.5))
        return True

    def handle_combat_stand_still_upper_left(self, auto):
        """Обрабатывает режим укрытия флота в левом верхнем углу.

        Args:
            auto (str): Режим боя.

        Returns:
            bool: Было ли выполнено действие.
        """
        if auto != 'hide_in_upper_left':
            return False

        self.device.long_click(MOVE_LEFT_UP, duration=(1.5, 3.5))
        return True

    def handle_combat_weapon_release(self):
        if self.appear_then_click(READY_AIR_RAID, interval=10):
            return True
        if self.appear_then_click(READY_TORPEDO, interval=10):
            return True

        return False

    def handle_combat_manual(self, auto):
        """Обрабатывает режим ручного боя.

        Args:
            auto (str): Режим боя.

        Returns:
            bool: Было ли выполнено действие.
        """
        if self.manual_executed or not self.auto_mode_checked:
            return False

        if self.handle_combat_stand_still_in_the_middle(auto):
            self.manual_executed = True
            return True
        if self.handle_combat_stand_still_bottom_left(auto):
            self.manual_executed = True
            return True
        if self.handle_combat_stand_still_upper_left(auto):
            self.manual_executed = True
            return True

        return False
