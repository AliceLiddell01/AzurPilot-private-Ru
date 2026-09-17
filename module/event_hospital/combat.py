"""Модуль обработки боев события больницы.

Обрабатывает боевой процесс в событии больницы, включая автоматический выбор флота,
подготовку к бою, проведение боя и обработку результатов.
Наследует Combat, HospitalUI и CampaignEvent для полного управления жизненным циклом боя.
"""

from module.base.decorator import run_once
from module.base.timer import Timer
from module.campaign.campaign_event import CampaignEvent
from module.combat.combat import BATTLE_PREPARATION, Combat
from module.event_hospital.assets import HOSPITAL_BATTLE_PREPARE
from module.event_hospital.ui import HospitalUI
from module.exception import OilExhausted, RequestHumanTakeover
from module.logger import logger
from module.map.assets import *
from module.map.map_fleet_preparation import FleetOperator
from module.raid.assets import RAID_FLEET_PREPARATION


class HospitalCombat(Combat, HospitalUI, CampaignEvent):
    """Обработчик боев события больницы, объединяющий логику боя, интерфейса и события."""

    def handle_fleet_recommend(self, recommend=True):
        """Обрабатывает выбор рекомендуемого флота.

        Проверяет, назначен ли уже флот. Если нет, в зависимости от конфигурации
        автоматически рекомендует состав либо запрашивает ручную расстановку.

        Args:
            recommend: Включен ли автоматический выбор рекомендуемого флота.

        Returns:
            bool: Была ли нажата кнопка рекомендации.

        Raises:
            RequestHumanTakeover: Если флот не готов и авто-рекомендация отключена.
        """
        fleet_1 = FleetOperator(
            choose=FLEET_1_CHOOSE, advice=FLEET_1_ADVICE, bar=FLEET_1_BAR, clear=FLEET_1_CLEAR,
            in_use=FLEET_1_IN_USE, hard_satisfied=FLEET_1_HARD_SATIESFIED, main=self)
        if fleet_1.in_use():
            return False

        if recommend:
            logger.info('Рекомендуемый флот')
            fleet_1.recommend()
            return True
        else:
            logger.error('[Госпиталь — бой] Флот не готов, а автоматическая рекомендация отключена; сформируйте флот вручную перед запуском')
            raise RequestHumanTakeover

    def combat_preparation(self, balance_hp=False, emotion_reduce=False, auto='combat_auto', fleet_index=1):
        """Фаза подготовки к бою: формирование флота и подтверждение выхода.

        Args:
            balance_hp: Балансировать ли здоровье кораблей.
            emotion_reduce: Снижать ли настроение кораблей.
            auto: Режим авто-боя.
            fleet_index: Индекс флота.
        """
        logger.info('Подготовка к бою.')
        skip_first_screenshot = True

        @run_once
        def check_oil():
            if self.get_oil() < max(500, self.config.StopCondition_OilLimit):
                logger.hr('Сработал лимит топлива')
                raise OilExhausted

        @run_once
        def check_coin():
            if self.coin_limit_triggered():
                logger.hr('Условие остановки: лимит монет')
                self.config.task_stop()
                return True
            if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
                logger.hr('Условие остановки: лимит монет')
                self.handle_task_balancer()
                return True

        for _ in self.loop():

            if self.appear(BATTLE_PREPARATION, offset=(30, 20)):
                if self.handle_combat_automation_set(auto=auto == 'combat_auto'):
                    continue
                check_oil()
                check_coin()
            if self.handle_retirement():
                continue
            if self.handle_combat_low_emotion():
                continue
            if self.appear_then_click(BATTLE_PREPARATION, offset=(30, 20), interval=2):
                continue
            if self.handle_combat_automation_confirm():
                continue
            if self.handle_story_skip():
                continue
            # Обрабатываем формирование флота.
            if self.appear(RAID_FLEET_PREPARATION, offset=(30, 30), interval=2):
                if self.handle_fleet_recommend(recommend=self.config.Hospital_UseRecommendFleet):
                    self.interval_clear(RAID_FLEET_PREPARATION)
                    continue
                self.device.click(RAID_FLEET_PREPARATION)
                continue
            if self.appear_then_click(HOSPITAL_BATTLE_PREPARE, offset=(20, 20), interval=2):
                continue

            # Бой начался.
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Боевой интерфейс', pause)
                if emotion_reduce:
                    self.emotion.reduce(fleet_index)
                break

    in_clue_confirm = Timer(0.5, count=2)

    def hospital_expected_end(self):
        """Определяет, завершился ли бой события больницы.

        Бой считается завершенным при обнаружении интерфейса улик два раза подряд.

        Returns:
            bool: Завершился ли бой.
        """
        if self.handle_clue_exit():
            return False
        if self.is_in_clue():
            self.in_clue_confirm.start()
            if self.in_clue_confirm.reached():
                return True
        else:
            self.in_clue_confirm.reset()
        return False

    def hospital_combat(self):
        """Выполняет цикл боя в событии больницы.

        Pages:
            in: FLEET_PREPARATION
            out: is_in_clue
        """
        self.combat(balance_hp=False, expected_end=self.hospital_expected_end)
