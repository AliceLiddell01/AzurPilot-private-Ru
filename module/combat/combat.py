"""Базовый обработчик боевой системы.

Интегрирует все функциональные модули, связанные с боем, обеспечивая полное управление боевым процессом.

Наследует от нескольких боевых подмодулей:
- Level: Определение уровня
- HPBalancer: Управление балансировкой здоровья
- Retirement: Обработка отставки (автоотставка при заполнении доков)
- SubmarineCall: Вызов подводных лодок
- CombatAuto: Режим автобоя
- CombatManual: Режим ручного боя
- AutoSearchHandler: Обработка автопоиска

Боевой процесс:
1. combat_appear() — обнаружение экрана боя
2. handle_combat_automation() — настройка автоматического/ручного режима
3. handle_combat_low_emotion() — обработка предупреждения о низком настроении
4. handle_retirement() — обработка подсказки об отставке
5. Ожидание завершения боя (combat_status)
6. Обработка результатов боя (опыт, дроп и т. д.)
"""

import numpy as np

from module.base.timer import Timer
from module.base.utils import color_similar, get_color, lower_template_match_similarity
from module.combat.assets import *
from module.combat.combat_auto import CombatAuto
from module.combat.combat_manual import CombatManual
from module.combat.hp_balancer import HPBalancer
from module.combat.level import Level
from module.combat.submarine import SubmarineCall
from module.combat_ui.assets import *
from module.handler.auto_search import AutoSearchHandler
from module.logger import logger
from module.map.assets import MAP_OFFENSIVE
from module.retire.retirement import Retirement
from module.statistics.azurstats import DropImage
from module.template.assets import TEMPLATE_COMBAT_LOADING
from module.ui.assets import BACK_ARROW, EXERCISE_CHECK, MUNITIONS_CHECK


