"""Статистика боевого статуса.

Распознает название флота противника через OCR по скриншотам завершения боя,
используется в системе статистики дропа для сохранения информации о врагах этапа.
"""

from module.base.decorator import cached_property
from module.combat.assets import BATTLE_STATUS_S
from module.ocr.ocr import Ocr
from module.statistics.assets import ENEMY_NAME


class BattleStatusStatistics:
    def appear_on(self, image):
        return BATTLE_STATUS_S.appear_on(image)

    @cached_property
    def ocr_object(self):
        return Ocr(ENEMY_NAME, lang='azur_lane', threshold=128, name='ENEMY_NAME')

    def stats_battle_status(self, image):
        """Распознает название противника по скриншоту боевого статуса.

        Args:
            image (np.ndarray): Скриншот боевого статуса.

        Returns:
            str: Название противника, например 'Medium Main Fleet'.
        """
        result = self.ocr_object.ocr(image)
        # Удаляем символы, ошибочно распознанные OCR.
        for letter in '-一个―~(':
            result = result.replace(letter, '')

        return result
