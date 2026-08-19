"""Основной обработчик списания кораблей.

Поддерживает списание в один клик и старый ручной режим, фильтрацию по редкости,
подтверждение списания и разбор снаряжения, сохранение обычного авианосца и
переход между усилением и списанием при заполненном доке.
"""

import re

from module.base.button import Button, ButtonGrid
from module.base.filter import Filter
from module.base.timer import Timer
from module.base.utils import color_similar, get_color, resize, lower_template_match_similarity
from module.combat.assets import GET_ITEMS_1
from module.exception import RequestHumanTakeover, ScriptError
from module.handler.assets import AUTO_SEARCH_MAP_OPTION_OFF, AUTO_SEARCH_MAP_OPTION_ON
from module.logger import logger
from module.retire.assets import (
    DOCK_CHECK, DOCK_SHIP_DOWN, EQUIP_CONFIRM, EQUIP_CONFIRM_2,
    GET_ITEMS_1_RETIREMENT_SAVE, IN_RETIREMENT_CHECK, ONE_CLICK_RETIREMENT,
    RETIRE_APPEAR_1, RETIRE_APPEAR_2, RETIRE_APPEAR_3, RETIRE_COIN,
    RETIRE_CONFIRM_SCROLL_AREA, SHIP_CONFIRM, SHIP_CONFIRM_2, SR_SSR_CONFIRM,
    TEMPLATE_AULICK, TEMPLATE_BOGUE, TEMPLATE_CASSIN_1, TEMPLATE_CASSIN_2,
    TEMPLATE_DOWNES_1, TEMPLATE_DOWNES_2, TEMPLATE_FOOTE, TEMPLATE_HERMES,
    TEMPLATE_LANGLEY, TEMPLATE_RANGER, TEMPLATE_Z20, TEMPLATE_Z21
)
from module.retire.enhancement import Enhancement
from module.retire.scanner import ShipScanner
from module.retire.setting import QuickRetireSettingHandler
from module.ui.scroll import Scroll

CARD_GRIDS = ButtonGrid(
    origin=(93, 76), delta=(164 + 2 / 3, 227), button_shape=(138, 204), grid_shape=(7, 2), name='CARD')
CARD_RARITY_GRIDS = ButtonGrid(
    origin=(93, 76), delta=(164 + 2 / 3, 227), button_shape=(138, 5), grid_shape=(7, 2), name='RARITY')

CARD_RARITY_COLORS = {
    'N': (174, 176, 187),
    'R': (106, 195, 248),
    'SR': (151, 134, 254),
    'SSR': (248, 223, 107)
    # Карточки с кольцом не поддерживаются.
}

RETIRE_CONFIRM_SCROLL = Scroll(RETIRE_CONFIRM_SCROLL_AREA, color=(74, 77, 110), name='STRATEGIC_SEARCH_SCROLL')
# Цвет фона — (66, 72, 77); стандартного порога 35 недостаточно для надёжного различения.
RETIRE_CONFIRM_SCROLL.color_threshold = 240

COMMON_CV_FILTER_REGEX = re.compile(
    '(bogue|hermes|langley|ranger)+?',
    flags=re.IGNORECASE)
COMMON_DD_FILTER_REGEX = re.compile(
    '(z20|z21|aulick|foote|cassin|downes)+?',
    flags=re.IGNORECASE)
FILTER_ATTR = ('ship',)
COMMON_CV_FILTER = Filter(COMMON_CV_FILTER_REGEX, FILTER_ATTR)
COMMON_DD_FILTER = Filter(COMMON_DD_FILTER_REGEX, FILTER_ATTR)

TEMPLATE_COMMON_CV = {
    'BOGUE': TEMPLATE_BOGUE,
    'HERMES': TEMPLATE_HERMES,
    'LANGLEY': TEMPLATE_LANGLEY,
    'RANGER': TEMPLATE_RANGER
}
TEMPLATE_COMMON_DD = {
    'Z20': TEMPLATE_Z20,
    'Z21': TEMPLATE_Z21,
    'AULICK': TEMPLATE_AULICK,
    'FOOTE': TEMPLATE_FOOTE,
    'CASSIN': [TEMPLATE_CASSIN_1, TEMPLATE_CASSIN_2],
    'DOWNES': [TEMPLATE_DOWNES_1, TEMPLATE_DOWNES_2]
}