class Combat(Level, HPBalancer, Retirement, SubmarineCall, CombatAuto, CombatManual, AutoSearchHandler):
    """Базовый обработчик боевой системы.

    Интегрирует определение уровней, балансировку здоровья, отставку кораблей, подводные лодки,
    автоматический/ручной бой и автопоиск, обеспечивая полный жизненный цикл боя от начала до завершения.

    Объединяет возможности подмодулей через множественное наследование, где каждый подмодуль
    отвечает за определённый аспект боя.

    Attributes:
        _automation_set_timer (Timer): Таймер защиты от дребезга при переключении режима автоматизации.
        battle_status_click_interval (int): Интервал кликов по экрану статуса боя.
    """
    _automation_set_timer = Timer(1)
    battle_status_click_interval = 0

    def combat_appear(self):
        """
        Определяет, произошёл ли переход к экрану боя.

        Returns:
            bool: Перешла ли игра в состояние подготовки к бою или экран загрузки боя.
        """
        if self.config.Campaign_UseFleetLock and not self.is_in_map():
            if self.is_combat_loading():
                return True

        if self.appear(BATTLE_PREPARATION, offset=(30, 20)):
            return True
        if self.appear(BATTLE_PREPARATION_WITH_OVERLAY, threshold=30) and self.handle_combat_automation_confirm():
            return True

        return False

    def map_offensive(self, skip_first_screenshot=True):
        """
        Pages:
            in: in_map, MAP_OFFENSIVE
            out: combat_appear
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(MAP_OFFENSIVE, interval=1):
                continue
            if self.handle_combat_low_emotion():
                self.interval_reset(MAP_OFFENSIVE)
                continue
            if self.handle_retirement():
                continue

            # Обнаружен экран боя, выход из цикла
            if self.combat_appear():
                break
    def is_combat_loading(self):
        """
        Определяет, отображается ли экран загрузки боя.

        Проверяется сопоставлением шаблона полосы загрузки в нижней части экрана;
        ресурсы CN/EN/TW одинаковы, на JP персонаж меньше.

        Returns:
            bool: Находится ли игра в состоянии загрузки боя.
        """
        image = self.image_crop((0, 620, 1280, 690), copy=False)
        # Ресурсы полосы загрузки CN/EN/TW одинаковы, на JP размер персонажа меньше
        similarity, button = TEMPLATE_COMBAT_LOADING.match_luma_result(image)
        if similarity > lower_template_match_similarity(0.85):
            loading = (button.area[0] + 38 - LOADING_BAR.area[0]) / (LOADING_BAR.area[2] - LOADING_BAR.area[0])
            logger.attr('Ход загрузки', f'{int(loading * 100)}%')
            return True
        if self.is_combat_executing():
            logger.warning('[Бой — загрузка] Обнаружено состояние боя, но индикатор загрузки не найден')
            return True
        return False

    def is_combat_executing(self):
        """
        Определяет, выполняется ли бой в данный момент (видима ли кнопка паузы).

        Перебирает скины кнопок паузы для всех серверов и возвращает совпавшую кнопку.

        Returns:
            Button | bool: Совпавшая кнопка паузы, либо False, если совпадений нет.
        """
        self.device.stuck_record_add(PAUSE)
        if self.config.SERVER in ['cn', 'en']:
            if PAUSE.match_luma(self.device.image, offset=(10, 10)):
                return PAUSE
        else:
            color = get_color(self.device.image, PAUSE.area)
            if color_similar(color, PAUSE.color) or color_similar(color, (238, 244, 248)):
                if np.max(self.image_crop(PAUSE_DOUBLE_CHECK, copy=False)) < 153:
                    return PAUSE
        if PAUSE_New.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_New
        if PAUSE_Iridescent_Fantasy.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_Iridescent_Fantasy
        if PAUSE_Christmas.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_Christmas
        # PAUSE_New, PAUSE_Cyber и PAUSE_Neon внешне похожи, различаются по цвету
        if PAUSE_Neon.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Neon
        if PAUSE_Cyber.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Cyber
        if PAUSE_HolyLight.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_HolyLight
        # У PAUSE_Pharaoh есть случайная анимация: ресурс должен избегать центральной области и использовать match_luma
        if PAUSE_Pharaoh.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_Pharaoh
        # PAUSE_Star может быть ошибочно распознан как PAUSE_Nurse, требует приоритетной проверки
        if PAUSE_Star.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_Star
        if PAUSE_Nurse.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_Nurse
        # PAUSE_Devil — красная тема
        if PAUSE_Devil.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Devil
        # PAUSE_Seaside — светло-синяя тема
        if PAUSE_Seaside.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Seaside
        if PAUSE_Ninja.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Ninja
        if PAUSE_ShadowPuppetry.match_luma(self.device.image, offset=(10, 10)):
            return PAUSE_ShadowPuppetry
        if PAUSE_MaidCafe.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_MaidCafe
        if PAUSE_Ancient.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Ancient
        if PAUSE_SpringInn.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_SpringInn
        if PAUSE_ElvenVine.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_ElvenVine
        if PAUSE_GildedReverie.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_GildedReverie
        if PAUSE_AzureCore.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_AzureCore
        if PAUSE_Nier.match_template_color(self.device.image, offset=(10, 10)):
            return PAUSE_Nier
        return False

    def handle_combat_quit(self, offset=(20, 20), interval=3):
        """
        Обрабатывает кнопку выхода из боя (выход из меню паузы).

        Перебирает скины кнопок выхода для всех серверов и кликает по совпавшей кнопке.

        Args:
            offset: Смещение сопоставления кнопки.
            interval: Интервал клика в секундах.

        Returns:
            bool: Была ли нажата кнопка выхода.
        """
        timer = self.get_interval_timer(QUIT, interval=interval)
        if not timer.reached():
            return False
        if QUIT.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT)
            timer.reset()
            return True
        if QUIT_New.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_New)
            timer.reset()
            return True
        if QUIT_Iridescent_Fantasy.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Iridescent_Fantasy)
            timer.reset()
            return True
        # В боевом интерфейсе PAUSE_Neon используется QUIT_New
        # В боевом интерфейсе PAUSE_Cyber используется QUIT_New
        # [TW] QUIT_New полужирный, PAUSE_Cyber обычной насыщенности
        if QUIT_Cyber.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Cyber)
            timer.reset()
            return True
        if QUIT_Christmas.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Christmas)
            timer.reset()
            return True
        # В боевом интерфейсе PAUSE_HolyLight используется QUIT_New
        if QUIT_Pharaoh.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Pharaoh)
            timer.reset()
            return True
        if QUIT_Nurse.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Nurse)
            timer.reset()
            return True
        # В боевом интерфейсе PAUSE_Devil используется QUIT_New
        if QUIT_Seaside.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Seaside)
            timer.reset()
            return True
        if QUIT_Ninja.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Ninja)
            timer.reset()
            return True
        if QUIT_MaidCafe.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_MaidCafe)
            timer.reset()
            return True
        if QUIT_SpringInn.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_SpringInn)
            timer.reset()
            return True
        if QUIT_GildedReverie.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_GildedReverie)
            timer.reset()
            return True
        if QUIT_Nier.match_luma(self.device.image, offset=offset):
            self.device.click(QUIT_Nier)
            timer.reset()
            return True
        return False

    def handle_combat_quit_reconfirm(self, interval=2):
        # Интервал QUIT_RECONFIRM должен быть короче QUIT для нескольких повторных попыток в пределах интервала QUIT
        if self.appear_then_click(QUIT_RECONFIRM, offset=(20, 20), interval=interval):
            # Сбрасываем таймер QUIT во избежание повторного клика QUIT, отменяющего QUIT_RECONFIRM
            self.interval_reset(QUIT)
            return True
        return False

    def ensure_combat_oil_loaded(self):
        """Ожидает стабилизации значения расхода нефти в бою."""
        self.wait_until_stable(COMBAT_OIL_LOADING)

    def handle_combat_automation_confirm(self):
        """
        Обрабатывает всплывающее окно подтверждения автоматизации боя.

        Returns:
            bool: Была ли нажата кнопка подтверждения.
        """
        if self.appear(AUTOMATION_CONFIRM_CHECK, threshold=30, interval=1):
            self.appear_then_click(AUTOMATION_CONFIRM, offset=(20, 20))
            return True

        return False

    def combat_preparation(self, balance_hp=False, emotion_reduce=False, auto='combat_auto', fleet_index=1):
        """
        Фаза подготовки к бою: настройка режима автоматизации, обработка отставки и настроения, ожидание входа в бой.

        Pages:
            in: BATTLE_PREPARATION
            out: is_combat_executing (видима кнопка паузы)

        Args:
            balance_hp: Выполнять ли балансировку здоровья перед боем.
            emotion_reduce: Ожидать ли восстановления настроения перед боем.
            auto: Режим автоматического боя ('combat_auto' или другой режим).
            fleet_index: Индекс флота (1 или 2).
        """
        logger.info('[Бой — подготовка] Подготовка к бою')
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        skip_first_screenshot = True
        interval_set = False

        if emotion_reduce:
            self.emotion.wait(fleet_index=fleet_index)
        if balance_hp:
            self.hp_balance()

        for _ in self.loop():

            if self.appear(BATTLE_PREPARATION, offset=(20, 20)):
                if self.handle_combat_automation_set(auto=auto == 'combat_auto'):
                    continue
            if self.handle_retirement():
                continue
            if self.handle_combat_low_emotion():
                continue
            if balance_hp and self.handle_emergency_repair_use():
                continue
            if self.handle_battle_preparation():
                continue
            if self.handle_combat_automation_confirm():
                continue
            if self.handle_story_skip():
                continue
            # Заблаговременно снижаем частоту скриншотов
            if not interval_set:
                if self.is_combat_loading():
                    self.device.screenshot_interval_set('combat')
                    interval_set = True

            # Обнаружено выполнение боя, выход из фазы подготовки
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Боевой UI', pause)
                if emotion_reduce:
                    self.emotion.reduce(fleet_index)
                # Если экран загрузки не обнаружен, резервно снижаем частоту скриншотов
                if not interval_set:
                    self.device.screenshot_interval_set('combat')
                break

    def handle_battle_preparation(self):
        """
        Кликает по кнопке подготовки к бою.

        Returns:
            bool: Была ли нажата кнопка подготовки к бою.
        """
        if self.appear_then_click(BATTLE_PREPARATION, offset=(20, 20), interval=2):
            return True

        return False

    def handle_combat_automation_set(self, auto):
        """
        Устанавливает состояние переключателя автоматизации боя.

        Определяет текущее состояние автоматизации (вкл/выкл) и кликает для переключения,
        если оно не совпадает с целевым.

        Args:
            auto: Включать ли автобой.

        Returns:
            bool: Была ли выполнена операция переключения.
        """
        if not self._automation_set_timer.reached():
            return False

        if self.appear(AUTOMATION_ON):
            logger.info('[Бой — автоматизация] Автобой включён')
            if not auto:
                self.device.click(AUTOMATION_SWITCH)
                self.device.sleep(1)
                self._automation_set_timer.reset()
                return True

        if self.appear(AUTOMATION_OFF):
            logger.info('[Бой — автоматизация] Автобой выключен')
            if auto:
                self.device.click(AUTOMATION_SWITCH)
                self.device.sleep(1)
                self._automation_set_timer.reset()
                return True

        if self.handle_combat_automation_confirm():
            self._automation_set_timer.reset()
            return True

        return False

    def handle_emergency_repair_use(self):
        if not self.config.HpControl_UseEmergencyRepair:
            return False

        if self.appear_then_click(EMERGENCY_REPAIR_CONFIRM, offset=True, interval=3):
            return True
        if self.appear(BATTLE_PREPARATION, offset=(20, 20)) and self.appear(EMERGENCY_REPAIR_AVAILABLE):
            # При входе на страницу подготовки к бою (или после аварийного ремонта) иконка ремонта активна по умолчанию, даже без доступных предметов.
            # Фактическое состояние отображается только после короткой анимации.
            # Используем значение боевой мощи флота как детектор стабильности: сначала ждем ненулевого значения, затем стабилизации числа.
            self.wait_until_disappear(MAIN_FLEET_POWER_ZERO, offset=(20, 20))
            stable_checker = Button(
                area=MAIN_FLEET_POWER_ZERO.area, color=(), button=MAIN_FLEET_POWER_ZERO.button, name='STABLE_CHECKER')
            self.wait_until_stable(stable_checker)
            if not self.appear(EMERGENCY_REPAIR_AVAILABLE):
                return False

            logger.info('[Бой — ремонт] Доступен аварийный ремонт')
            if not len(self.hp):
                return False
            if max(self.hp[:3]) <= 0.001 or max(self.hp[3:]) <= 0.001:
                logger.warning(f'[Бой — ремонт] Недопустимое значение здоровья при использовании аварийного ремонта: {self.hp}')
                return False

            hp = np.array(self.hp)
            hp = hp[hp > 0.001]
            if (len(hp) and np.min(hp) < self.config.HpControl_RepairUseSingleThreshold) \
                    or max(self.hp[:3]) < self.config.HpControl_RepairUseMultiThreshold \
                    or max(self.hp[3:]) < self.config.HpControl_RepairUseMultiThreshold:
                logger.info('[Бой — ремонт] Используется аварийный ремонт')
                self.device.click(EMERGENCY_REPAIR_AVAILABLE)
                self.interval_clear(EMERGENCY_REPAIR_CONFIRM)
                return True

        return False

    def combat_execute(self, auto='combat_auto', submarine='do_not_use', drop=None):
        """
        Фаза выполнения боя: обработка автоматического/ручного боя, вызов подводных лодок, всплывающих окон, ожидание подведения итогов боя.

        Pages:
            in: is_combat_executing (видима кнопка паузы)
            out: BATTLE_STATUS / GET_ITEMS (экран подведения итогов боя)

        Args:
            auto: Боевой режим, варианты: 'combat_auto', 'combat_manual', 'stand_still_in_the_middle', 'hide_in_bottom_left'.
            submarine: Режим подводных лодок, варианты: 'do_not_use', 'hunt_only', 'every_combat'.
            drop: Объект записи дропа для ведения статистики.
        """
        logger.info('[Бой — выполнение] Бой выполняется')
        self.submarine_call_reset()
        self.combat_auto_reset()
        self.combat_manual_reset()
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        confirm_timer = Timer(10)
        confirm_timer.start()

        for _ in self.loop():

            if not confirm_timer.reached():
                if self.handle_combat_automation_confirm():
                    continue

            if self.handle_story_skip():
                continue
            if self.handle_combat_auto(auto):
                continue
            if self.handle_combat_manual(auto):
                continue
            if auto != 'combat_auto' and self.auto_mode_checked and self.is_combat_executing():
                if self.handle_combat_weapon_release():
                    continue
            if self.handle_submarine_call(submarine):
                continue
            # Обработка различных всплывающих окон
            if self.handle_popup_confirm('COMBAT_EXECUTE'):
                continue
            if self.handle_urgent_commission():
                continue
            if self.handle_guild_popup_cancel():
                continue
            if self.handle_vote_popup():
                continue
            if self.handle_mission_popup_ack():
                continue

            # Завершение боя, выход из цикла
            if self.handle_battle_status(drop=drop) \
                    or self.handle_get_items(drop=drop):
                break

    def handle_battle_status(self, drop=None):
        """
        Обрабатывает экран результатов боя (оценки S/A/B/C/D).

        Проверяет, завершился ли бой, затем по приоритету сопоставляет экран итогов каждого ранга.

        Args:
            drop: Объект записи дропа для статистических снимков.

        Returns:
            bool: Был ли совершен клик по экрану результатов.
        """
        if self.is_combat_executing():
            return False
        if self.appear(BATTLE_STATUS_S, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_S)
            return True
        if self.appear(BATTLE_STATUS_A, interval=self.battle_status_click_interval):
            logger.warning('[Бой — результаты] Оценка боя: A')
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_A)
            return True
        if self.appear(BATTLE_STATUS_B, interval=self.battle_status_click_interval):
            logger.warning('[Бой — результаты] Оценка боя: B')
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_B)
            return True
        if self.appear(BATTLE_STATUS_C, interval=self.battle_status_click_interval):
            logger.warning('[Бой — результаты] Оценка боя: C')
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_C)
            return True
        if self.appear(BATTLE_STATUS_D, interval=self.battle_status_click_interval):
            logger.warning('[Бой — результаты] Оценка боя: D')
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_D)
            return True

        return False

    def handle_get_items(self, drop=None):
        """
        Обрабатывает экран выпавших предметов боя.

        Определяет три типа экранов выпадения GET_ITEMS_1/2/3 и нажимает на них.

        Args:
            drop: Объект записи дропа для статистических снимков.

        Returns:
            bool: Был ли совершён клик по экрану выпадения предметов.
        """
        if self.appear(GET_ITEMS_1, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            self.device.click(GET_ITEMS_1)
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True
        if self.appear(GET_ITEMS_2, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            self.device.click(GET_ITEMS_1)
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True
        if self.appear(GET_ITEMS_3, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            self.device.click(GET_ITEMS_1)
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True

        return False

    def handle_exp_info(self):
        """
        Обрабатывает экран начисления опыта (оценки S/A/B/C/D).

        Returns:
            bool: Был ли совершён клик по экрану начисления опыта.
        """
        if self.is_combat_executing():
            return False
        if self.appear_then_click(EXP_INFO_S):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_A):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_B):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_C):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_D):
            self.device.sleep((0.25, 0.5))
            return True

        return False

    def handle_get_ship(self, drop=None):
        """
        Обрабатывает экран получения нового корабля.

        Определяет кнопку GET_SHIP и кликает по ней; при наличии отметки NEW_SHIP фиксирует получение нового корабля.

        Args:
            drop: Объект записи дропа для статистических снимков.

        Returns:
            bool: Был ли совершен клик по экрану получения корабля.
        """
        if self.appear_then_click(GET_SHIP, interval=1):
            if self.appear(NEW_SHIP):
                logger.info('[Бой — корабль] Получен новый корабль')
                if drop:
                    drop.handle_add(self)
                self.config.GET_SHIP_TRIGGERED = True
            return True

        return False

    def handle_combat_mis_click(self):
        """
        Обрабатывает случайные нажатия во время боя (случайный переход на страницу снабжения или учений).

        Pages:
            in: MUNITIONS_CHECK или EXERCISE_CHECK
            out: Возврат на предыдущую страницу

        Returns:
            bool: Было ли обработано случайное нажатие.
        """
        if self.appear(MUNITIONS_CHECK, offset=(20, 20), interval=5):
            logger.info(f'[Бой — ошибочное нажатие] Случайно открыта страница снабжения {MUNITIONS_CHECK} -> {BACK_ARROW}')
            self.device.click(BACK_ARROW)
            return True
        if self.appear(EXERCISE_CHECK, offset=(20, 20), interval=5):
            logger.info(f'[Бой — ошибочное нажатие] Случайно открыта страница учений {EXERCISE_CHECK} -> {BACK_ARROW}')
            self.device.click(BACK_ARROW)
            return True

        return False

    def combat_status(self, drop=None, expected_end=None):
        """
        Фаза подведения итогов боя: обработка оценки боя, начисления опыта, выпавших предметов, получения кораблей и т. д. до возврата на ожидаемую страницу.

        Pages:
            in: BATTLE_STATUS / GET_ITEMS (экран подведения итогов боя)
            out: В зависимости от expected_end возвращает на различные экраны (карта, выбор этапа и т. д.)

        Args:
            drop: Объект записи дропа для статистических снимков.
            expected_end: Ожидаемое состояние завершения, варианты: 'with_searching', 'no_searching', 'in_stage', 'in_ui',
                либо пользовательская функция обратного вызова.
        """
        logger.info('[Бой — результаты] Подведение итогов боя')
        logger.attr('Ожидаемое состояние завершения', expected_end.__name__ if callable(expected_end) else expected_end)
        self.device.screenshot_interval_set()
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        battle_status = False
        exp_info = False  # Для обработки бага с белым экраном в игре
        for _ in self.loop():

            # Проверка ожидаемого конечного состояния
            if isinstance(expected_end, str):
                if expected_end == 'in_stage' and self.handle_in_stage():
                    break
                if expected_end == 'with_searching' and self.handle_in_map_with_enemy_searching(drop=drop):
                    break
                if expected_end == 'no_searching' and self.handle_in_map_no_enemy_searching(drop=drop):
                    break
                if expected_end == 'in_ui' and self.appear(BACK_ARROW, offset=(30, 30)):
                    break
            if callable(expected_end):
                if expected_end():
                    break

            if self.handle_story_skip(drop=drop):
                continue
            # Обработка экрана результатов боя
            if self.handle_get_ship(drop=drop):
                continue
            if self.handle_get_items(drop=drop):
                continue
            if self.handle_popup_confirm('COMBAT_STATUS'):
                if battle_status and not exp_info:
                    logger.info('[Бой — корабль] Блокировка нового корабля')
                    self.config.GET_SHIP_TRIGGERED = True
                continue
            if not battle_status:
                if not exp_info and self.handle_battle_status(drop=drop):
                    battle_status = True
                    continue
                if self.handle_exp_info():
                    exp_info = True
                    continue
            else:
                # После клика по оценке боя приоритетно проверяем экран начисления опыта
                if self.handle_exp_info():
                    exp_info = True
                    continue
                if not exp_info and self.handle_battle_status(drop=drop):
                    battle_status = True
                    continue
            # Обработка различных всплывающих окон
            if self.handle_popup_confirm('COMBAT_STATUS'):
                continue
            if self.handle_urgent_commission(drop=drop):
                continue
            if self.handle_guild_popup_cancel():
                continue
            if self.handle_vote_popup():
                continue
            if self.handle_mission_popup_ack():
                continue
            # Дополнительные обработчики во время боя
            if self.handle_auto_search_exit(drop=drop):
                continue
            if self.handle_combat_mis_click():
                continue

            # Обнаружен экран выбора уровня, выход из цикла
            if self.handle_in_stage():
                break
            if expected_end is None:
                if self.handle_in_map_with_enemy_searching(drop=drop):
                    break

    def combat(self, balance_hp=None, emotion_reduce=None, auto_mode=None, submarine_mode=None,
               save_get_items=None, expected_end=None, fleet_index=1):
        """
        Выполняет полный цикл боя: подготовка → выполнение → подведение итогов.

        Если параметры равны None, используются значения по умолчанию из конфигурации пользователя.

        Args:
            balance_hp: Выполнять ли балансировку здоровья; при None считывается из конфигурации.
            emotion_reduce: Управлять ли настроением; при None считывается из конфигурации.
            auto_mode: Режим боя, варианты: 'combat_auto', 'combat_manual',
                'stand_still_in_the_middle', 'hide_in_bottom_left'.
            submarine_mode: Режим подводных лодок, варианты: 'do_not_use', 'hunt_only', 'every_combat'.
            save_get_items: Сохранять ли скриншоты выпавших предметов, принимает объект DropImage или False для отключения.
            expected_end: Ожидаемое состояние завершения (строка или функция обратного вызова).
            fleet_index: Индекс флота (1 или 2).
        """
        balance_hp = balance_hp if balance_hp is not None else self.config.HpControl_UseHpBalance
        emotion_reduce = emotion_reduce if emotion_reduce is not None else self.emotion.is_calculate
        if auto_mode is None:
            auto_mode = self.config.Fleet_Fleet1Mode if fleet_index == 1 else self.config.Fleet_Fleet2Mode
        if submarine_mode is None:
            submarine_mode = 'do_not_use'
            if self.config.Submarine_Fleet:
                submarine_mode = self.config.Submarine_Mode
        self.battle_status_click_interval = 7 if save_get_items else 0

        with self.stat.new(
                genre=self.config.campaign_name, method=self.config.DropRecord_CombatRecord
        ) as drop:
            if save_get_items is False:
                drop = None
            elif isinstance(save_get_items, DropImage):
                drop = save_get_items
            self.combat_preparation(
                balance_hp=balance_hp, emotion_reduce=emotion_reduce, auto=auto_mode, fleet_index=fleet_index)
            self.combat_execute(
                auto=auto_mode, submarine=submarine_mode, drop=drop)
            self.combat_status(
                drop=drop, expected_end=expected_end)

        logger.info('[Бой — завершение] Бой завершён')
