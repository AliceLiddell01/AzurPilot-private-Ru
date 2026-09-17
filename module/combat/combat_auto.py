"""Модуль управления режимом автобоя.

Управляет переключением между автоматическим и ручным режимами во время боя.

Бой в Azur Lane поддерживает два режима:
- Автоматический режим (Auto): корабли перемещаются и атакуют автоматически, без участия игрока
- Ручной режим (Manual): игрок контролирует движение кораблей и моменты применения навыков

Автоматический режим переключается кнопкой Auto на экране боя.
При разных значениях настроения позиция кнопки Auto может изменяться (смещения 133/150).

Наследует от ModuleBase, используется в составе Combat.
"""

from module.base.base import ModuleBase
from module.base.timer import Timer
from module.combat.assets import COMBAT_AUTO, COMBAT_AUTO_133, COMBAT_AUTO_150, COMBAT_AUTO_SWITCH
from module.logger import logger


class CombatAuto(ModuleBase):
    """Менеджер режима автоматического боя.

    Определяет и переключает автоматический/ручной режим во время боя.

    Attributes:
        auto_skip_timer (Timer): Таймер пропуска проверок автобоя.
        auto_click_interval_timer (Timer): Таймер интервала клика по переключателю автобоя.
        auto_mode_checked (bool): Проверен ли режим автобоя.
        auto_mode_switched (bool): Переключён ли режим автобоя.
        auto_mode_click_timer (Timer): Таймер кликов режима автобоя.
    """
    auto_skip_timer = Timer(1)
    auto_click_interval_timer = Timer(1)
    auto_mode_checked = False
    auto_mode_switched = False
    auto_mode_click_timer = Timer(5)

    def combat_joystick_appear(self) -> bool:
        """Определяет, отображается ли джойстик управления; его наличие означает, что бой идёт в ручном режиме."""
        if self.appear(COMBAT_AUTO, offset=(20, 20)):
            return True
        if self.appear(COMBAT_AUTO_133, offset=(20, 20)):
            return True
        if self.appear(COMBAT_AUTO_150, offset=(20, 20)):
            return True
        return False

    def combat_auto_reset(self):
        self.auto_mode_click_timer.reset()
        self.auto_skip_timer.reset()
        self.auto_mode_checked = False
        self.auto_mode_switched = False

    def handle_combat_auto(self, auto):
        """Обрабатывает переключение режима автобоя.

        Args:
            auto (str): Режим автобоя.

        Returns:
            bool: Было ли выполнено действие.
        """
        if self.auto_mode_checked:
            return False
        if self.auto_mode_click_timer.reached():
            logger.info('[Бой — автобой] Сработал таймер проверки автоматического режима')
            self.auto_mode_checked = True
            return False
        if not self.auto_skip_timer.reached():
            return False
        if not self.auto_click_interval_timer.reached():
            return False

        auto = auto == 'combat_auto'
        if self.combat_joystick_appear():
            if auto:
                self.device.click(COMBAT_AUTO_SWITCH)
                self.auto_click_interval_timer.reset()
                self.auto_mode_switched = True
                return True
        else:
            if not auto:
                self.device.click(COMBAT_AUTO_SWITCH)
                self.auto_click_interval_timer.reset()
                self.auto_mode_switched = True
                return True

        return False
