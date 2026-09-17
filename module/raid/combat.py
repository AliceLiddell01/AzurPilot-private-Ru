"""Обработчик рейдовых боёв, наследующий базовый боевой класс и адаптированный под экраны результатов рейдов.

Обрабатывает кнопки статуса боя и отображение опыта в стиле Operation Siren.
"""

from module.combat.assets import EXP_INFO_C, EXP_INFO_D
from module.combat.combat import Combat
from module.guild.assets import BATTLE_STATUS_CF, EXP_INFO_CF


class RaidCombat(Combat):
    def handle_battle_status(self, drop=None):
        """
        Обработка экрана статуса боя в рейде с распознаванием кнопки результатов в стиле Operation Siren.

        Args:
            drop (DropImage): Обработчик изображений выпавшей добычи.

        Returns:
            bool: True, если экран результатов успешно распознан и обработан.
        """
        if self.is_combat_executing():
            return False
        if super().handle_battle_status(drop=drop):
            return True
        if self.appear(BATTLE_STATUS_CF, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_CF)
            return True

        return False

    def handle_get_items(self, drop=None):
        """
        Обработка экрана получения предметов в рейдовом бою.

        Args:
            drop (DropImage): Обработчик изображений выпавшей добычи.

        Returns:
            bool: True, если получение предметов успешно обработано.
        """
        if super().handle_get_items(drop=drop):
            self.interval_reset(BATTLE_STATUS_CF)
            return True
        else:
            return False

    def handle_exp_info(self):
        """
        Обработка экрана начисления опыта в рейдовом бою.

        Returns:
            bool: True, если экран опыта успешно распознан и обработан.
        """
        if self.is_combat_executing():
            return False
        if super().handle_exp_info():
            return True
        if self.appear_then_click(EXP_INFO_C):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_D):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_CF):
            self.device.sleep((0.25, 0.5))
            return True

        return False
