"""Модуль зачистки совместных операций через затопление (scuttle) для набора очков.

Специализированная обработка результатов боёв совместных операций с затоплением кораблей.
Оптимизирован под оценку D (затопление), управляет списанием морали, критериями завершения боя,
а также обрабатывает специфические диалоговые окна результатов и подтверждения затопления.
"""

from module.combat.assets import (
    BATTLE_STATUS_D, BATTLE_STATUS_A, BATTLE_STATUS_B, BATTLE_STATUS_S,
    OPTS_INFO_D,
    EXP_INFO_D, EXP_INFO_A, EXP_INFO_B, EXP_INFO_S
)
from module.coalition.assets import *
from module.coalition.combat import CoalitionCombat
from module.coalition.coalition import Coalition
from module.exception import ScriptEnd, ScriptError
from module.logger import logger
from module.ui.page import page_coalition


class CoalitionScuttleCombat(CoalitionCombat):
    """Обработка результатов боя при коалиционном затоплении с приоритетным распознаванием специальных кнопок."""

    triggered_normal_end = False
    _is_shipwreck = False  # Является ли текущий бой затоплением с оценкой D
    _is_s_rank = False  # Получена ли в текущем бою оценка S

    def auto_search_combat_execute(self, emotion_reduce=True, fleet_index=1, expected_end=None):
        """
        Переопределение выполнения автоматического поиска без дополнительного списания морали.

        В совместных операциях этап состоит из нескольких боёв (флоты 1/2/3/4),
        но игра снимает 2 единицы морали только один раз при входе на этап, а не за каждый бой.
        При оценке D дополнительное снижение морали (shipwreck=True) также не производится.

        Args:
            emotion_reduce (bool): Списывать ли мораль (True только в первом бою этапа).
            fleet_index (int): Номер флота.
            expected_end (callable): Пользовательское условие завершения.
        """
        from module.base.timer import Timer
        from module.combat.assets import OPTS_INFO_D
        from module.combat.auto_search_combat import AutoSearchCombat
        from module.exception import CampaignEnd

        self.device.stuck_record_clear()
        self.device.click_record_clear()

        # При коалиционном затоплении снимаем 2 морали только в первом бою (стоимость входа на этап)
        # В последующих боях (флоты 2/3/4) мораль не уменьшается, как и на стороне игрового сервера
        if emotion_reduce:
            self.emotion.reduce(fleet_index)

        auto = self.config.Fleet_Fleet1Mode if fleet_index == 1 else self.config.Fleet_Fleet2Mode
        confirm_timer = Timer(10)
        confirm_timer.start()

        while 1:
            self.device.screenshot()

            if self.handle_submarine_call('do_not_use', call=False):
                continue
            if self.handle_combat_auto(auto):
                continue
            if self.handle_combat_manual(auto):
                continue
            if self.handle_popup_confirm('AUTO_SEARCH_COMBAT_EXECUTE'):
                continue
            if not self._withdraw and self.handle_urgent_commission():
                continue
            if self.handle_story_skip():
                continue
            if self.handle_guild_popup_cancel():
                continue
            if self.handle_vote_popup():
                continue
            if self.handle_mission_popup_ack():
                continue

            # Условие завершения
            if self.is_in_auto_search_menu() or self._handle_auto_search_menu_missing():
                self.device.screenshot_interval_set()
                raise CampaignEnd
            if self.is_combat_executing():
                confirm_timer.reset()
                continue
            if self.handle_get_ship():
                continue

            # Затопление с оценкой D: дополнительно мораль не уменьшаем
            if self.appear_then_click(OPTS_INFO_D, offset=(30, 30), interval=2):
                self._withdraw = True
                self._is_shipwreck = True
                break
            # На экране результатов с оценкой D переходные кадры анимации S/A/B могут кратковременно ошибочно совпасть с шаблоном D,
            # но только при настоящем затоплении появляется окно OPTS_INFO_D.
            # Поэтому здесь не выставляем признак затопления без подтверждения через OPTS_INFO_D, чтобы последующие условия S/A/B могли его переопределить.
            if self.appear(BATTLE_STATUS_D) or self.appear(EXP_INFO_D):
                break
            if confirm_timer.reached():
                self._withdraw = True
                self._is_shipwreck = True
                self.device.click(OPTS_INFO_D)
                confirm_timer.reset()
                break

            # Оценка A/B/S: при коалиционном затоплении дополнительно мораль не уменьшаем
            # Игровой сервер снимает 2 морали только один раз при входе на этап, а не при каждом внутреннем результате боя
            if self.appear(BATTLE_STATUS_A) or self.appear(BATTLE_STATUS_B) \
                    or self.appear(EXP_INFO_A) or self.appear(EXP_INFO_B):
                break

            # Оценка S или выполняется автоматический поиск
            if self.appear(BATTLE_STATUS_S) or self.appear(EXP_INFO_S) \
                    or self.is_auto_search_running():
                self._is_s_rank = True
                self.device.screenshot_interval_set()
                break

            if callable(expected_end):
                if expected_end():
                    self.device.screenshot_interval_set()
                    break

    def coalition_combat(self):
        """
        Проведение боёв совместной операции с затоплением со списанием морали только в первом бою.

        Этап совместной операции включает несколько боёв (флоты 1/2/3/4),
        но игра списывает 2 морали лишь единожды при входе на этап; последующие бои мораль не снижают.
        """
        from module.exception import CampaignEnd

        self.battle_count = 0
        self.combat_preparation(emotion_reduce=False)

        try:
            while 1:
                logger.hr(f'{self.FUNCTION_NAME_BASE}{self.battle_count}', level=2)
                self._is_shipwreck = False
                self._is_s_rank = False
                # Только первый бой снимает 2 морали (стоимость входа на этап); последующие бои мораль не уменьшают
                self.auto_search_combat_execute(
                    emotion_reduce=self.battle_count == 0,
                    fleet_index=1,
                    expected_end=self.auto_search_combat_end
                )
                self.coalition_combat_re_enter()
                self.battle_count += 1
        except CampaignEnd:
            logger.info('Коалиционный бой завершён.')

    def handle_battle_status(self, drop=None):
        """
        Обработка экрана результатов боя при коалиционном затоплении с приоритетом кнопок затопления.

        Последовательность обработки результатов: BATTLE_STATUS_D -> OPTS_INFO_D -> SCUTTLE_CONFIRM -> родительский класс.
        При обнаружении стандартных результатов (не D) выставляется флаг triggered_normal_end, обозначающий уничтожение корабля.

        Args:
            drop (DropImage): Обработчик изображений выпавшей добычи.

        Returns:
            bool: True, если экран результатов успешно распознан и обработан.
        """
        if self.is_combat_executing():
            return False
        if self.appear(BATTLE_STATUS_D, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(BATTLE_STATUS_D)
            return True
        if self.appear(OPTS_INFO_D, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(OPTS_INFO_D)
            return True
        # Кнопка подтверждения после результатов затопления
        if self.appear_then_click(SCUTTLE_CONFIRM, offset=(20, 20), interval=2):
            return True
        if super().handle_battle_status(drop=drop):
            logger.warning('Сработало обычное завершение')
            self.triggered_normal_end = True
            return True

        return False

    def handle_exp_info(self):
        """
        Обработка экрана начисления опыта при коалиционном затоплении.

        Returns:
            bool: True, если экран опыта успешно распознан и обработан.
        """
        if self.is_combat_executing():
            return False
        if self.appear_then_click(EXP_INFO_D):
            self.device.sleep((0.25, 0.5))
            return True
        if super().handle_exp_info():
            return True

        return False

    def coalition_combat_re_enter(self, skip_first_screenshot=True):
        """
        Повторный вход в бой после затопления с дополнительной обработкой кнопок подтверждения.

        Pages:
            in: battle_status
            out: is_combat_executing
        """
        from module.base.timer import Timer
        from module.os_ash.assets import BATTLE_STATUS

        logger.info('[Коалиция — зачистка] Повторный вход в бой после затопления')
        status_clicked = False
        click_timer = Timer(0.3)
        click_last = Timer(2)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # End
            if self.is_combat_loading():
                break
            if self.is_combat_executing():
                break
            if self.in_coalition():
                from module.exception import CampaignEnd
                raise CampaignEnd

            if self.appear_then_click(BATTLE_STATUS, offset=(80, 20), interval=2):
                continue
            if self.appear_then_click(COALITION_REWARD_CONFIRM, offset=(20, 20), interval=2):
                status_clicked = False
                continue
            # Кнопка подтверждения результатов затопления
            if self.appear_then_click(SCUTTLE_CONFIRM, offset=(20, 20), interval=2):
                continue
            if self.handle_get_ship():
                continue
            if self.handle_battle_status():
                status_clicked = True
                click_last.reset()
                continue
            if status_clicked:
                if click_timer.reached() and not click_last.reached():
                    self.device.click(BATTLE_STATUS)
                    click_timer.reset()


class CoalitionScuttleRun(Coalition, CoalitionScuttleCombat):
    """Главный цикл коалиционного затопления; за вход на этап списывается 2 единицы морали только 1 раз."""

    def handle_combat_low_emotion(self):
        """
        Переопределение обработки предупреждающего окна о низком настроении (красная мордочка).

        В сценарии затопления жертвенный корабль неизбежно имеет низкую мораль;
        при появлении предупреждения нажимается подтверждение для продолжения выхода в бой.
        """
        return self.handle_popup_confirm('IGNORE_LOW_EMOTION')

    def coalition_execute_once(self, event, stage, fleet):
        """Выполнение одного боя совместной операции при затоплении.

        Переопределяет метод базового класса, рассчитывая списание морали как за 1 бой (2 морали за весь этап).
        Несмотря на несколько внутренних боёв (флоты 1/2/3/4), игра снимает мораль только при первом входе.

        Args:
            event: Название события.
            stage: Название этапа.
            fleet: Режим флота.
        """
        self.config.override(
            Campaign_Name=f'{event}_{stage}',
            Campaign_UseAutoSearch=False,
            Fleet_FleetOrder='fleet1_all_fleet2_standby',
        )
        if self.config.Coalition_Fleet == 'single' and self.config.Emotion_Fleet1Control == 'prevent_red_face':
            logger.warning('Azur Lane не допускает коалицию с одной флотилией при морали < 30; '
                           'управление моралью принудительно переключено на prevent_yellow_face')
            self.config.override(Emotion_Fleet1Control='prevent_yellow_face')
        if stage == 'sp':
            self.config.override(Coalition_Fleet='multi')

        # Коалиционное затопление: весь этап снимает только 2 морали один раз, независимо от числа внутренних боёв
        try:
            self.emotion.check_reduce(battle=1)
        except ScriptEnd:
            self.coalition_map_exit(event)
            raise

        if self._coalition_has_oil_icon and self.triggered_stop_condition(oil_check=True, coin_check=True):
            self.coalition_map_exit(event)
            raise ScriptEnd

        self.enter_map(event=event, stage=stage, mode=fleet)
        self.coalition_combat()

    def triggered_stop_condition(self, oil_check=False, pt_check=False, coin_check=False):
        """
        Проверка срабатывания условий остановки.

        Коалиционное затопление не останавливается по флагу triggered_normal_end (потопление корабля);
        остановка управляется счётчиком RunCount. Бои с оценкой D и другими оценками считаются полноценными боями.

        Returns:
            bool: True, если условие остановки сработало.
        """
        if super().triggered_stop_condition(oil_check=oil_check, pt_check=pt_check, coin_check=coin_check):
            return True

        return False

    def run(self, event='', mode='', fleet='', total=0):
        """
        Запуск основного цикла коалиционного затопления без лишнего списания морали.

        Особая логика для этапа SP:
        - Оценка D (затопление): считается непройденным, продолжаются новые попытки
        - Оценка выше D (успех): считается пройденным, запуск откладывается до обновления сервера

        Args:
            event (str): Название события; если пусто, считывается из конфигурации.
            mode (str): Название этапа; если пусто, считывается из конфигурации.
            fleet (str): Режим флота; если пусто, считывается из конфигурации.
            total (int): Общий лимит числа запусков, 0 — без ограничений.
        """
        event = event if event else self.config.Campaign_Event
        mode = mode if mode else self.config.Coalition_Mode
        fleet = fleet if fleet else self.config.Coalition_Fleet
        if not event or not mode or not fleet:
            raise ScriptError(f'Не заполнены аргументы CoalitionScuttle. name={event}, mode={mode}, fleet={fleet}')

        event, mode = self.handle_stage_name(event, mode)
        self.run_count = 0
        self.run_limit = self.config.StopCondition_RunCount

        while 1:
            # Завершаем после достижения заданного числа запусков
            if total and self.run_count == total:
                break
            if self.event_time_limit_triggered():
                self.config.task_stop()

            # Вывод в лог
            logger.hr(f'Коалиция: {event}_{mode}', level=2)
            if self.config.StopCondition_RunCount > 0:
                logger.info(f'Осталось запусков: {self.config.StopCondition_RunCount}')
            else:
                logger.info(f'Счётчик: {self.run_count}')

            # Если значка топлива нет, сначала проверяем условия остановки в меню кампании
            if not self._coalition_has_oil_icon:
                from module.ui.page import page_campaign_menu
                self.ui_goto(page_campaign_menu)
                if self.triggered_stop_condition(oil_check=True, coin_check=True):
                    break

            # Убеждаемся, что открыта страница коалиции
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            self.ui_goto_coalition()
            self.disable_event_on_raid()
            self.coalition_ensure_mode(event, 'battle')

            # Проверяем условия остановки по PT и монетам
            if self.triggered_stop_condition(pt_check=True, coin_check=True):
                break

            # Выполняем бой
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                self.coalition_execute_once(event=event, stage=mode, fleet=fleet)
            except ScriptEnd as e:
                logger.hr('Завершение скрипта')
                logger.info(str(e))
                break

            # После боя обновляем счётчики
            self.run_count += 1
            if self.config.StopCondition_RunCount:
                self.config.StopCondition_RunCount -= 1

            # На этапе SP только оценка S считается успешным прохождением и откладывает задачу до обновления сервера
            # Оценки A/B/C/D считаются неудачными; продолжаем попытки
            if mode == 'sp' and self._is_s_rank and not self._is_shipwreck:
                logger.info('SP пройден с оценкой S')
                self.config.task_delay(server_update=True)
                self.config.task_stop()

            # Проверяем условия остановки
            if self.triggered_stop_condition(pt_check=True, coin_check=True):
                break
            # Проверяем, переключил ли планировщик задачу
            if self.config.task_switched():
                self.config.task_stop()
