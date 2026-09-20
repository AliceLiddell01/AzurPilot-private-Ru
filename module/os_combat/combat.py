"""Модуль обработчика боёв в Operation Siren.

Адаптирует боевой процесс под условия Operation Siren, наследуя стандартный обработчик
боя и обработчик событий карты. Предоставляет распознавание непрерывных боёв (сценарии сканирующих устройств Сирен),
отложенный клик по оценке S, специализированную логику получения предметов и таймер статистики боёв.
"""
from module.combat.assets import *
from module.combat.combat import Combat as Combat_
from module.logger import logger
from module.os_combat.assets import *
from module.os_handler.assets import *
from module.os_handler.map_event import MapEventHandler
from module.base.timer import Timer
from module.exception import GameBugError
from module.statistics.opsi_runtime import finish_battle_timer, start_battle_timer


class ContinuousCombat(Exception):
    """
    Исключение непрерывного боя в Operation Siren.

    В сценариях вроде сканирующих устройств Сирен враги появляются подряд без пауз.
    Если сразу после завершения одного боя начинается следующий, выбрасывается это исключение,
    которое перехватывается в combat() для повторного прогона цикла боя.
    """
    pass


class Combat(Combat_, MapEventHandler):
    """
    Обработчик боёв в Operation Siren.

    Наследует стандартный боевой процессор и обработчик событий карты, адаптируя боевой поток под Operation Siren:
    распознавание непрерывных боёв, задержка клика по оценке S, специальная логика сбора предметов.

    Attributes:
        battle_status_s_autoclick_delay (int): Задержка в секундах перед автоматическим кликом по экрану оценки S.
    """
    battle_status_s_autoclick_delay = 20

    def combat_appear(self):
        """
        Проверить, начался ли вход в бой.

        Последовательно проверяет состояние карты, загрузку боя, выполнение боя, экран подготовки и подготовку к Сирене;
        при выполнении любого условия считает, что бой начался.

        Pages:
            in: карта Operation Siren или переходный экран боя
            out: экран подготовки к бою или активный бой

        Returns:
            bool: Выполняется ли переход в бой.
        """
        if self.is_in_map():
            return False

        if self.is_combat_loading():
            return True

        # Проверяем, не выполняется ли уже бой — видна кнопка паузы
        # Обрабатываем случай, когда автопоиск пропускает экран подготовки к бою
        if self.is_combat_executing():
            return True

        if self.appear(BATTLE_PREPARATION):
            return True
        if self.appear(SIREN_PREPARATION, offset=(20, 20)):
            return True
        if self.appear(BATTLE_PREPARATION_WITH_OVERLAY) and self.handle_combat_automation_confirm():
            return True

        return False

    def _battle_status_s_timer(self):
        """
        Получить или создать таймер задержки клика по оценке S.

        В автобою Operation Siren клик по экрану оценки S выполняется с задержкой,
        чтобы не конфликтовать с автоматическим продвижением автопоиска.

        Returns:
            Timer: Экземпляр таймера задержки оценки S.
        """
        try:
            timer = self._os_battle_status_s_timer
        except AttributeError:
            timer = Timer(self.battle_status_s_autoclick_delay)
            self._os_battle_status_s_timer = timer
        if timer.limit != self.battle_status_s_autoclick_delay:
            timer = Timer(self.battle_status_s_autoclick_delay)
            self._os_battle_status_s_timer = timer
        return timer

    def _clear_battle_status_s_timer(self):
        """
        Сбросить таймер задержки клика по оценке S; вызывается при каждом появлении нового статуса боя.
        """
        self._battle_status_s_timer().clear()

    def _handle_auto_battle_status_s(self, drop=None, timer=None):
        """
        В автоматическом режиме Operation Siren ожидать перед кликом по BATTLE_STATUS_S.

        Автопоиск обычно сам перелистывает экран результата оценки S, поэтому данный обработчик служит страховкой на случай зависания клиента.
        Сначала запускает таймер и выполняет клик только по истечении порога задержки.

        Args:
            drop (DropImage): Объект изображений дропа для сохранения наград.
            timer (Timer): Опциональный внешний таймер; если None, используется внутренний.

        Returns:
            tuple: (появился ли статус, был ли обработан клик).
        """
        timer = timer or self._battle_status_s_timer()
        if not self.appear(BATTLE_STATUS_S):
            timer.clear()
            return False, False

        timer.start()
        if not timer.reached():
            return True, False

        handled = self._handle_single_battle_status(BATTLE_STATUS_S, 'S', drop)
        timer.clear()
        return True, handled

    def combat_preparation(self, balance_hp=False, emotion_reduce=False, auto='combat_auto', fleet_index=1):
        """
        Фаза подготовки к бою в Operation Siren.

        Циклически обрабатывает интерфейс подготовки, настраивает автобой, обрабатывает отставку и подтверждения,
        пока не появится экран выполнения боя.

        Pages:
            in: интерфейс подготовки к бою (BATTLE_PREPARATION или SIREN_PREPARATION)
            out: экран активного боя

        Args:
            balance_hp (bool): Балансировать ли здоровье флота.
            emotion_reduce (bool): Снижать ли настроение кораблей.
            auto (str): Режим автобоя.
            fleet_index (int): Индекс флота.
        """
        logger.info('Подготовка к бою')
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        skip_first_screenshot = True

        for _ in self.loop():

            if self.appear(BATTLE_PREPARATION):
                if self.handle_combat_automation_set(auto=auto == 'combat_auto'):
                    continue
            if self.handle_retirement():
                continue
            if self.appear_then_click(BATTLE_PREPARATION, interval=2):
                continue
            if self.appear_then_click(SIREN_PREPARATION, offset=(20, 20), interval=2):
                continue
            if self.handle_popup_confirm('ENHANCED_ENEMY'):
                continue
            if self.handle_combat_automation_confirm():
                continue
            if self.handle_story_skip():
                continue

            # Завершение
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Интерфейс боя', pause)
                break

    def _get_exp_info_sleep(self):
        """
        Возвращает диапазон времени случайного ожидания после клика по экрану опыта.

        При наличии дропа ожидает дольше (1.5-2 с), чтобы распознавание успело отработать;
        без дропа быстро пропускает (0.25-0.5 с).

        Returns:
            tuple: (минимальное число секунд, максимальное число секунд).
        """
        return (1.5, 2) if self.__os_combat_drop else (0.25, 0.5)

    def handle_exp_info(self):
        """
        Обработать экран опыта после окончания боя (оценки S/A/B/C/D).

        После клика по соответствующей кнопке оценки сбрасывает таймер оценки S и ожидает случайное время.
        Не обрабатывается, если бой ещё продолжается.

        Pages:
            in: конец боя, экран расчёта опыта
            out: экран расчёта опыта закрыт

        Returns:
            bool: Была ли нажата кнопка экрана опыта.
        """
        if self.is_combat_executing():
            return False
        sleep = self._get_exp_info_sleep()
        if self.appear_then_click(EXP_INFO_S):
            self._clear_battle_status_s_timer()
            self.device.sleep(sleep)
            return True
        if self.appear_then_click(EXP_INFO_A):
            self._clear_battle_status_s_timer()
            self.device.sleep(sleep)
            return True
        if self.appear_then_click(EXP_INFO_B):
            self._clear_battle_status_s_timer()
            self.device.sleep(sleep)
            return True
        if self.appear_then_click(EXP_INFO_C):
            self._clear_battle_status_s_timer()
            self.device.sleep(sleep)
            return True
        if self.appear_then_click(EXP_INFO_D):
            self._clear_battle_status_s_timer()
            self.device.sleep(sleep)
            return True

        return False

    def handle_get_items(self, drop=None):
        """
        Кликнуть по безопасной зоне для закрытия окна получения предметов вместо клика по самой кнопке.

        Args:
            drop (DropImage): Объект изображений дропа.

        Returns:
            bool: Было ли обработано всплывающее окно получения предметов.
        """
        if getattr(self, '_disable_handle_get_items', False):
            return False
        if self.appear(GET_ITEMS_1, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self, before=2)
            self.device.click(CLICK_SAFE_AREA)
            self._clear_battle_status_s_timer()
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True
        if self.appear(GET_ITEMS_2, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self, before=2)
            self.device.click(CLICK_SAFE_AREA)
            self._clear_battle_status_s_timer()
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True
        if self.appear(GET_ADAPTABILITY, offset=5, interval=self.battle_status_click_interval):
            if drop:
                drop.handle_add(self, before=2)
            self.device.click(CLICK_SAFE_AREA)
            self._clear_battle_status_s_timer()
            self.interval_reset(BATTLE_STATUS_S)
            self.interval_reset(BATTLE_STATUS_A)
            self.interval_reset(BATTLE_STATUS_B)
            return True

        return False

    def _os_combat_expected_end(self):
        """
        Определить, завершён ли бой в Operation Siren и выполнен ли возврат на карту.

        После обработки событий карты проверяет, не начался ли новый бой (выбрасывая исключение ContinuousCombat);
        иначе подтверждает нахождение на карте Operation Siren.

        Pages:
            in: расчёт окончания боя или события карты
            out: карта Operation Siren

        Returns:
            bool: Выполнен ли возврат на карту Operation Siren.
        """
        if self.handle_map_event(drop=self.__os_combat_drop):
            return False
        if self.combat_appear():
            raise ContinuousCombat

        return self.handle_os_in_map()

    __os_combat_drop = None

    def combat_status(self, drop=None, expected_end=None):
        """
        Обработка статуса боя в Operation Siren.

        Отключает стандартную обработку окон предметов, используя только специализированную логику Operation Siren.
        Через флаг _disable_handle_get_items временно отключает handle_get_items,
        перенаправляя обработку наград через handle_map_get_items.

        Pages:
            in: бой продолжается или идёт расчёт завершения
            out: карта Operation Siren

        Args:
            drop (DropImage): Объект изображений дропа.
            expected_end (callable): Пользовательская функция проверки завершения боя (по умолчанию _os_combat_expected_end).
        """
        self.__os_combat_drop = drop
        if expected_end is None:
            expected_end = self._os_combat_expected_end
        # Отключаем handle_get_items и используем только handle_map_get_items
        self._disable_handle_get_items = True
        try:
            super().combat_status(drop=drop, expected_end=expected_end)
        finally:
            self._disable_handle_get_items = False

    def combat(self, *args, save_get_items=False, **kwargs):
        """
        Обработать последовательные непрерывные бои в Operation Siren.

        В сканирующих устройствах Сирен появляются 2 вражеские засады подряд без пауз.
        Флот подходит к устройству Сирен, атакует одного врага, пропускает диалог TB и сразу атакует второго.
        Стандартный combat требует подтверждения выхода на карту, что приводит к зависанию на втором бою.
        Данный метод перехватывает исключение ContinuousCombat и повторяет бой (до 3 раз).

        Pages:
            in: карта Operation Siren, переход в бой
            out: карта Operation Siren, все последовательные бои завершены

        Args:
            *args: Позиционные аргументы родительского метода combat.
            save_get_items (bool): Сохранять ли скриншоты наград.
            **kwargs: Именованные аргументы родительского метода combat.
        """
        for count in range(3):
            self._clear_battle_status_s_timer()
            if count >= 2:
                logger.warning('[Операция «Сирена» — бой] Слишком много последовательных боёв')

            try:
                super().combat(*args, save_get_items=save_get_items, **kwargs)
                break
            except ContinuousCombat:
                logger.info('[Операция «Сирена» — бой] Обнаружен последовательный бой')
                continue
            finally:
                self._clear_battle_status_s_timer()

    def _handle_single_battle_status(self, status_button, status_letter, drop):
        """
        Обработать отдельную кнопку оценки боя (S/A/B/C/D).

        При обнаружении соответствующей кнопки оценки фиксирует дроп и кликает кнопку для закрытия экрана.
        Оценка S логируется на уровне info, остальные — warning.

        Args:
            status_button (Button): Ресурс кнопки статуса оценки.
            status_letter (str): Буква оценки боя ('S'/'A'/'B'/'C'/'D').
            drop (DropImage): Объект дропа; если None, только ожидает случайное время.

        Returns:
            bool: Была ли обнаружена и нажата кнопка оценки.
        """
        if self.appear(status_button, interval=self.battle_status_click_interval):
            if status_letter == 'S':
                logger.info(f'[Операция «Сирена» — бой] Оценка боя {status_letter}')
            else:
                logger.warning(f'[Операция «Сирена» — бой] Оценка боя {status_letter}')
            if drop:
                drop.handle_add(self)
            else:
                self.device.sleep((0.25, 0.5))
            self.device.click(status_button)
            return True
        return False

    def handle_battle_status(self, drop=None):
        """
        Обработать экран оценки боя Operation Siren (S/A/B/C/D).

        Во время выполнения боя сбрасывает таймер оценки S и пропускает обработку.
        Сначала через механизм задержки обрабатывает оценку S, затем последовательно проверяет A/B/C/D.

        Pages:
            in: экран оценки боя (видна любая из S/A/B/C/D)
            out: экран оценки закрыт

        Args:
            drop (DropImage): Объект изображений дропа.

        Returns:
            bool: Был ли обработан экран статуса боя.
        """
        if self.is_combat_executing():
            self._clear_battle_status_s_timer()
            return False

        appeared, clicked = self._handle_auto_battle_status_s(drop=drop)
        if clicked:
            return True
        if appeared:
            return True

        for status_button, status_letter in [
            (BATTLE_STATUS_A, 'A'),
            (BATTLE_STATUS_B, 'B'),
            (BATTLE_STATUS_C, 'C'),
            (BATTLE_STATUS_D, 'D'),
        ]:
            if self._handle_single_battle_status(status_button, status_letter, drop):
                return True
        return False

    def handle_auto_search_battle_status(self, drop=None, battle_status_s_timer=None):
        """
        Обработка статуса оценки боя в режиме автопоиска.

        Аналогично handle_battle_status, но использует переданный извне таймер оценки S
        и не проверяет состояние is_combat_executing (гарантируется вызывающей стороной).

        Pages:
            in: экран оценки боя (видна любая из S/A/B/C/D)
            out: экран оценки закрыт

        Args:
            drop (DropImage): Объект изображений дропа.
            battle_status_s_timer (Timer): Таймер задержки оценки S.

        Returns:
            bool: Был ли обработан экран статуса боя.
        """
        _, clicked = self._handle_auto_battle_status_s(
            drop=drop, timer=battle_status_s_timer
        )
        if clicked:
            return True

        for status_button, status_letter in [
            (BATTLE_STATUS_A, 'A'),
            (BATTLE_STATUS_B, 'B'),
            (BATTLE_STATUS_C, 'C'),
            (BATTLE_STATUS_D, 'D'),
        ]:
            if self._handle_single_battle_status(status_button, status_letter, drop):
                return True
        return False

    def handle_auto_search_exp_info(self):
        """
        Обработка экрана расчёта опыта в режиме автопоиска.

        Последовательно проверяет кнопки опыта S/A/B/C/D, кликает, сбрасывает таймер оценки S и ожидает случайное время.
        Длительность ожидания зависит от наличия дропа (с дропом 1.5-2 с, без дропа 0.25-0.5 с).

        Pages:
            in: экран опыта (видна любая из кнопок S/A/B/C/D)
            out: экран опыта закрыт

        Returns:
            bool: Была ли нажата кнопка экрана опыта.
        """
        sleep = self._get_exp_info_sleep()
        for exp_info_button in [EXP_INFO_S, EXP_INFO_A, EXP_INFO_B, EXP_INFO_C, EXP_INFO_D]:
            if self.appear_then_click(exp_info_button):
                self._clear_battle_status_s_timer()
                self.device.sleep(sleep)
                return True
        return False

    def auto_search_combat(self, drop=None):
        """
        Обработка боя автопоиска в Operation Siren.

        Разделена на фазы загрузки и выполнения: на фазе загрузки ожидает готовности интерфейса боя,
        на фазе выполнения обрабатывает вызов подлодок, оценки боя, экран опыта и события карты.
        В режиме метрик CL1 установлен 5-минутный лимит времени.

        Pages:
            in: загрузка боя (is_combat_loading())
            out: карта Operation Siren (статус боя полностью обработан)

        Args:
            drop (DropImage): Объект изображений дропа.

        Returns:
            bool: Уничтожены ли враги; при гибели флота возвращает False.

        Raises:
            GameBugError: Если в режиме метрик CL1 длительность боя превысила 5 минут.
        """
        # Бой отвечает только за переходы состояний; слой метрик решает, должна ли эта задача создавать выборку таймера CL1/short-meow.
        battle_timer_source = start_battle_timer(self.config)
        
        cl1_combat_timer = Timer(300, count=300)
        
        logger.info('[Операция «Сирена» — бой] Загрузка боя автопоиска')
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        self.device.screenshot_interval_set('combat')
        while 1:
            self.device.screenshot()

            if self.handle_combat_automation_confirm():
                continue

            # Завершение
            if self.handle_os_auto_search_map_option(drop=drop):
                self._clear_battle_status_s_timer()
                break
            pause = self.is_combat_executing()
            if pause:
                logger.attr('Интерфейс боя', pause)
                break
            if self.is_in_map():
                break

        logger.info('[Операция «Сирена» — бой] Выполнение боя автопоиска')
        self.submarine_call_reset()
        self.device.stuck_record_clear()
        self.device.click_record_clear()
        submarine_mode = 'do_not_use'
        if self.config.Submarine_Fleet:
            submarine_mode = self.config.Submarine_Mode

        if battle_timer_source == 'cl1':
            cl1_combat_timer.start()

        success = True
        battle_status_s_timer = Timer(self.battle_status_s_autoclick_delay)
        while 1:
            self.device.screenshot()

            if battle_timer_source == 'cl1' and cl1_combat_timer.reached():
                logger.warning('[Операция «Сирена» — бой] Истекло время боя CL1 (ограничение: 5 минут)')
                raise GameBugError('CL1 combat timeout')

            if self.handle_submarine_call(submarine_mode):
                continue
            # При неудаче не меняем настройку автопоиска
            enable = success if success is not None else None
            if self.handle_os_auto_search_map_option(drop=drop, enable=enable):
                battle_status_s_timer.clear()
                continue

            # Завершение
            if self.is_in_map():
                self.device.screenshot_interval_set()
                break
            if self.is_combat_executing():
                battle_status_s_timer.clear()
                continue
            if self.config.OpsiGeneral_RepairThreshold > 0 and self.handle_auto_search_exp_info():
                battle_status_s_timer.clear()
                success = None
                continue
            if self.handle_auto_search_battle_status(drop=drop, battle_status_s_timer=battle_status_s_timer):
                success = None
                continue
            if self.handle_map_event():
                battle_status_s_timer.clear()
                continue
            
        logger.info('Бой окончен')
        
        # Завершаем через тот же источник метрик, чтобы выборки CL1 и short-meow случайно не использовали общий ключ хранения.
        finish_battle_timer(self.config, battle_timer_source)
        
        return success
