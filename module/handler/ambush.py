"""Обработчик засад и воздушных налётов. Обрабатывает уклонение/перехват засад и ожидание налётов при исследовании карты."""

from module.base.timer import Timer
from module.base.utils import get_color, red_overlay_transparency
from module.combat.combat import Combat
from module.handler.assets import *
from module.handler.info_handler import info_letter_preprocess
from module.logger import logger
from module.template.assets import *

TEMPLATE_AMBUSH_EVADE_SUCCESS.pre_process = info_letter_preprocess
TEMPLATE_AMBUSH_EVADE_FAILED.pre_process = info_letter_preprocess
TEMPLATE_MAP_WALK_OUT_OF_STEP.pre_process = info_letter_preprocess


class AmbushHandler(Combat):
    """Обработчик засад и воздушных налётов, определяющий события по прозрачности красного перекрытия."""
    MAP_AMBUSH_OVERLAY_TRANSPARENCY_THRESHOLD = 0.40
    MAP_AIR_RAID_OVERLAY_TRANSPARENCY_THRESHOLD = 0.35  # Обычное значение: (0.50, 0.53)
    MAP_AIR_RAID_CONFIRM_SECOND = 0.5

    def ambush_color_initial(self):
        """Инициализирует базовые цветовые значения для засад и воздушных налётов."""
        MAP_AMBUSH.load_color(self.device.image)
        MAP_AIR_RAID.load_color(self.device.image)

    def _ambush_appear(self):
        """Проверяет появление засады."""
        return red_overlay_transparency(MAP_AMBUSH.color, get_color(self.device.image, MAP_AMBUSH.area)) > \
               self.MAP_AMBUSH_OVERLAY_TRANSPARENCY_THRESHOLD

    def _air_raid_appear(self):
        """Проверяет появление воздушного налёта."""
        return red_overlay_transparency(MAP_AIR_RAID.color, get_color(self.device.image, MAP_AIR_RAID.area)) > \
               self.MAP_AIR_RAID_OVERLAY_TRANSPARENCY_THRESHOLD

    def _handle_air_raid(self):
        """
        Ожидает исчезновения анимации воздушного налёта.
        """
        logger.info('[Карта — засада] Воздушный налёт')
        disappear = Timer(self.MAP_AIR_RAID_CONFIRM_SECOND).start()
        timeout = Timer(2.5, count=2).start()

        while 1:
            self.device.screenshot()
            # Обработка тайм-аута
            if timeout.reached():
                logger.warning('[Карта — засада] Истекло время обработки воздушного налёта; предполагается, что он исчез')
                break
            # Проверяем, исчез ли налёт
            if self._air_raid_appear():
                disappear.reset()
            else:
                if disappear.reached():
                    break

    def _handle_ambush_evade(self):
        """Обрабатывает уклонение от засады."""
        logger.info('[Карта — засада] Обнаружена засада')
        # Ждём появления MAP_AMBUSH_EVADE
        self.wait_until_appear(MAP_AMBUSH_EVADE, offset=(30, 30))
        self.handle_info_bar()

        # Нажимаем MAP_AMBUSH_EVADE
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения
            if self.info_bar_count():
                break

            if self.appear_then_click(MAP_AMBUSH_EVADE, offset=(30, 30), interval=3):
                continue

        # Обрабатываем успешное и неудачное уклонение
        image = info_letter_preprocess(self.image_crop(INFO_BAR_DETECT, copy=False))
        if TEMPLATE_AMBUSH_EVADE_SUCCESS.match(image):
            logger.attr('Уклонение от засады', 'Успешно')
        elif TEMPLATE_AMBUSH_EVADE_FAILED.match(image):
            logger.attr('Уклонение от засады', 'Неудачно')
            self.combat(expected_end='no_searching', fleet_index=self.fleet_show_index)
        else:
            logger.warning('[Карта — засада] Не удалось распознать результат уклонения от засады')
            self.ensure_no_info_bar()
            if self.combat_appear():
                self.combat(fleet_index=self.fleet_show_index)

    def _handle_ambush_attack(self):
        """Обрабатывает вступление в бой при засаде."""
        logger.info('[Карта — засада] Обнаружена засада')
        # Ждём появления MAP_AMBUSH_ATTACK
        self.wait_until_appear(MAP_AMBUSH_ATTACK, offset=(30, 30))

        # Нажимаем MAP_AMBUSH_ATTACK
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения
            if self.combat_appear():
                break

            if self.appear_then_click(MAP_AMBUSH_ATTACK, offset=(30, 30), interval=3):
                continue
            if self.handle_combat_low_emotion():
                continue
            if self.handle_retirement():
                continue

        # Входим в бой
        logger.attr('Уклонение от засады', 'Вступить в бой')
        self.combat(expected_end='no_searching', fleet_index=self.fleet_show_index)

    def _handle_ambush(self):
        """Выбирает уклонение или перехват в зависимости от конфигурации."""
        if self.config.Campaign_AmbushEvade:
            return self._handle_ambush_evade()
        else:
            return self._handle_ambush_attack()

    def handle_ambush(self):
        """Единая точка входа для обработки засад и воздушных налётов."""
        if not self.config.MAP_HAS_AMBUSH:
            return False

        if self._air_raid_appear():
            self._handle_air_raid()
            return True

        if self._ambush_appear():
            self._handle_ambush()
            return True

        if self.appear(MAP_AMBUSH_EVADE, offset=(30, 30)):
            self._handle_ambush()

        return False

    def handle_walk_out_of_step(self):
        """Обрабатывает уведомление об исчерпании шагов флота."""
        if not self.config.MAP_HAS_FLEET_STEP:
            return False
        if not self.info_bar_count():
            return False

        image = info_letter_preprocess(self.image_crop(INFO_BAR_DETECT, copy=False))
        if TEMPLATE_MAP_WALK_OUT_OF_STEP.match(image):
            logger.warning('[Карта — засада] Недостаточно шагов флота')
            self.handle_info_bar()
            return True

        return False
