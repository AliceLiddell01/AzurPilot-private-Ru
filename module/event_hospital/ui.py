"""Модуль обработки интерфейса события больницы.

Предоставляет функции взаимодействия с интерфейсом события больницы,
включая распознавание экрана улик, обработку всплывающих окон получения улик,
обнаружение экрана подготовки к бою и навигацию возврата.
Является базовой UI-зависимостью подмодулей события больницы.
"""

from module.combat.assets import BATTLE_PREPARATION
from module.event_hospital.assets import *
from module.logger import logger
from module.minigame.assets import BACK
from module.raid.assets import RAID_FLEET_PREPARATION
from module.ui.page import page_hospital
from module.ui.ui import UI


class HospitalUI(UI):
    """Обработчик интерфейса события больницы, управляющий переходом в экран улик и выходом из него."""

    def is_in_clue(self, interval=0):
        """Проверяет, находится ли экран в интерфейсе улик."""
        return self.appear(HOSIPITAL_CLUE_CHECK, offset=(20, 20), interval=interval)

    def handle_get_clue(self):
        """Обрабатывает всплывающее окно получения улики.

        Нажимает подтверждение при обнаружении кнопки получения улики.

        Returns:
            bool: Была ли нажата кнопка подтверждения.
        """
        if self.appear_then_click(GET_CLUE, offset=(20, 20), interval=1):
            return True
        if self.appear(GET_CLUE_TEXT, offset=(20, 20), interval=1):
            logger.info(f'{GET_CLUE_TEXT} -> {GET_CLUE}')
            self.device.click(GET_CLUE)
            return True
        return False

    def handle_clue_exit(self):
        """Обрабатывает логику выхода из интерфейса улик.

        Обнаруживает и нажимает различные кнопки возврата, включая выход из боя,
        переход на главную страницу больницы, возврат из подготовки к бою и т. д.

        Returns:
            bool: Была ли нажата кнопка возврата.
        """
        if self.appear_then_click(HOSPITAL_BATTLE_EXIT, offset=(20, 20), interval=2):
            return True
        if self.ui_page_appear(page_hospital, interval=2):
            logger.info(f'{page_hospital} -> {HOSIPITAL_GOTO_CLUE}')
            self.device.click(HOSIPITAL_GOTO_CLUE)
            return True
        if self.appear(BATTLE_PREPARATION, offset=(30, 20), interval=2):
            logger.info(f'{BATTLE_PREPARATION} -> {BACK}')
            self.device.click(BACK)
            return True
        if self.appear(RAID_FLEET_PREPARATION, offset=(30, 30), interval=2):
            logger.info(f'{RAID_FLEET_PREPARATION} -> {BACK}')
            self.device.click(BACK)
            return True
        if self.handle_get_clue():
            return True
        return False