class Retirement(Enhancement, QuickRetireSettingHandler):
    """Выполнять списание и связанное с ним усиление при заполненном доке.

    Обработчик объединяет ``Enhancement`` и ``QuickRetireSettingHandler`` и
    поддерживает списание в один клик, старый режим, подтверждения, разбор
    снаряжения и сохранение обычного авианосца.
    """
    _unable_to_enhance = False
    _have_kept_cv = True
    # GAME_TIPS разрешён только после подтверждённого перехода из окна заполненного дока.
    _retirement_game_tips_pending = False

    # Таймер из MapOperation для окна списания во время боя.
    map_cat_attack_timer = Timer(2)

    @property
    def retire_keep_common_cv(self):
        return self.config.is_task_enabled('GemsFarming') or self.config.is_task_enabled('ThreeOilLowCost')

    def _retirement_choose(self, amount=10, target_rarity=('N',)):
        """Выбрать на экране списания корабли заданной редкости.

        Редкость определяется по цвету карточки, после чего подходящие карточки
        нажимаются до достижения ``amount``.

        Возвращает:
            int: фактическое число выбранных карточек.
        """
        cards = []
        rarity = []
        for x, y, button in CARD_RARITY_GRIDS.generate():
            card_color = get_color(image=self.device.image, area=button.area)
            f = False
            for r, rarity_color in CARD_RARITY_COLORS.items():
                if color_similar(card_color, rarity_color, threshold=15):
                    cards.append([x, y])
                    rarity.append(r)
                    f = True

            if not f:
                logger.warning(f'[Списание — редкость] Неизвестный цвет редкости, ячейка: ({x}, {y}), цвет: {card_color}')

        logger.info('[Списание — редкость] ' + ' '.join([r.rjust(3) for r in rarity[:7]]))
        logger.info('[Списание — редкость] ' + ' '.join([r.rjust(3) for r in rarity[7:]]))

        selected = 0
        for card, r in zip(cards, rarity):
            if r in target_rarity:
                self.device.click(CARD_GRIDS[card])
                self.device.sleep((0.1, 0.15))
                selected += 1
            if selected >= amount:
                break
        return selected

    def _retirement_get_items_appear(self):
        """Распознать экран наград по цвету, сохранив шаблонный путь как резервный."""
        GET_ITEMS_1.clear_offset()
        if self.appear(GET_ITEMS_1, interval=2, threshold=20):
            return True

        GET_ITEMS_1.clear_offset()
        if self.appear(GET_ITEMS_1, offset=(30, 30), interval=2):
            return True

        GET_ITEMS_1.clear_offset()
        return False

    def _retirement_confirm(self, skip_first_screenshot=True):
        """Подтвердить списание и обработать последовательность всплывающих окон.

        Проверяются подтверждение кораблей, разбор снаряжения, получение предметов
        и подтверждение SR/SSR. Короткое ожидание после награды отличает сценарий
        без снаряжения от запаздывающего окна разбора, а общий тайм-аут остаётся
        последней защитой от бесконечного цикла.
        """
        logger.info('[Списание — подтверждение] Подтверждение списания')
        reward_handled = False
        equipment_confirm_seen = False
        equipment_reward_handled = False
        for button in [SHIP_CONFIRM, SHIP_CONFIRM_2, EQUIP_CONFIRM, EQUIP_CONFIRM_2, GET_ITEMS_1, SR_SSR_CONFIRM]:
            self.interval_clear(button)
        self.popup_interval_clear()
        completion_wait = Timer.from_seconds(3)
        timeout = Timer(10, count=10).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Завершаем по тайм-ауту как защиту от бесконечного цикла.
            if timeout.reached():
                logger.warning('[Списание — подтверждение] Тайм-аут ожидания подтверждения; считаем списание завершённым')
                break

            in_retirement = self.appear(IN_RETIREMENT_CHECK, offset=(20, 20))
            equip_confirm_visible = self.appear(EQUIP_CONFIRM, offset=(30, 30)) \
                                    or self.appear(EQUIP_CONFIRM_2, offset=(30, 30))
            if in_retirement and not equip_confirm_visible:
                if equipment_reward_handled:
                    logger.info('[Списание — подтверждение] Подтверждение завершено после награды за разбор снаряжения')
                    break
                if reward_handled and not equipment_confirm_seen:
                    completion_wait.start()
                    if completion_wait.reached():
                        logger.info('[Списание — подтверждение] Дополнительного разбора снаряжения нет; подтверждение завершено')
                        break
                else:
                    completion_wait.clear()
            else:
                completion_wait.clear()
                timeout.reset()

            # Обрабатываем окна в порядке визуальных слоёв.
            if self._unable_to_enhance \
                    or self.config.OldRetire_SR \
                    or self.config.OldRetire_SSR \
                    or self.config.Retirement_RetireMode == 'one_click_retire':
                if self.handle_popup_confirm(name='RETIRE_SR_SSR', offset=(20, 50)):
                    completion_wait.clear()
                    # Не допускаем повторного нажатия нижележащего SHIP_CONFIRM.
                    self.interval_reset([SHIP_CONFIRM, SHIP_CONFIRM_2])
                    # EQUIP_CONFIRM_2 может ошибочно совпасть с общим popup confirm.
                    self.interval_reset([EQUIP_CONFIRM, EQUIP_CONFIRM_2])
                    continue
                if self.config.SERVER in ['cn', 'jp', 'tw'] and \
                        self.appear_then_click(SR_SSR_CONFIRM, offset=(20, 50), interval=2):
                    completion_wait.clear()
                    self.interval_reset([SHIP_CONFIRM, SHIP_CONFIRM_2])
                    self.interval_reset([EQUIP_CONFIRM, EQUIP_CONFIRM_2])
                    continue
            # Подтверждение кораблей в режиме списания в один клик.
            if self.match_template_color(SHIP_CONFIRM_2, offset=(30, 30), interval=2):
                if self.retire_keep_common_cv and not self._have_kept_cv:
                    self.keep_one_common_cv()
                completion_wait.clear()
                self.device.click(SHIP_CONFIRM_2)
                # Следующим ожидается GET_ITEMS_1; очищаем его интервал.
                self.interval_clear(GET_ITEMS_1)
                self.interval_reset([SHIP_CONFIRM, SHIP_CONFIRM_2])
                continue
            # Подтверждение кораблей в старом режиме.
            if self.match_template_color(SHIP_CONFIRM, offset=(30, 30), interval=2):
                completion_wait.clear()
                self.device.click(SHIP_CONFIRM)
                continue
            # Подтверждение разбора снаряжения.
            if self.appear_then_click(EQUIP_CONFIRM, offset=(30, 30), interval=2):
                completion_wait.clear()
                equipment_confirm_seen = True
                self.interval_clear(GET_ITEMS_1)
                continue
            if self.appear_then_click(EQUIP_CONFIRM_2, offset=(30, 30), interval=2):
                completion_wait.clear()
                equipment_confirm_seen = True
                self.interval_clear(GET_ITEMS_1)
                continue
            # Экран полученных предметов.
            if self._retirement_get_items_appear():
                completion_wait.clear()
                self.device.click(GET_ITEMS_1_RETIREMENT_SAVE)
                reward_handled = True
                if equipment_confirm_seen:
                    equipment_reward_handled = True
                logger.info('[Списание — подтверждение] Экран наград обработан')
                self.interval_reset(SHIP_CONFIRM)
                # Следующим ожидается подтверждение разбора снаряжения.
                self.interval_clear([EQUIP_CONFIRM, EQUIP_CONFIRM_2])
                continue

    def retirement_appear(self):
        """Проверить появление окна списания по трём характерным шаблонам."""
        return self.appear(RETIRE_APPEAR_1, offset=30) \
               and self.appear(RETIRE_APPEAR_2, offset=30) \
               and self.appear(RETIRE_APPEAR_3, offset=30)

    def _retirement_quit(self):
        """Выйти из экранов списания/дока на предыдущую страницу."""
        def check_func():
            return not self.appear(IN_RETIREMENT_CHECK, offset=(20, 20)) \
                   and not self.appear(DOCK_CHECK, offset=(20, 20))

        self.ui_back(check_button=check_func, skip_first_screenshot=True)

    @property
    def _retire_rarity(self):
        """Вернуть набор редкостей, разрешённых настройками для списания."""
        rarity = set()
        if self.config.OldRetire_N:
            rarity.add('N')
        if self.config.OldRetire_R:
            rarity.add('R')
        if self.config.OldRetire_SR:
            rarity.add('SR')
        if self.config.OldRetire_SSR:
            rarity.add('SSR')
        return rarity

    def _retire_wait_slow_retire(self, skip_first_screenshot=True):
        """Дождаться ``SHIP_CONFIRM_2`` на медленном устройстве или большом доке."""
        logger.info('[Списание — подтверждение] Ожидание медленного списания')
        self.device.click_record_clear()
        self.device.stuck_record_clear()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(SHIP_CONFIRM_2, offset=(30, 30)):
                return True

    def _one_click_retirement_click(self):
        """Нажать кнопку списания, сохранив совместимость обычного и смещённого интерфейса."""
        ONE_CLICK_RETIREMENT.clear_offset()
        if self.appear_then_click(ONE_CLICK_RETIREMENT, interval=2):
            return True

        ONE_CLICK_RETIREMENT.clear_offset()
        if self.appear_then_click(ONE_CLICK_RETIREMENT, offset=(20, 20), interval=2):
            return True

        ONE_CLICK_RETIREMENT.clear_offset()
        return False

    def retire_ships_one_click(self):
        """Списать подходящие корабли штатной функцией «в один клик».

        Возвращает:
            int: условное количество списанных кораблей, по 10 на выполненный цикл.
        """
        logger.hr('Списание')
        logger.info('[Списание — в один клик] Используется списание в один клик')
        # Для режима в один клик не требуется ожидать полную загрузку дока.
        self.dock_favourite_set(wait_loading=False)
        self.dock_sort_method_dsc_set(wait_loading=False)
        end = False
        total = 0

        if self.retire_keep_common_cv:
            self._have_kept_cv = False

        while 1:
            self.handle_info_bar()

            # Внутренний цикл: ONE_CLICK_RETIREMENT -> SHIP_CONFIRM_2 или info_bar.
            skip_first_screenshot = True
            click_count = 0
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()
                if self.appear(SHIP_CONFIRM_2, offset=(30, 30)):
                    break
                if self.info_bar_count():
                    logger.info('[Списание — в один клик] Больше нет кораблей для списания')
                    end = True
                    break

                # После нескольких неудачных нажатий переходим к ожиданию медленного окна.
                if click_count >= 5:
                    logger.warning('[Списание — в один клик] Не удалось выбрать корабль после 5 попыток')
                    self._retire_wait_slow_retire()
                if self._one_click_retirement_click():
                    click_count += 1
                    continue

            # info_bar сообщает, что доступных для списания кораблей больше нет.
            if end:
                break
            self._retirement_confirm()
            total += 10
            # Клиент списывает подходящие корабли одной операцией; второй цикл не нужен.
            break

        logger.info(f'[Списание — в один клик] Всего циклов списания: {total // 10}')
        return total

    def retire_ships_old(self, amount=None, rarity=None):
        """В старом режиме вручную выбрать и списать заданные редкости.

        Возвращает:
            int: фактическое количество списанных кораблей.
        """
        if amount is None:
            amount = self._retire_amount
        if rarity is None:
            rarity = self._retire_rarity
        logger.hr('Списание')
        logger.info(f'[Списание — старый режим] Количество={amount}, редкость={rarity}')

        # Сопоставляем внутренние обозначения редкости с фильтром дока.
        correspond_name = {
            'N': 'common',
            'R': 'rare',
            'SR': 'elite',
            'SSR': 'super_rare'
        }
        _rarity = [correspond_name[i] for i in rarity]
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_filter_set(
            sort='level', index='all', faction='all', rarity=_rarity, extra='no_limit')

        total = 0

        if self.retire_keep_common_cv:
            self._have_kept_cv = False

        while amount:
            selected = self._retirement_choose(
                amount=10 if amount > 10 else amount, target_rarity=rarity)
            total += selected
            if selected == 0:
                break
            self.device.screenshot()
            if not self.match_template_color(SHIP_CONFIRM, offset=(30, 30)):
                logger.warning('[Списание — старый режим] Корабль не выбран; повторная попытка')
                continue

            self._retirement_confirm()

            amount -= selected
            if amount <= 0:
                break

            self.handle_dock_cards_loading()
            continue

        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set()
        logger.info(f'[Списание — старый режим] Всего списано: {total}')
        return total

    def retire_gems_farming_flagships(self, keep_one=True) -> int:
        """Списать ненужные обычные авианосцы из GemsFarming/ThreeOilLowCost.

        Выбираются свободные обычные авианосцы уровня выше 1, не находящиеся во
        флоте. При ``keep_one=True`` сохраняется один корабль минимального уровня.
        """
        logger.info('[Списание — сохранение] Списание ненужного флагмана для фарма самоцветов / низкозатратного флота на 3 нефти')

        gems_farming_enable: bool = self.config.is_task_enabled('GemsFarming') or self.config.is_task_enabled('ThreeOilLowCost')
        if not gems_farming_enable:
            logger.info('[Списание — сохранение] Задача не относится к фарму самоцветов / низкозатратному флоту на 3 нефти; пропуск')
            return 0

        self.dock_favourite_set(wait_loading=False)
        self.dock_sort_method_dsc_set(wait_loading=False)
        self.dock_filter_set(index='cv', rarity='common', extra='not_level_max', sort='level')

        scanner = ShipScanner(
            rarity='common', fleet=0, status='free', level=(2, 100))
        scanner.disable('emotion')

        total = 0
        _ = self._have_kept_cv
        self._have_kept_cv = True

        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            self.handle_info_bar()
            ships = scanner.scan(self.device.image)
            if not ships:
                # Подходящих кораблей для списания больше нет.
                break
            if keep_one:
                if len(ships) < 2:
                    break
                else:
                    # Сохраняем корабль минимального уровня.
                    ships.sort(key=lambda s: -s.level)
                    ships = ships[:-1]

            for ship in ships:
                self.device.click(ship.button)
                self.device.sleep((0.1, 0.15))
                total += 1

            self._retirement_confirm()

            # Если выбрано меньше десяти кораблей, следующей страницы уже не требуется.
            if len(ships) < 10:
                break

        self._have_kept_cv = _
        # Списание завершено; перед выходом не ждём повторной загрузки дока.
        self.dock_filter_set(wait_loading=False)

        return total

    def handle_retirement(self):
        """Обработать списание или усиление при заполненном доке.

        Режим выбирается из конфигурации. ``GAME_TIPS`` обрабатывается только
        после подтверждённого этим обработчиком перехода из окна заполненного
        дока, поскольку шаблон используется и на других экранах.
        """
        if self._retirement_game_tips_pending and self.handle_game_tips():
            self._retirement_game_tips_pending = False
            return True

        if self._unable_to_enhance:
            if self.appear_then_click(RETIRE_APPEAR_1, offset=(20, 20), interval=3):
                self._retirement_game_tips_pending = True
                self.interval_clear(IN_RETIREMENT_CHECK)
                self.interval_reset([AUTO_SEARCH_MAP_OPTION_OFF, AUTO_SEARCH_MAP_OPTION_ON])
                self.map_cat_attack_timer.reset()
                return False
            if self.appear(IN_RETIREMENT_CHECK, offset=(20, 20), interval=10):
                self._retirement_game_tips_pending = False
                try:
                    # Используем режим списания из конфигурации без локального переопределения.
                    self._retire_handler()
                    self._unable_to_enhance = False
                    self.interval_reset(IN_RETIREMENT_CHECK)
                    self.map_cat_attack_timer.reset()
                    return True
                except Exception as e:
                    logger.warning(f'[Списание — док] Списание не удалось: {e}')
                    self._unable_to_enhance = False  # Предотвращает бесконечный повтор.
                    return False
        elif self.config.Retirement_RetireMode == 'enhance':
            if self.appear_then_click(RETIRE_APPEAR_3, offset=(20, 20), interval=3):
                self._retirement_game_tips_pending = True
                self.interval_clear(DOCK_CHECK)
                self.interval_reset([AUTO_SEARCH_MAP_OPTION_OFF, AUTO_SEARCH_MAP_OPTION_ON])
                self.map_cat_attack_timer.reset()
                return False
            if self.appear(DOCK_CHECK, offset=(20, 20), interval=10):
                self._retirement_game_tips_pending = False
                self.handle_dock_cards_loading()
                try:
                    total, remain = self._enhance_handler()
                    if not total:
                        logger.info('[Списание — док] Нет кораблей для усиления, но док заполнен; пробуем списание')
                        self._unable_to_enhance = True
                    logger.info(f'[Списание — док] Осталось свободных мест в доке: {remain}')
                    if remain < 3:
                        logger.info('[Списание — док] Свободных мест в доке слишком мало; списание будет выполнено позже')
                        self._unable_to_enhance = True
                except Exception as e:
                    logger.warning(f'[Списание — док] Усиление не удалось: {e}')
                    self._unable_to_enhance = True  # Следующий проход переключится на списание.
                self.interval_reset(DOCK_CHECK)
                self.map_cat_attack_timer.reset()
                return True
        else:
            if self.appear_then_click(RETIRE_APPEAR_1, offset=(20, 20), interval=3):
                self._retirement_game_tips_pending = True
                self.interval_clear(IN_RETIREMENT_CHECK)
                self.interval_reset([AUTO_SEARCH_MAP_OPTION_OFF, AUTO_SEARCH_MAP_OPTION_ON])
                self.map_cat_attack_timer.reset()
                return False
            if self.appear(IN_RETIREMENT_CHECK, offset=(20, 20), interval=10):
                self._retirement_game_tips_pending = False
                try:
                    self._retire_handler()
                    self._unable_to_enhance = False
                    self.interval_reset(IN_RETIREMENT_CHECK)
                    self.map_cat_attack_timer.reset()
                    return True
                except Exception as e:
                    logger.warning(f'[Списание — док] Списание не удалось: {e}')
                    self._unable_to_enhance = False  # Предотвращает бесконечный повтор.
                    return False

        return False

    def _retire_handler(self, mode=None):
        """Распределить выполнение на выбранную стратегию списания.

        При режиме ``one_click_retire`` неудачная попытка постепенно ослабляет
        настройки быстрого списания. ``old_retire`` вызывает старый ручной путь.
        """
        if mode is None:
            mode = self.config.Retirement_RetireMode

        # Для ``enhance`` списание в один клик используется как резервный путь.
        if mode == 'enhance':
            logger.info('[Списание — док] Режим списания настроен на усиление; списание в один клик используется как резервный вариант')
            mode = 'one_click_retire'

        if mode == 'one_click_retire':
            total = self.retire_ships_one_click()
            if not total:
                logger.warning(
                    '[Списание — док] Корабли не списаны; сбрасываем фильтр дока, отключаем избранное и пробуем снова')
                self.dock_favourite_set(False, wait_loading=False)
                self.dock_filter_set()
                total = self.retire_ships_one_click()
            if self.server_support_quick_retire_setting_fallback():
                # Пользователь мог заранее установить filter_5='all'; сначала сохраняем эту настройку.
                if not total:
                    logger.warning('[Списание — док] Корабли не списаны; сбрасываем первые 4 настройки быстрого списания')
                    self.quick_retire_setting_set(filter_5=None)
                    total = self.retire_ships_one_click()
                if not total:
                    logger.warning('[Списание — док] Корабли не списаны; сбрасываем настройку быстрого списания на «сохранять для прорыва»')
                    self.quick_retire_setting_set(filter_5='keep_limit_break')
                    total = self.retire_ships_one_click()
                if not total and self.config.OneClickRetire_KeepLimitBreak == 'do_not_keep':
                    logger.warning('[Списание — док] Корабли не списаны; сбрасываем настройку быстрого списания на «все»')
                    self.quick_retire_setting_set('all')
                    total = self.retire_ships_one_click()
            total += self.retire_gems_farming_flagships(keep_one=total > 0)
            if not total:
                logger.critical('[Списание] Дядя-неумёха~ Тут вообще нет кораблей для списания. Вы решили разыграть шутку? ❤')
                logger.critical('[Списание] Скорее настройте «Списание в один клик» в игре! Или хотите, чтобы я нажимала всё за вас? ❤')
                logger.critical('[Списание] Хм, списание настроено неправильно, поэтому скрипту остаётся остановиться. Разберитесь с настройками и попробуйте снова~')
                raise RequestHumanTakeover
        elif mode == 'old_retire':
            self.handle_dock_cards_loading()
            total = self.retire_ships_old()
            total += self.retire_gems_farming_flagships()
            if not total:
                logger.critical('[Списание] Нет даже кораблей, которые можно списать. Вы уверены, что настройки корректны?')
                logger.critical('[Списание] С такими настройками скрипт действительно придётся остановить.')
                logger.critical('[Списание] Ни один корабль не списан. Проверьте, включены ли нужные редкости в настройках Alas.')
                raise RequestHumanTakeover
        else:
            raise ScriptError(
                f'[Списание — режим] Неизвестный режим списания: {self.config.Retirement_RetireMode}')

        self._retirement_quit()
        self.config.DOCK_FULL_TRIGGERED = True

        return total

    def _retire_select_one(self, button, skip_first_screenshot=True):
        """Убрать один корабль из списка подтверждения списания.

        Успех определяется по изменению шаблона ``RETIRE_COIN``; выполняется до
        трёх повторных попыток.
        """
        count = 0
        RETIRE_COIN.load_color(self.device.image)
        RETIRE_COIN._match_init = True
        self.interval_clear(SHIP_CONFIRM_2)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Изменение RETIRE_COIN подтверждает успешное снятие выбора.
            if not RETIRE_COIN.match(self.device.image, offset=(20, 20), similarity=0.97):
                return True
            if count > 3:
                logger.warning('[Списание — выбор] Не удалось выбрать корабль после 3 попыток')
                return False

            if self.appear(SHIP_CONFIRM_2, offset=(30, 30), interval=2):
                self.device.click(button)
                count += 1
                continue

    def get_common_ship_filter(self, string, ship_type='cv', output=True):
        """Разобрать фильтр обычных авианосцев/эсминцев в список имён.

        При некорректном фильтре используется значение по умолчанию из
        конфигурации, которое также записывается обратно в конфигурацию.
        """
        if ship_type.lower() not in ['cv', 'dd']:
            logger.warning(f'[Списание — сканирование] Недопустимый тип корабля: {ship_type}')
            return []

        ship_type = ship_type.upper()
        filter_obj: Filter = globals()[f'COMMON_{ship_type}_FILTER']
        templates = globals()[f'TEMPLATE_COMMON_{ship_type}']
        command = self.config.task.command if hasattr(self.config, 'task') and self.config.task else 'GemsFarming'
        key = f'{command}.GemsFarming.Common{ship_type}Filter'
        default = self.config.__getattribute__(f'COMMON_{ship_type}_FILTER')

        while 1:
            filter_obj.load(string)
            common_cv = list(dict.fromkeys(
                [str(name[0]) for name in filter_obj.filter if name[0].upper() in templates]))
            if not common_cv:
                logger.warning(f'[Списание — сканирование] Недопустимый фильтр: "{string}"; используется фильтр по умолчанию')
                string = default
                self.config.cross_set(keys=key, value=default)
                continue

            # Фильтр успешно разобран.
            if output:
                logger.attr('Порядок фильтра', ' > '.join(common_cv))
            return common_cv

    def retirement_get_common_rarity_cv_in_page(self):
        """Найти обычный авианосец на текущей странице по шаблону."""
        preset = self.config.GemsFarming_CommonCV
        if preset in ['custom', 'any', 'eagle']:
            filter_string = self.config.GemsFarming_CommonCVFilter if preset == 'custom' else self.config.COMMON_CV_FILTER
            common_cv = self.get_common_ship_filter(filter_string, ship_type='cv', output=False)
            if self.config.GemsFarming_CommonCV == 'eagle' and 'hermes' in common_cv:
                common_cv.remove('hermes')
            logger.attr('Порядок фильтра', ' > '.join(common_cv))
            for name in common_cv:
                template = globals()[f'TEMPLATE_{name.upper()}']
                sim, button = template.match_result(
                    resize(self.device.image, size=(1189, 669)))

                if sim > lower_template_match_similarity(self.config.COMMON_CV_THRESHOLD):
                    return Button(button=tuple(_ * 155 // 144 for _ in button.button), area=button.area,
                                  color=button.color,
                                  name=f'TEMPLATE_{name}_RETIRE')

            return None
        else:
            template = globals()[
                f'TEMPLATE_{self.config.GemsFarming_CommonCV.upper()}']
            sim, button = template.match_result(
                resize(self.device.image, size=(1189, 669)))

            if sim > lower_template_match_similarity(self.config.COMMON_CV_THRESHOLD):
                return Button(button=tuple(_ * 155 // 144 for _ in button.button), area=button.area, color=button.color,
                              name=f'TEMPLATE_{self.config.GemsFarming_CommonCV.upper()}_RETIRE')

            return None

    def retirement_get_common_rarity_cv(self, skip_first_screenshot=False):
        """Прокручивать подтверждение списания снизу вверх в поиске обычного CV."""
        swipe_count = 0
        disappear_confirm = Timer(2, count=6)
        top_checked = False
        button = None
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Сначала ищем авианосец на текущей странице.
            button = self.retirement_get_common_rarity_cv_in_page()
            if button is not None:
                return button

            # Ждём появления полосы прокрутки.
            if RETIRE_CONFIRM_SCROLL.appear(main=self):
                disappear_confirm.clear()
            else:
                disappear_confirm.start()
                if disappear_confirm.reached():
                    logger.warning('Полоса прокрутки исчезла; остановка')
                    break
                else:
                    continue

            if not top_checked:
                top_checked = True
                logger.info('Поиск обычного авианосца снизу вверх')
                RETIRE_CONFIRM_SCROLL.set_bottom(main=self)
                continue
            else:
                if RETIRE_CONFIRM_SCROLL.at_top(main=self):
                    logger.info('[Списание — прокрутка] Достигнут верх полосы прокрутки; остановка')
                    break
                # Переходим на предыдущую страницу.
                if swipe_count >= 7:
                    logger.info('[Списание — прокрутка] Достигнут лимит пролистываний при поиске обычного авианосца')
                    break
                RETIRE_CONFIRM_SCROLL.prev_page(main=self)
                swipe_count += 1

        return button

    def keep_one_common_cv(self):
        """Сохранить один обычный авианосец, сняв его с подтверждения списания."""
        logger.info('Сохранение одного обычного авианосца')
        button = self.retirement_get_common_rarity_cv()
        if button is not None:
            self._retire_select_one(button)
            self._have_kept_cv = True
        logger.info('Сохранение одного обычного авианосца завершено')
