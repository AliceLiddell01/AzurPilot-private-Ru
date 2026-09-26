"""
Модуль выполнения боёв в учениях.

Обрабатывает боевой процесс в учениях, включая:
- Выбор соперника и вход на экран подготовки к бою
- Выполнение боя и обработку экранов расчёта результатов
- Мониторинг низкого уровня здоровья и выход из боя при необходимости
- Управление снаряжением (экипировка перед учениями, снятие после)

В процессе боя автоматически отслеживаются экраны результатов с оценками S/D, опыт, полученные предметы,
а также обрабатываются всплывающие события (срочные заказы, голосования и т. д.).
"""
from module.combat.combat import *
from module.exercise.assets import *
from module.exercise.equipment import ExerciseEquipment
from module.exercise.hp_daemon import HpDaemon
from module.exercise.opponent import OPPONENT, OpponentChoose
from module.ui.assets import EXERCISE_CHECK


class ExerciseCombat(HpDaemon, OpponentChoose, ExerciseEquipment, Combat):
    """
    Обработчик боёв в учениях, объединяющий выбор соперника, мониторинг здоровья и управление снаряжением.

    Наследуется от HpDaemon (мониторинг здоровья), OpponentChoose (выбор соперника),
    ExerciseEquipment (управление снаряжением) и Combat (логика боя),
    предоставляя полный цикл выполнения боёв в учениях.

    Цикл боя: выбор соперника -> подготовка -> выполнение боя -> расчёт результатов -> возврат.
    """

    def _in_exercise(self):
        """Проверка, находится ли сейчас на главной странице учений."""
        return self.appear(EXERCISE_CHECK, offset=(20, 20))

    def _combat_preparation(self, skip_first_screenshot=True):
        """
        Обработка экрана подготовки к бою, нажатие кнопки начала боя для входа в сражение.

        Args:
            skip_first_screenshot (bool): Пропускать ли первый снимок экрана.
        """
        logger.info('[Учения — бой] Подготовка к бою')
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(BATTLE_PREPARATION, offset=(20, 20), interval=2):
                # self.equipment_take_on()
                pass

                self.device.click(BATTLE_PREPARATION)
                continue

            # Завершение
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Тема боевого интерфейса', pause)
                break

    def _combat_execute(self):
        """
        Выполнение боя.

        Returns:
            bool: True при победе, False при выходе из боя.
        """
        logger.info('[Учения — бой] Выполнение боя')
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        self.low_hp_confirm_timer = Timer(1.5, count=2).start()
        show_hp_timer = Timer(5)
        pause_interval = Timer(0.5, count=1)
        # Кнопка паузы используется для определения темы боевого UI
        pause = None
        success = True
        end = False
        battle_status_detected = False  # находимся ли на экране результатов боя
        while 1:
            self.device.screenshot()
            # Завершение
            if self._in_exercise() or self.appear(BATTLE_PREPARATION, offset=(20, 20)):
                logger.hr('Бой завершён')
                if not end:
                    logger.warning('[Учения — бой] Бой завершён, но условие завершения не обнаружено')
                break
            p = self.is_combat_executing()
            if p:
                if end:
                    end = False
                if pause is None:
                    pause = p
            else:
                self.low_hp_confirm_timer.reset()
                # Результат боя — оценка S или D
                if self.appear(BATTLE_STATUS_S, interval=1):
                    logger.info(f'[Учения — бой] {BATTLE_STATUS_S} -> {CLICK_SAFE_AREA}')
                    self.device.click(CLICK_SAFE_AREA)
                    success = True
                    end = True
                    battle_status_detected = True
                    continue
                if self.appear(BATTLE_STATUS_D, interval=1):
                    logger.info(f'[Учения — бой] {BATTLE_STATUS_D} -> {CLICK_SAFE_AREA}')
                    self.device.click(CLICK_SAFE_AREA)
                    success = True
                    end = True
                    battle_status_detected = True
                    logger.info('[Учения — бой] Учения проиграны')
                    continue

            # Обрабатываем GET_ITEMS_1 только после экрана результатов боя
            if battle_status_detected and self.appear(GET_ITEMS_1, offset=(30, 30), interval=1):
                logger.info(f'[Учения — бой] {GET_ITEMS_1} -> {CLICK_SAFE_AREA}')
                self.device.click(CLICK_SAFE_AREA)
                continue
            if self.appear(EXP_INFO_S, interval=1):
                logger.info(f'[Учения — бой] {EXP_INFO_S} -> {CLICK_SAFE_AREA}')
                self.device.click(CLICK_SAFE_AREA)
                continue
            if self.appear(EXP_INFO_D, interval=1):
                logger.info(f'[Учения — бой] {EXP_INFO_D} -> {CLICK_SAFE_AREA}')
                self.device.click(CLICK_SAFE_AREA)
                continue
            # Финальный экран оценки D
            if self.appear_then_click(OPTS_INFO_D, offset=(30, 30), interval=1):
                success = True
                end = True
                logger.info('[Учения — бой] Учения проиграны')
                continue
            # Выход
            if self.handle_combat_quit():
                pause_interval.reset()
                success = False
                end = True
                continue
            if self.handle_combat_quit_reconfirm():
                pause_interval.reset()
                continue
            if not end:
                if p and self._at_low_hp(image=self.device.image, pause=pause):
                    logger.info('[Учения — бой] Выход из учений')
                    if pause_interval.reached():
                        self.device.click(p)
                        pause_interval.reset()
                        continue
                else:
                    if show_hp_timer.reached():
                        show_hp_timer.reset()
                        self._show_hp()
            # Обработка всплывающих окон
            if self.handle_popup_confirm('EXERCISE_COMBAT_EXECUTE'):
                continue
            if self.handle_urgent_commission():
                continue
            if self.handle_guild_popup_cancel():
                continue
            if self.handle_vote_popup():
                continue
            if self.handle_mission_popup_ack():
                continue
        return success

    def _choose_opponent(self, index, skip_first_screenshot=True):
        """
        Выбор соперника.

        Args:
            index (int): Слева направо, от 0 до 3.
        """
        logger.hr('Противник: %s' % str(index))
        opponent_timer = Timer(5)
        preparation_timer = Timer(5)

        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if opponent_timer.reached() and self._in_exercise():
                self.device.click(OPPONENT[index, 0])
                opponent_timer.reset()

            if preparation_timer.reached() and self.appear_then_click(EXERCISE_PREPARATION):
                # self.device.sleep(0.3)
                preparation_timer.reset()
                opponent_timer.reset()
                continue

            # Завершение
            if self.appear(BATTLE_PREPARATION, offset=(20, 20)):
                break

    def _preparation_quit(self):
        """Возврат с экрана подготовки к бою на главную страницу учений."""
        logger.info('[Учения — бой] Выход из экрана подготовки')
        self.ui_back(check_button=self._in_exercise, appear_button=BATTLE_PREPARATION, skip_first_screenshot=True)

    def _combat(self, opponent):
        """
        Выполнение одного боя.

        Args:
            opponent(int): Слева направо, от 0 до 3.

        Returns:
            bool: True при победе, False при исчерпании попыток.
        """
        self._choose_opponent(opponent)

        trial = self.config.Exercise_OpponentTrial
        if not isinstance(trial, int) or trial < 1:
            logger.warning(f'[Учения — бой] Недопустимое число попыток противника: {trial}; исправлено на 1')
            self.config.Exercise_OpponentTrial = 1

        for n in range(1, self.config.Exercise_OpponentTrial + 1):
            logger.hr('Попытка: %s' % n)
            self._combat_preparation()
            success = self._combat_execute()
            if success:
                return success

        self._preparation_quit()
        return False

    def equipment_take_off_when_finished(self):
        """Снятие снаряжения после завершения учений."""
        if self.config.EXERCISE_FLEET_EQUIPMENT is None:
            return False
        if not self.equipment_has_take_on:
            return False

        self._choose_opponent(0)
        super().equipment_take_off()
        self._preparation_quit()

    # def equipment_take_on(self):
    #     if self.config.EXERCISE_FLEET_EQUIPMENT is None:
    #         return False
    #     if self.equipment_has_take_on:
    #         return False
    #
    #     self._choose_opponent(0)
    #     super().equipment_take_on()
    #     self._preparation_quit()
