"""Операции на карте и подготовка к бою.

Модуль предоставляет базовые действия кампании: переключение и подготовку
флотов, вход на этап, смену режима сложности, обработку подготовки карты,
отступление, пропуск атаки Мяуфицера и учёт обратного порядка флотов.

``MapOperation`` объединяет обработчики тайников, подготовки флота, списания и
ускорения, необходимые для полного цикла входа на карту.
"""

import cv2

from module.base.timer import Timer
from module.exception import CampaignEnd, RequestHumanTakeover, ScriptEnd
from module.handler.fast_forward import FastForwardHandler
from module.handler.mystery import MysteryHandler
from module.logger import logger
from module.map.assets import *
from module.map.map_fleet_preparation import FleetPreparation
from module.retire.retirement import Retirement
from module.ui.assets import BACK_ARROW, DAILY_CHECK


class MapOperation(MysteryHandler, FleetPreparation, Retirement, FastForwardHandler):
    """Обработчик основных операций на карте кампании.

    Объединяет вход на этап, переключение флотов, отступление, смену режима и
    связанные обработчики подготовки.

    Атрибуты:
        map_cat_attack_timer: таймер проверки атаки Мяуфицера.
        map_clear_percentage_prev: последнее значение процента прохождения.
        map_clear_percentage_timer: таймер стабилизации процента прохождения.
        fleet_show_index: номер флота, отображаемого на экране.
        fleet_current_index: логический номер текущего флота с учётом разворота.
    """

    map_cat_attack_timer = Timer(2)
    map_clear_percentage_prev = -1
    map_clear_percentage_timer = Timer(0.3, count=1)

    # Номер флота, отображаемого на экране.
    fleet_show_index = 1
    # Это не то же самое, что ``get_fleet_current_index()``:
    # логический индекс 1 означает флот для обычных врагов, 2 — флот босса.
    fleet_current_index = 1

    def get_fleet_show_index(self):
        """Получить номер флота, который сейчас отображается на экране.

        Возвращает:
            int: 1 или 2.

        Страница:
            in_map.
        """
        if self.appear(FLEET_NUM_1, offset=(20, 20)):
            self.fleet_show_index = 1
            return 1
        elif self.appear(FLEET_NUM_2, offset=(20, 20)):
            self.fleet_show_index = 2
            return 2
        else:
            logger.warning('[Карта — операция] Индекс текущего флота неизвестен; используется 1')
            self.fleet_show_index = 1
            return 1

    def get_fleet_current_index(self):
        """Получить логический номер текущего флота с учётом разворота порядка."""
        if self.fleets_reversed:
            self.fleet_current_index = 3 - self.fleet_show_index
            return self.fleet_current_index
        else:
            self.fleet_current_index = self.fleet_show_index
            return self.fleet_current_index

    def fleet_set(self, index=None, skip_first_screenshot=True):
        """Переключиться на целевой логический флот.

        Аргументы:
            index (int): целевой ``fleet_current_index``.
            skip_first_screenshot (bool): пропустить ли первый снимок экрана.

        Возвращает:
            bool: выполнялось ли переключение.
        """
        logger.info(f'[Карта — операция] Выбран флот {index}')
        timeout = Timer(5, count=10).start()
        count = 0
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('[Карта — операция] Истекло время выбора флота; предполагаю, что текущий флот выбран верно')
                break

            if self.handle_story_skip():
                timeout.reset()
                continue
            if self.handle_in_stage():
                timeout.reset()
                continue

            self.get_fleet_show_index()
            self.get_fleet_current_index()
            logger.info(f'[Карта — операция] Отображаемый флот: {self.fleet_show_index}, индекс текущего флота: {self.fleet_current_index}')
            if self.fleet_current_index == index:
                break
            elif self.appear_then_click(SWITCH_OVER):
                count += 1
                self.device.sleep((1, 1.5))
                timeout.reset()
                continue
            else:
                logger.warning('[Карта — операция] Кнопка переключения не найдена')
                continue

        return count > 0

    def enter_map(self, button, mode='normal', skip_first_screenshot=True):
        """Войти на этап кампании.

        Аргументы:
            button: кнопка этапа.
            mode (str): ``normal`` или ``hard``.
            skip_first_screenshot (bool): пропустить ли первый снимок экрана.
        """
        logger.hr('Вход на карту')
        campaign_timer = Timer(5)
        map_timer = Timer(5)
        fleet_timer = Timer(5)
        campaign_click = 0
        map_click = 0
        fleet_click = 0
        checked_in_map = False
        self.stage_entrance = button
        self.map_clear_percentage_prev = -1
        self.map_clear_percentage_timer.reset()

        with self.stat.new(
                genre=self.config.campaign_name, method=self.config.DropRecord_CombatRecord
        ) as drop:
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()

                # Проверяем ошибочные циклы нажатий.
                if campaign_click > 5:
                    logger.critical(f"[Карта] Не удалось открыть {button}: выполнено слишком много нажатий на {button}.")
                    logger.critical("[Карта] Возможная причина #1: уровень командира ещё недостаточен для открытия этого этапа.")
                    raise RequestHumanTakeover
                if fleet_click > 5:
                    logger.critical(f"[Карта] Не удалось открыть {button}: выполнено слишком много нажатий на FLEET_PREPARATION")
                    logger.critical("[Карта] Возможная причина #1: "
                                    "флот ещё не соответствует требованиям характеристик этого этапа.")
                    logger.critical("[Карта] Возможная причина #2: "
                                    "этот этап можно пройти только один раз в день, "
                                    "а это уже вторая попытка входа")
                    raise RequestHumanTakeover

                # Если уже на карте, повторный вход не нужен.
                if not checked_in_map and self.is_in_map():
                    logger.info('[Карта — операция] Уже на карте; вход пропущен')
                    return False
                else:
                    checked_in_map = True

                # Обрабатываем случайный переход на ежедневную проверку.
                if self.appear(DAILY_CHECK, offset=(20, 20), interval=3):
                    logger.info(f'{DAILY_CHECK} -> {BACK_ARROW}')
                    self.device.click(BACK_ARROW)
                    continue

                # Подготовка карты.
                if map_timer.reached() and self.handle_map_mode_switch(mode) and self.handle_map_preparation():
                    self.map_get_info()
                    self.handle_map_walk_speedup()
                    self.handle_fast_forward()
                    self.handle_auto_search()
                    if self.triggered_map_stop():
                        self.enter_map_cancel()
                        self.handle_map_stop()
                        raise ScriptEnd(f'Условие прохождения: {self.config.StopCondition_MapAchievement}')
                    self.device.click(MAP_PREPARATION)
                    map_click += 1
                    map_timer.reset()
                    campaign_timer.reset()
                    continue

                # Подготовка флота.
                if fleet_timer.reached() and self.appear(FLEET_PREPARATION, offset=(20, 50)):
                    if mode == 'normal' or mode == 'hard':
                        self.handle_2x_book_setting(mode='prep')
                        self.fleet_preparation()
                        self.handle_auto_submarine_call_disable()
                        self.handle_auto_search_setting()
                        self.map_fleet_checked = True
                    self.device.click(FLEET_PREPARATION)
                    fleet_click += 1
                    fleet_timer.reset()
                    campaign_timer.reset()
                    continue

                # Продолжение автопоиска.
                if self.handle_auto_search_continue(drop=drop):
                    campaign_timer.reset()
                    continue

                # Списание кораблей.
                if self.handle_retirement():
                    continue

                # Использование ключа данных.
                if self.handle_use_data_key():
                    continue

                # Всплывающее окно поддержки подлодок на 16-1/16-2.
                if self.handle_submarine_support_popup():
                    continue

                # Обработка низкого настроения.
                if self.handle_combat_low_emotion():
                    continue

                # Срочная комиссия.
                if self.handle_urgent_commission(drop=drop):
                    continue

                # Окно удвоения опыта.
                if self.handle_2x_book_popup():
                    continue

                if self.handle_submarine_cost_popup():
                    continue

                # Пропуск сюжета.
                if self.handle_story_skip():
                    campaign_timer.reset()
                    continue

                # Нажатие кнопки входа на этап.
                if campaign_timer.reached() and self.appear_then_click(button):
                    campaign_click += 1
                    campaign_timer.reset()
                    continue

                # Проверяем завершение входа.
                if self.map_is_auto_search:
                    if self.is_auto_search_running():
                        logger.info('[Карта — операция] Обнаружен выполняющийся автопоиск')
                        break
                    if hasattr(self, 'is_combat_loading') and self.is_combat_loading():
                        logger.warning('[Карта — операция] При входе на карту появился экран загрузки боя')
                        break
                else:
                    if hasattr(self, 'is_combat_loading') and self.is_combat_loading():
                        logger.warning('[Карта — операция] При входе на карту появился экран загрузки боя')
                        break
                    if self.handle_in_map_with_enemy_searching():
                        break

        return True

    def enter_map_cancel(self, skip_first_screenshot=True):
        """Отменить вход на карту и вернуться из окна подготовки к выбору этапа.

        Аргументы:
            skip_first_screenshot (bool): пропустить ли первый снимок экрана.

        Возвращает:
            bool: всегда ``True``.
        """
        logger.hr('Отмена входа на карту')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Проверка завершения.
            if self.is_in_stage():
                break

            if self._map_preparation_appear(interval=2):
                self.device.click(MAP_PREPARATION_CANCEL)
                continue
            if self.appear(FLEET_PREPARATION, offset=(20, 50), interval=2):
                self.device.click(MAP_PREPARATION_CANCEL)
                continue

        return True

    def handle_map_mode_switch(self, mode):
        """Переключить режим сложности карты.

        Аргументы:
            mode (str): ``normal`` или ``hard``.

        Возвращает:
            bool: соответствует ли текущий режим требуемому. Для карты без
            переключателя режима всегда возвращается ``True``.
        """
        if not self.config.MAP_HAS_MODE_SWITCH:
            return True

        if mode == 'normal':
            if self.match_template_color(MAP_MODE_SWITCH_NORMAL, offset=(20, 20)):
                logger.attr('Режим карты', 'Обычный')
                return True
            if self._is_mod_switch_hard_appear(active=False, interval=2):
                logger.attr('Режим карты', 'Сложный')
                MAP_MODE_SWITCH_NORMAL.clear_offset()
                self.device.click(MAP_MODE_SWITCH_NORMAL)
                self.interval_reset(MAP_MODE_SWITCH_HARD)
            return False
        elif mode == 'hard':
            if self._is_mod_switch_hard_appear(active=True):
                logger.attr('Режим карты', 'Сложный')
                return True
            if self.match_template_color(MAP_MODE_SWITCH_NORMAL, offset=(20, 20), interval=2):
                logger.attr('Режим карты', 'Обычный')
                MAP_MODE_SWITCH_HARD.clear_offset()
                self.device.click(MAP_MODE_SWITCH_HARD)
                return False
            return False
        else:
            logger.attr('Режим карты', 'Неизвестный')
            return False

    def _is_mod_switch_hard_appear(self, active=True, interval=0):
        """Проверить наличие кнопки переключения сложного режима.

        Перебираются все поддерживаемые шаблоны кнопки.

        Аргументы:
            active (bool): требуется ли подтверждение активного состояния.
            interval (int): интервал проверки в секундах.

        Возвращает:
            bool: найдена ли подходящая кнопка и, при необходимости, активна ли она.
        """
        if interval:
            interval = self.get_interval_timer(MAP_MODE_SWITCH_HARD, interval=interval)
            if not interval.reached():
                return False

        for button in [
            MAP_MODE_SWITCH_HARD,
            MAP_MODE_SWITCH_HARD2,
            MAP_MODE_SWITCH_HARD3,
            MAP_MODE_SWITCH_HARD4,
            MAP_MODE_SWITCH_HARD5,
            MAP_MODE_SWITCH_HARD6,
        ]:
            if self.appear(button, offset=(20, 20), similarity=0.7):
                if active:
                    return self._is_mod_switch_hard_active(button)
                else:
                    return True
        return False

    def _is_mod_switch_hard_active(self, button):
        """Определить по цвету, активна ли кнопка сложного режима.

        У активной кнопки более половины пикселей значка имеют максимальный
        RGB-канал выше 235.
        """
        image = self.image_crop(button.button)
        # Берём максимум трёх RGB-каналов.
        r, g, b = cv2.split(image)
        cv2.max(r, g, dst=r)
        cv2.max(r, b, dst=r)
        # У активной кнопки белый значок; считаем пиксели ярче 235.
        cv2.inRange(r, 235, 255, dst=r)
        sum_ = cv2.countNonZero(r)
        total = r.shape[0] * r.shape[1]
        return sum_ / total > 0.5

    def _map_preparation_appear(self, interval=0):
        """Проверить наличие кнопки подготовки карты.

        Для одноразовых этапов допускается цветовая проверка штатной области
        ``MAP_PREPARATION``, если строгий шаблон не совпал. Это сохраняет строгую
        проверку для обычных карт и покрывает вариант кнопки GO с тем же
        устойчивым цветовым контрактом.
        """
        if self.appear(MAP_PREPARATION, offset=(20, 20), interval=interval):
            return True
        if self.config.MAP_IS_ONE_TIME_STAGE and self.appear(
            MAP_PREPARATION, interval=interval
        ):
            logger.info(
                '[Карта — операция] Кнопка подготовки одноразового этапа '
                'распознана по цвету'
            )
            return True
        return False

    def handle_map_preparation(self):
        """Обработать подготовку карты и дождаться завершения анимации информации.

        Возвращает:
            bool: кнопка ``MAP_PREPARATION`` обнаружена и информация карты готова.
        """
        if not self._map_preparation_appear():
            self.map_clear_percentage_prev = -1
            self.map_clear_percentage_timer.reset()
            return False
        if not self.config.MAP_HAS_CLEAR_PERCENTAGE:
            logger.attr('На карте отображается процент прохождения', self.config.MAP_HAS_CLEAR_PERCENTAGE)
            return True
        if self.config.MAP_IS_ONE_TIME_STAGE:
            logger.attr('Карта является одноразовым этапом', self.config.MAP_IS_ONE_TIME_STAGE)
            return True
        # Информационная панель перекрывает шкалу прогресса и MAP_GREEN.
        if self.info_bar_count():
            return False

        percent = self.get_map_clear_percentage()
        logger.attr('Процент прохождения карты', f'{int(percent * 100)}%')
        # Шкала сначала отображает 100%, затем растёт от 0% до фактического значения.
        # Логика остаётся активной и когда ``percent`` начинает расти от нуля.
        if percent > 0.95 and 0 <= self.map_clear_percentage_prev < 0.95:
            # При достижении 100% можно сразу завершить ожидание.
            return True
        if abs(percent - self.map_clear_percentage_prev) < 0.02:
            self.map_clear_percentage_prev = percent
            if self.map_clear_percentage_timer.reached():
                return True
            else:
                return False
        else:
            self.map_clear_percentage_prev = percent
            self.map_clear_percentage_timer.reset()
            return False

    def withdraw(self, skip_first_screenshot=True):
        """Отступить с текущей карты."""
        logger.hr('Отступление с карты')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(FLEET_SWITCH_CONFIRM, offset=(30, 30)):
                continue
            if self.handle_popup_confirm('WITHDRAW'):
                continue
            if self.appear_then_click(WITHDRAW, interval=5):
                continue
            if self.handle_auto_search_exit():
                continue
            # Обрабатываем случайный переход на ежедневную проверку.
            if self.appear(DAILY_CHECK, offset=(20, 20), interval=3):
                logger.info(f'{DAILY_CHECK} -> {BACK_ARROW}')
                self.device.click(BACK_ARROW)
                continue

            # Завершение подтверждается возвратом на экран выбора этапа.
            if self.handle_in_stage():
                raise CampaignEnd('Отступление')

    def handle_map_cat_attack(self):
        """Пропустить анимацию атаки Мяуфицера или вражеской атаки."""
        if not self.map_cat_attack_timer.reached():
            return False
        if self.image_color_count(MAP_CAT_ATTACK, color=(255, 231, 123), threshold=221, count=100):
            logger.info('[Карта — операция] Атака Мяуфицера на карте пропущена')
            self.device.click(MAP_CAT_ATTACK)
            self.map_cat_attack_timer.reset()
            return True
        if not self.map_is_clear_mode:
            # Для средней угрозы наблюдается около 106 пикселей, а у
            # ``MAP_CAT_ATTACK_MIRROR`` — около 290, поэтому порог оставляем 200.
            if self.image_color_count(MAP_CAT_ATTACK_MIRROR, color=(255, 231, 123), threshold=221, count=200):
                logger.info('[Карта — операция] Вражеская атака на карте пропущена')
                self.device.click(MAP_CAT_ATTACK)
                self.map_cat_attack_timer.reset()
                return True

        return False

    @property
    def fleets_reversed(self):
        if not self.config.FLEET_2:
            return False
        return self.config.Fleet_FleetOrder in ['fleet1_boss_fleet2_mob', 'fleet1_standby_fleet2_all']

    def handle_fleet_reverse(self):
        """Обработать обратный порядок флотов.

        Игра обычно выбирает флот с меньшим номером первым независимо от выбора
        на экране подготовки. После обновления автопоиска пользовательский
        порядок учитывается корректнее.

        Возвращает:
            bool: изменился ли выбранный флот.
        """
        if not self.map_is_hard_mode \
                and self.config.Fleet_FleetOrder in ['fleet1_boss_fleet2_mob', 'fleet1_standby_fleet2_all']:
            logger.warning(f"[Карта] В обычном режиме нельзя использовать обратный порядок флотов ({self.config.Fleet_FleetOrder}).")
            logger.warning('[Карта] Поменяйте местами настройки флота 1 и флота 2; '
                           'используйте "fleet1_mob_fleet2_boss" или "fleet1_all_fleet2_standby"')

        if not self.fleets_reversed:
            return False

        return self.fleet_set(index=2)
