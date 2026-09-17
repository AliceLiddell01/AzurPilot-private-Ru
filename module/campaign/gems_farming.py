"""
Модуль фарма алмазов (через срочные поручения).

Реализует автоматизированный процесс фарма алмазов путём циклического прохождения этапов низкой сложности
для вызова срочных поручений (Emergency Commission). Ключевая логика:
- Использование авианосцев обычной редкости в качестве флагмана (низкий уровень, легко восполняются после отставки)
- Опциональная ротация эсминцев авангарда
- Автоматическая установка и снятие снаряжения через коды снаряжения флагмана и авангарда
- Отслеживание настроения: автоматическая замена кораблей при падении настроения
- Ограничение 32-го уровня: автоматическая замена флагмана по достижении 32-го уровня (настраивается)
- Адаптация под сложный режим: использование иных экранов и кнопок входа во флот

Типичный сценарий: фарм этапа 2-4, прокачка флагмана до 32-го уровня с последующей заменой на новый авианосец 1-го уровня,
получение алмазов за выполнение срочных поручений.

Зависимости:
- CampaignRun: каркас выполнения кампании
- FleetEquipment: управление снаряжением
- EquipmentCodeHandler: импорт и экспорт кодов снаряжения
- Retirement: отставка кораблей и управление доком
"""

from module.base.decorator import cached_property
from module.campaign.assets import CHAPTER_NEXT, CHAPTER_PREV
from module.campaign.campaign_base import CampaignBase
from module.campaign.run import CampaignRun
from module.combat.assets import BATTLE_PREPARATION, EXP_INFO_C, EXP_INFO_D, OPTS_INFO_D
from module.combat.emotion import Emotion
from module.equipment.assets import (
    EMPTY_SHIP_R,
    FLEET_DETAIL, FLEET_DETAIL_CHECK, FLEET_DETAIL_ENTER, FLEET_DETAIL_ENTER_FLAGSHIP,
    FLEET_DETAIL_ENTER_FLAGSHIP_HARD_1, FLEET_DETAIL_ENTER_FLAGSHIP_HARD_2,
    FLEET_DETAIL_ENTER_HARD_1, FLEET_DETAIL_ENTER_HARD_2,
    FLEET_ENTER, FLEET_ENTER_FLAGSHIP,
    FLEET_ENTER_FLAGSHIP_HARD_1, FLEET_ENTER_FLAGSHIP_HARD_2,
    FLEET_ENTER_HARD_1, FLEET_ENTER_HARD_2,
    FLEET_NEXT, FLEET_PREV
)
from module.equipment.equipment_code import EquipmentCodeHandler
from module.equipment.fleet_equipment import FleetEquipment, OCR_FLEET_INDEX
from module.exception import CampaignEnd, HardNotSatisfied, ScriptError, RequestHumanTakeover
from module.retire.retirement import Retirement, TEMPLATE_COMMON_CV, TEMPLATE_COMMON_DD
from module.retire.assets import DOCK_CHECK, DOCK_SHIP_DOWN, TEMPLATE_BOGUE, TEMPLATE_HERMES, TEMPLATE_LANGLEY, TEMPLATE_RANGER, TEMPLATE_CASSIN_1, TEMPLATE_CASSIN_2, TEMPLATE_DOWNES_1, TEMPLATE_DOWNES_2, TEMPLATE_AULICK, TEMPLATE_FOOTE
from module.handler.assets import AUTO_SEARCH_MAP_OPTION_OFF
from module.logger import logger
from module.map.assets import FLEET_PREPARATION, MAP_PREPARATION
from module.retire.scanner import ShipScanner
from module.ui.assets import BACK_ARROW, FLEET_CHECK
from module.ui.page import page_fleet

SIM_VALUE = 0.9


class GemsEmotion(Emotion):
    """Специализированный класс управления настроением для фарма алмазов.

    Переопределяет проверку настроения: при обнаружении низкого настроения выбрасывает исключение CampaignEnd
    вместо ожидания восстановления, чтобы инициировать процедуру ротации кораблей.

    Attributes:
        Наследует все атрибуты класса Emotion.
    """
    def check_reduce(self, battle):
        """
        Переопределяет emotion.check_reduce().
        Проверяет настроение перед входом в бой кампании.

        Args:
            battle (int): Количество боёв в текущей кампании.

        Raises:
            CampaignEnd: Приостанавливает текущую задачу во избежание проблем с контролем настроения.
        """
        if not self.is_calculate:
            return

        recovered, delay = self._check_reduce(battle)
        if delay:
            self.config.GEMS_EMOTION_TRIGGERED = True
            logger.info('[Фарм самоцветов] Обнаружено низкое настроение; текущая задача приостановлена')
            raise CampaignEnd('Emotion control')

    def wait(self, fleet_index):
        pass


class GemsCampaignOverride(CampaignBase):
    """Класс переопределения кампании для фарма алмазов.

    Переопределяет обработку низкого настроения в бою и экранов опыта из CampaignBase:
    - При низком настроении игнорирует предупреждение либо отступает для ротации кораблей согласно конфигурации
    - Поддерживает обработку различных окон завершения боя и опыта
    """
    def handle_combat_low_emotion(self):
        """
        Переопределяет info_handler.handle_combat_low_emotion().
        Если включена ротация авангарда, выходит из боя для замены флагмана и авангарда.
        """
        if self.config.GemsFarming_IgnoreEmotionWarning or self.config.GemsFarming_ChangeVanguard == 'disabled':
            result = self.handle_popup_confirm('IGNORE_LOW_EMOTION')
            if result:
                # Не нажимаем AUTO_SEARCH_MAP_OPTION_OFF
                self.interval_reset(AUTO_SEARCH_MAP_OPTION_OFF)
                if self.config.GemsFarming_IgnoreEmotionWarning and self.config.GemsFarming_ChangeVanguard != 'disabled':
                    self.config.GEMS_EMOTION_TRIGGERED = True
            return result

        if self.handle_popup_cancel('IGNORE_LOW_EMOTION'):
            self.config.GEMS_EMOTION_TRIGGERED = True
            logger.hr('[Фарм самоцветов] Отступление из-за настроения')

            while 1:
                self.device.screenshot()

                if self.handle_story_skip():
                    continue
                if self.handle_popup_cancel('IGNORE_LOW_EMOTION'):
                    continue

                if self.appear(BATTLE_PREPARATION, offset=(20, 20), interval=2):
                    self.device.click(BACK_ARROW)
                    continue
                if self.handle_auto_search_exit():
                    continue
                if self.is_in_stage():
                    break

                if self.is_in_map():
                    self.withdraw()
                    break

                if self.appear(FLEET_PREPARATION, offset=(20, 50), interval=2) \
                        or self.appear(MAP_PREPARATION, offset=(20, 20), interval=2):
                    self.enter_map_cancel()
                    break
            raise CampaignEnd('Emotion withdraw')

    def handle_exp_info(self):
        if self.is_combat_executing():
            return False
        if super().handle_exp_info():
            return True
        if self.appear_then_click(EXP_INFO_C, threshold=10):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(EXP_INFO_D):
            self.device.sleep((0.25, 0.5))
            return True
        if self.appear_then_click(OPTS_INFO_D, offset=True, similarity=0.9):
            self.device.sleep((0.25, 0.5))
            return True
        return False


class GemsEquipmentHandler(EquipmentCodeHandler):
    """Обработчик снаряжения для фарма алмазов.

    Наследуется от EquipmentCodeHandler, предоставляя функции импорта и экспорта кодов снаряжения.
    Автоматически определяет путь ключа конфигурации снаряжения по текущему типу флагмана (авианосец/эсминец).

    Attributes:
        Наследует все атрибуты EquipmentCodeHandler.
    """


    def __init__(self, config, device=None, task=None):
        super().__init__(config=config, device=device, task=task)

    @property
    def equipment_code_config_key(self):
        """Возвращает путь ключа конфигурации кода снаряжения.

        Returns:
            str: Путь ключа конфигурации, например 'GemsFarming.GemsFarming.EquipmentCode'.
        """
        command = self.config.task.command if hasattr(self.config, 'task') and self.config.task else 'GemsFarming'
        return f"{command}.GemsFarming.EquipmentCode"

    def current_ship(self, skip_first_screenshot=True):
        """
        Использует шаблоны из module.retire.assets с адаптированным масштабом под текущий флагман.

        Pages:
            in: gear_code
        """
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            # Условие завершения
            if not self.appear(EMPTY_SHIP_R):
                break
            else:
                logger.info('[Фарм самоцветов] Ожидание загрузки значков кораблей.')

        if TEMPLATE_BOGUE.match(self.device.image, scaling=1.46):  # image has rotation
            return 'bogue'
        if TEMPLATE_HERMES.match(self.device.image, scaling=124 / 89):
            return 'hermes'
        if TEMPLATE_RANGER.match(self.device.image, scaling=4 / 3):
            return 'ranger'
        if TEMPLATE_LANGLEY.match(self.device.image, scaling=25 / 21):
            return 'langley'
        return 'DD'

    def clear_all_equip(self):
        """Экспортирует код снаряжения текущего флагмана и снимает всё снаряжение.

        Сохраняет конфигурацию снаряжения через код снаряжения перед разоружением,
        чтобы впоследствии применить её к новому флагману.

        Returns:
            bool: Успешно ли снято снаряжение.

        Raises:
            RequestHumanTakeover: Выбрасывается при ошибке экспорта кода снаряжения во избежание потери конфигурации.
        """
        success = self.code_clear()
        if not success:
            logger.warning('[Фарм самоцветов] Не удалось экспортировать код снаряжения; замена кораблей остановлена, чтобы не потерять состояние снаряжения.')
            raise RequestHumanTakeover
        return success

    def apply_equip_code(self, code=None):
        """Применяет код снаряжения к текущему кораблю.

        Применяет ранее экспортированный код снаряжения к новому флагману, восстанавливая конфигурацию.

        Args:
            code (str, optional): Строка кода снаряжения. Если None, используется последний экспортированный код.

        Returns:
            bool: Успешно ли применён код.

        Raises:
            RequestHumanTakeover: Выбрасывается при ошибке применения кода снаряжения.
        """
        if code is None:
            success = self.code_apply()
        else:
            success = self._code_apply(code=code)
        if not success:
            logger.warning('[Фарм самоцветов] Не удалось применить код снаряжения; проверьте снаряжение текущего флота вручную.')
            raise RequestHumanTakeover
        return success


class GemsFarming(CampaignRun, FleetEquipment, GemsEquipmentHandler, Retirement):
    """Основной класс задачи фарма алмазов.

    Объединяет функциональность выполнения кампании, управления флотом, работы с кодами снаряжения и отставки кораблей
    в единый автоматизированный цикл фарма алмазов.

    Основной рабочий процесс:
    1. Загрузка карты кампании и вылазка с авианосцем обычной редкости во главе флота
    2. Отслеживание уровня и настроения флагмана
    3. При достижении 32-го уровня или падении настроения — автоматическая замена на новый низкоуровневый авианосец
    4. Опциональная синхронная замена эсминца авангарда
    5. Автоматическое снятие и установка снаряжения флагмана/авангарда по кодам снаряжения

    Attributes:
        _initial_flagship_check_done (bool): Завершена ли начальная проверка уровня флагмана.
        _trigger_lv32 (bool): Сработало ли ограничение 32-го уровня.
        _trigger_emotion (bool): Сработало ли ограничение по настроению.
        hard_mode (bool): Активен ли сложный режим (влияет на навигацию по флоту).
        page_fleet_check_button (Button): Кнопка проверки нахождения на странице флота.
        fleet_detail_enter_flagship (Button): Кнопка входа в экран флагмана.
        fleet_detail_enter (Button): Кнопка входа в экран авангарда.
        fleet_enter_flagship (Button): Кнопка входа в слот флагмана из дока.
        fleet_enter (Button): Кнопка входа в слот авангарда из дока.
    """
    _initial_flagship_check_done = False

    def hard_mode_override(self):
        """Переключает способ входа во флот в зависимости от режима кампании.

        В сложном режиме используются другие кнопки входа на страницу редактирования флота (через экран подготовки кампании),
        а в обычном режиме вход осуществляется напрямую через page_fleet. Кнопки входа для флагмана/авангарда выбираются
        в соответствии с конфигурацией порядка флотов.
        """
        if self.campaign.config.Campaign_Mode == 'hard':
            logger.info('[Фарм самоцветов] Сложный режим: меняю способ замены кораблей')
            self.hard_mode = True
            self._ship_detail_enter = self._ship_detail_enter_hard
            self._fleet_detail_enter = self._fleet_detail_enter_hard
            self._fleet_back = self._fleet_back_hard
            self.page_fleet_check_button = FLEET_PREPARATION
            if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
                self.fleet_detail_enter_flagship = FLEET_DETAIL_ENTER_FLAGSHIP_HARD_2
                self.fleet_enter_flagship = FLEET_ENTER_FLAGSHIP_HARD_2
                self.fleet_detail_enter = FLEET_DETAIL_ENTER_HARD_2
                self.fleet_enter = FLEET_ENTER_HARD_2
            else:
                self.fleet_detail_enter_flagship = FLEET_DETAIL_ENTER_FLAGSHIP_HARD_1
                self.fleet_enter_flagship = FLEET_ENTER_FLAGSHIP_HARD_1
                self.fleet_detail_enter = FLEET_DETAIL_ENTER_HARD_1
                self.fleet_enter = FLEET_ENTER_HARD_1
        else:
            self.hard_mode = False
            self.page_fleet_check_button = page_fleet.check_button
            self.fleet_detail_enter_flagship = FLEET_DETAIL_ENTER_FLAGSHIP
            self.fleet_detail_enter = FLEET_DETAIL_ENTER
            self.fleet_enter_flagship = FLEET_ENTER_FLAGSHIP
            self.fleet_enter = FLEET_ENTER

    def load_campaign(self, name, folder='campaign_main'):
        """Загружает модуль карты кампании и внедряет переопределения для фарма алмазов.

        На базе родительского load_campaign() заменяет Campaign на подкласс с наследованием GemsCampaignOverride,
        внедряя управление настроением GemsEmotion.
        Устанавливает режим управления настроением в зависимости от того, ротируется ли авангард.

        Args:
            name (str): Имя файла карты.
            folder (str): Имя папки карты.
        """
        super().load_campaign(name, folder)

        class GemsCampaign(GemsCampaignOverride, self.module.Campaign):


            @cached_property
            def emotion(self) -> GemsEmotion:
                return GemsEmotion(config=self.config)

        self.campaign = GemsCampaign(device=self.campaign.device, config=self.campaign.config)
        if self.change_vanguard:
            self.campaign.config.override(Emotion_Mode='ignore_calculate')
            self.campaign.config.override(EnemyPriority_EnemyScaleBalanceWeight='S1_enemy_first')
        else:
            self.campaign.config.override(Emotion_Mode='ignore')

    @property
    def emotion_lower_bound(self):
        """Нижняя граница значения настроения.

        Динамически рассчитывает порог настроения по количеству боёв на текущей карте,
        гарантируя, что кораблям хватит настроения на всю кампанию.

        Returns:
            int: Нижняя граница настроения.
        """
        return 4 + self.campaign._map_battle * 2

    @property
    def change_flagship(self):
        """Требуется ли ротация флагмана.

        Returns:
            bool: True, если конфигурация содержит 'ship'.
        """
        return 'ship' in self.config.GemsFarming_ChangeFlagship

    @property
    def change_flagship_equip(self):
        """Требуется ли переустановка снаряжения флагмана.

        Returns:
            bool: True, если конфигурация содержит 'equip'.
        """
        return 'equip' in self.config.GemsFarming_ChangeFlagship

    @property
    def change_vanguard(self):
        """Требуется ли ротация корабля авангарда.

        Returns:
            bool: True, если конфигурация содержит 'ship'.
        """
        return 'ship' in self.config.GemsFarming_ChangeVanguard

    @property
    def change_vanguard_equip(self):
        """Требуется ли переустановка снаряжения авангарда.

        Returns:
            bool: True, если конфигурация содержит 'equip'.
        """
        return 'equip' in self.config.GemsFarming_ChangeVanguard

    @property
    def fleet_to_attack(self):
        """Возвращает номер флота для атаки.

        Возвращает фактически используемый номер флота в соответствии с настройкой порядка флотов.
        В режиме fleet1_standby_fleet2_all используется второй флот.

        Returns:
            int: Номер флота.
        """
        if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
            return self.config.Fleet_Fleet2
        else:
            return self.config.Fleet_Fleet1

    def _fleet_detail_enter(self, fleet):
        """Переходит на экран редактирования указанного флота (обычный режим).

        Осуществляет навигацию к флоту через page_fleet.

        Args:
            fleet (int): Номер флота.
        """
        self.ui_ensure(page_fleet)
        self.ui_ensure_index(fleet, letter=OCR_FLEET_INDEX,
                             next_button=FLEET_NEXT, prev_button=FLEET_PREV, skip_first_screenshot=True)

    def _ship_detail_enter(self, button):
        """Переходит на экран деталей снаряжения указанного корабля (обычный режим).

        С экрана флота переходит в детали флота, а затем на страницу снаряжения корабля.

        Args:
            button (Button): Кнопка слота корабля.
        """
        self.ui_click(FLEET_DETAIL, appear_button=page_fleet.check_button,
                      check_button=FLEET_DETAIL_CHECK, skip_first_screenshot=True)
        self.equip_enter(button, long_click=False)

    def _fleet_detail_enter_hard(self, fleet):
        """Переходит на экран редактирования указанного флота (сложный режим).

        В сложном режиме вход осуществляется через экран подготовки к бою кампании,
        поэтому сначала выполняется переход ко входу на этап и вход в подготовку.

        Args:
            fleet (int): Номер флота (не используется в сложном режиме, вход всегда фиксирован).
        """
        if self.appear(FLEET_PREPARATION, offset=(20, 50)):
            return
        self.campaign.ensure_campaign_ui(self.stage)
        self.ui_click(click_button=self.campaign.ENTRANCE, appear_button=BACK_ARROW, check_button=MAP_PREPARATION)
        while 1:
            self.device.screenshot()

            if self.appear_then_click(MAP_PREPARATION, interval=1):
                continue

            if self.handle_retirement():
                continue

            if self.appear(FLEET_PREPARATION, offset=(20, 50)):
                break

    def _ship_detail_enter_hard(self, button):
        """Переходит на экран деталей снаряжения указанного корабля (сложный режим).

        В сложном режиме переход выполняется напрямую через кнопку входа в снаряжение.

        Args:
            button (Button): Кнопка слота корабля.
        """
        self.equip_enter(button)

    def _fleet_back(self):
        """Возвращается из деталей снаряжения на экран флота (обычный режим)."""
        self.ui_back(FLEET_DETAIL_CHECK)
        self.ui_back(FLEET_CHECK)

    def _fleet_back_hard(self):
        """Возвращается из деталей снаряжения на экран подготовки (сложный режим)."""
        self.ui_back(self.page_fleet_check_button)

    def flagship_change(self):
        """
        Заменяет флагман и обновляет его снаряжение по коду снаряжения.

        Returns:
            bool: Успешно ли заменён флагман.
        """

        logger.hr('Замена флагмана', level=1)
        logger.attr('Замена флагмана', self.config.GemsFarming_ChangeFlagship)
        self._fleet_detail_enter(self.fleet_to_attack)
        if self.change_flagship_equip:
            logger.hr('Снятие снаряжения флагмана', level=2)
            self._ship_detail_enter(self.fleet_detail_enter_flagship)
            self.clear_all_equip()
            self._fleet_back()

        logger.hr('Замена флагмана', level=2)
        success = self.flagship_change_execute()

        if self.change_flagship_equip:
            logger.hr('Установка снаряжения флагмана', level=2)
            self._ship_detail_enter(self.fleet_detail_enter_flagship)
            self.apply_equip_code()
            self._fleet_back()

        return success

    def vanguard_change(self):
        """
        Заменяет корабль авангарда и обновляет его снаряжение по коду снаряжения.

        Returns:
            bool: Успешно ли заменён авангард.
        """
        logger.hr('Замена авангарда', level=1)
        logger.attr('Замена авангарда', self.config.GemsFarming_ChangeVanguard)
        self._fleet_detail_enter(self.fleet_to_attack)
        if self.change_vanguard_equip:
            logger.hr('Снятие снаряжения авангарда', level=2)
            self._ship_detail_enter(self.fleet_detail_enter)
            self.clear_all_equip()
            self._fleet_back()

        logger.hr('Замена авангарда', level=2)
        success = self.vanguard_change_execute()

        if self.change_vanguard_equip:
            logger.hr('Установка снаряжения авангарда', level=2)
            self._ship_detail_enter(self.fleet_detail_enter)
            self.apply_equip_code()
            self._fleet_back()


        return success

    def _dock_reset(self):
        """Сбрасывает фильтры и сортировку дока."""
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(wait_loading=False)
        self.dock_filter_set()

    def _ship_change_confirm(self, button):
        """Выбирает корабль и подтверждает замену.

        Args:
            button (Button): Кнопка выбираемого корабля.
        """
        self.dock_select_one(button)
        self._dock_reset()
        self.dock_select_confirm(check_button=self.page_fleet_check_button)

    def get_common_rarity_cv(self, lv=31, emotion=16):
        """
        Получает авианосец обычной редкости согласно config.GemsFarming_CommonCV.
        Если config.GemsFarming_CommonCV == 'any', возвращает обычный авианосец уровня 1~33.

        После вызова необходимо вызвать _dock_reset().

        Args:
            lv (int): Максимальный уровень обычного авианосца.
            emotion (int): Минимальное настроение обычного авианосца.

        Returns:
            Ship: Подходящий корабль.
        """
        faction = 'eagle' if self.config.GemsFarming_CommonCV == 'eagle' else 'all'
        extra = 'can_limit_break' if self.config.GemsFarming_AllowHighFlagshipLevel else 'enhanceable'
        self.dock_favourite_set(False, wait_loading=False)
        self.dock_sort_method_dsc_set(False, wait_loading=False)
        self.dock_filter_set(
            index='cv', rarity='common', faction=faction, extra=extra, sort='total')

        logger.hr('[Фарм самоцветов] Поиск флагмана')

        if self.config.GemsFarming_AllowHighFlagshipLevel:
            if self.config.SERVER in ['cn']:
                max_level = 100
            else:
                max_level = 70
            min_level = max_level
        else:
            max_level = lv
            min_level = 1
        emotion_lower_bound = 0 if emotion == 0 else self.emotion_lower_bound
        fleet = [0, self.fleet_to_attack] if self.config.GemsFarming_AllowHighFlagshipLevel else self.fleet_to_attack

        if self.config.GemsFarming_UseEmotionFirst:
            scanner = ShipScanner(
                level=(min_level, max_level), emotion=(emotion_lower_bound, 150), fleet=[0, self.fleet_to_attack], status='free')
            scanner.disable('rarity')

            if self.config.GemsFarming_CommonCV in ['custom', 'any', 'eagle']:
                if self.config.GemsFarming_CommonCV == 'custom':
                    filter_string = self.config.GemsFarming_CommonCVFilter
                else:
                    filter_string = self.config.COMMON_CV_FILTER
                common_ship = self.get_common_ship_filter(filter_string, ship_type='cv')
            else:
                common_ship = [self.config.GemsFarming_CommonCV]

            if common_ship is not None:
                candidates = self.find_all_backline_candidates(scanner, common_ship)
                if candidates:
                    return [candidates[0]]

                logger.info('[Фарм самоцветов] Указанный авианосец не найден; пробую обратный порядок.')
                self.dock_sort_method_dsc_set(True)
                candidates = self.find_all_backline_candidates(scanner, common_ship)
                if candidates:
                    return [candidates[0]]

                # Восстанавливаем порядок сортировки: он был изменён, но результат не найден
                self.dock_sort_method_dsc_set(False)
            logger.info('[Фарм самоцветов] UseEmotionFirst не нашёл подходящих кораблей; возвращаюсь к исходному способу выбора')

        scanner = ShipScanner(
            level=(min_level, max_level), emotion=(emotion_lower_bound, 150), fleet=fleet, status='free')
        scanner.disable('rarity')

        if not self.config.GemsFarming_AllowHighFlagshipLevel:
            ships = scanner.scan(self.device.image)
            if ships:
                # Текущий корабль менять не нужно
                return ships

            # Заменяем на любой корабль
            scanner.set_limitation(fleet=0)

        if self.config.GemsFarming_CommonCV in ['custom', 'any', 'eagle']:
            candidates = self.find_custom_candidates(scanner, ship_type='cv')

            if candidates:
                # Заменяем на указанный корабль
                return candidates

            return scanner.scan(self.device.image, output=False)

        else:
            template = TEMPLATE_COMMON_CV[f'{self.config.GemsFarming_CommonCV.upper()}']

            candidates = [ship for ship in scanner.scan(self.device.image, output=False)
                          if template.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE)]

            if candidates:
                # Заменяем на указанный корабль
                return candidates

            logger.info('[Фарм самоцветов] Указанный авианосец не найден; пробую обратный порядок.')
            self.dock_sort_method_dsc_set(True)

            candidates = [ship for ship in scanner.scan(self.device.image)
                          if template.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE)]

            return candidates

    def get_common_rarity_dd(self, emotion=16):
        """
        Получает обычный эсминец уровня 100 (70 для не-CN серверов) с настроением >= self.emotion_lower_bound.

        После вызова необходимо вызвать _dock_reset().

        Args:
            emotion (int): Минимальное настроение обычного эсминца.

        Returns:
            Ship: Подходящий корабль.
        """
        rarity = 'common'
        extra = 'can_limit_break'
        if self.config.GemsFarming_CommonDD in ['any', 'custom']:
            faction = ['eagle', 'iron']
        elif self.config.GemsFarming_CommonDD == 'favourite':
            faction = 'all'
        elif self.config.GemsFarming_CommonDD == 'z20_or_z21':
            faction = 'iron'
        elif self.config.GemsFarming_CommonDD == 'DDG':
            faction = 'dragon'
            rarity = 'super_rare'
            extra = 'no_limit'
        elif self.config.GemsFarming_CommonDD in ['aulick_or_foote', 'cassin_or_downes']:
            faction = 'eagle'
        else:
            logger.error(f'[Фарм самоцветов] Недопустимая настройка обычного эсминца: {self.config.GemsFarming_CommonDD}')
            raise ScriptError('Недопустимое значение GemsFarming_CommonDD')
        favourite = self.config.GemsFarming_CommonDD == 'favourite'
        self.dock_favourite_set(favourite, wait_loading=False)
        self.dock_sort_method_dsc_set(True, wait_loading=False)
        self.dock_filter_set(
            index='dd', rarity=rarity, faction=faction, extra=extra)

        logger.hr('[Фарм самоцветов] Поиск авангарда')

        min_level, max_level = self.config.GemsFarming_VanguardLevelMin, self.config.GemsFarming_VanguardLevelMax
        
        # Если новые настройки остались на абсолютных значениях по умолчанию (1, 125), возвращаемся к старой логике
        # Это сохраняет существующие конфигурации GemsFarming, неявно зависящие от 100/70.
        if min_level <= 1 and max_level >= 125:
            if self.config.SERVER in ['cn']:
                max_level = 100
            else:
                max_level = 70
            if getattr(self.config, 'GemsFarming_CommonDD', '') == 'DDG':
                max_level = 125
            if getattr(self.config, 'GemsFarming_AllowLowVanguardLevel', False):
                min_level = 30
            else:
                min_level = max_level
            if self.hard_mode:
                min_level = max(min_level, 70)
        emotion_lower_bound = 0 if emotion == 0 else self.emotion_lower_bound
        scanner = ShipScanner(level=(min_level, max_level), emotion=(emotion_lower_bound, 150),
                              fleet=[0, self.fleet_to_attack], status='free')
        scanner.disable('rarity')

        if self.config.GemsFarming_UseEmotionFirst:
            if self.config.GemsFarming_CommonDD == 'custom':
                filter_string = self.config.GemsFarming_CommonDDFilter
                common_ship = self.get_common_ship_filter(filter_string, ship_type='dd')
            elif self.config.GemsFarming_CommonDD == 'any':
                filter_string = self.config.COMMON_DD_FILTER
                common_ship = self.get_common_ship_filter(filter_string, ship_type='dd')
            elif self.config.GemsFarming_CommonDD == 'cassin_or_downes':
                common_ship = ['cassin', 'downes']
            elif self.config.GemsFarming_CommonDD == 'aulick_or_foote':
                common_ship = ['aulick', 'foote']
            elif self.config.GemsFarming_CommonDD == 'z20_or_z21':
                common_ship = ['z20', 'z21']
            else:
                common_ship = None

            if common_ship is not None:
                candidates = self.find_all_vanguard_candidates(scanner, common_ship)
                if candidates:
                    return candidates

                logger.info('[Фарм самоцветов] Указанный эсминец не найден; пробую обратный порядок.')
                self.dock_sort_method_dsc_set(False)
                candidates = self.find_all_vanguard_candidates(scanner, common_ship)
                if not candidates and self.config.GemsFarming_CommonDD == 'custom':
                    return scanner.scan(self.device.image, output=False)
                return candidates
            else:
                candidates = scanner.scan(self.device.image, output=False)
                if candidates:
                    candidates.sort(key=lambda s: s.emotion, reverse=True)
                    return candidates

        if self.config.GemsFarming_CommonDD in ['any', 'favourite', 'z20_or_z21', 'DDG']:
            # Заменяем на любой корабль
            return scanner.scan(self.device.image)

        elif self.config.GemsFarming_CommonDD == 'custom':
            candidates = self.find_custom_candidates(scanner, ship_type='dd')

            if candidates:
                # Заменяем на указанный корабль
                return candidates

            return scanner.scan(self.device.image, output=False)

        else:
            candidates = self.find_candidates(self.get_templates(self.config.GemsFarming_CommonDD), scanner)

            if candidates:
                # Заменяем на указанный корабль
                return candidates

            logger.info('[Фарм самоцветов] Указанный эсминец не найден; пробую обратный порядок.')
            self.dock_sort_method_dsc_set(False)

            # Заменяем на указанный корабль
            candidates = self.find_candidates(self.get_templates(self.config.GemsFarming_CommonDD), scanner)
            return candidates

    def match_ship_to_template(self, ship, template):
        """Проверяет соответствие значка корабля указанному шаблону.

        Args:
            ship (Ship): Объект корабля.
            template: Объект шаблона или список шаблонов.

        Returns:
            bool: Соответствует ли значок шаблону.
        """
        if isinstance(template, list):
            return any(item.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE) for item in template)
        else:
            return template.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE)

    def find_all_vanguard_candidates(self, scanner, common_ship):
        """
        Сканирует и находит всех подходящих кандидатов из списка common_ship, возвращая их по убыванию (настроение, -индекс приоритета).
        """
        templates_list = [TEMPLATE_COMMON_DD[name.upper()] for name in common_ship]
        all_ships = scanner.scan(self.device.image, output=False)
        matched_candidates = []
        for ship in all_ships:
            for i, template in enumerate(templates_list):
                if self.match_ship_to_template(ship, template):
                    matched_candidates.append((ship, i))
                    break
        # Сортируем по настроению (по убыванию) и индексу приоритета (по возрастанию)
        matched_candidates.sort(key=lambda x: (x[0].emotion, -x[1]), reverse=True)
        return [x[0] for x in matched_candidates]

    def find_all_backline_candidates(self, scanner, common_ship):
        """
        Сканирует и находит всех подходящих кандидатов из списка common_ship, сортируя в следующем порядке:
        1. Настроение (по убыванию)
        2. Уровень (по возрастанию)
        3. Индекс приоритета (по возрастанию)
        """
        templates_list = [TEMPLATE_COMMON_CV[name.upper()] for name in common_ship]
        all_ships = scanner.scan(self.device.image, output=False)
        matched_candidates = []
        for ship in all_ships:
            for i, template in enumerate(templates_list):
                if self.match_ship_to_template(ship, template):
                    matched_candidates.append((ship, i))
                    break
        # Сортируем по настроению (по убыванию), уровню (по возрастанию) и индексу приоритета (по возрастанию)
        matched_candidates.sort(key=lambda x: (x[0].emotion, -x[0].level, -x[1]), reverse=True)
        return [x[0] for x in matched_candidates]

    def find_custom_candidates(self, scanner, ship_type='cv'):
        """
        Находит кандидатов обычной редкости (CV/DD), используется только для режима 'custom' GemsFarming_CommonCV/DD.

        Args:
            scanner (ShipScanner): Сканер кораблей.
            ship_type (str): 'cv' или 'dd'.
        """
        if ship_type.lower() not in ['cv', 'dd']:
            logger.warning(f'[Фарм самоцветов] Недопустимый тип корабля: {ship_type}')
            return []

        ship_type = ship_type.upper()
        logger.info(f'[Фарм самоцветов] Поиск {ship_type} обычной редкости.')
        if ship_type.lower() == 'cv' and self.config.GemsFarming_CommonCV != 'custom':
            filter_string = self.config.COMMON_CV_FILTER
        else:
            filter_string =  self.config.__getattribute__(f'GemsFarming_Common{ship_type}Filter')
        sort_dsc_first = ship_type.lower() == 'dd'
    
        common_ship = self.get_common_ship_filter(filter_string, ship_type=ship_type)
        templates = globals()[f'TEMPLATE_COMMON_{ship_type}']
        find_first = True
        common_ship_candidates = {}
        for name in common_ship:
            template = templates[name.upper()]
            candidates = self.find_candidates(template, scanner)

            if find_first:
                find_first = False
                if candidates:
                    logger.info(f'[Фарм самоцветов] Найден подходящий {ship_type}: {name}')
                    return candidates

            common_ship_candidates[name] = candidates

        logger.info(f'[Фарм самоцветов] Подходящий {ship_type} не найден; пробую обратный порядок.')
        self.dock_sort_method_dsc_set(not sort_dsc_first)

        for name in common_ship:
            template = templates[name.upper()]
            candidates = self.find_candidates(template, scanner)

            if candidates:
                logger.info(f'[Фарм самоцветов] Найден подходящий корабль: {name}')
                return candidates
            elif common_ship_candidates[name]:
                logger.info(f'[Фарм самоцветов] Найден подходящий корабль: {name}')
                self.dock_sort_method_dsc_set(sort_dsc_first, wait_loading=False)
                return common_ship_candidates[name]

        return []

    def find_candidates(self, template, scanner):
        """
        Находит корабли-кандидаты на основе сопоставления шаблонов.
        """
        candidates = []
        if isinstance(template, list):
            for item in template:
                candidates = [ship for ship in scanner.scan(self.device.image, output=False)
                            if item.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE)]
                if candidates:
                    break
        else:
            candidates = [ship for ship in scanner.scan(self.device.image, output=False)
                          if template.match(self.image_crop(ship.button, copy=False), similarity=SIM_VALUE)]
        return candidates

    @staticmethod
    def get_templates(common_dd):
        """
        Возвращает список соответствующих шаблонов по значению настройки CommonDD.
        """
        if common_dd == 'aulick_or_foote':
            return [
                TEMPLATE_AULICK,
                TEMPLATE_FOOTE
            ]
        elif common_dd == 'cassin_or_downes':
            return [
                TEMPLATE_CASSIN_1, TEMPLATE_CASSIN_2,
                TEMPLATE_DOWNES_1, TEMPLATE_DOWNES_2
            ]
        else:
            logger.error(f'[Фарм самоцветов] Недопустимая настройка обычного эсминца: {common_dd}')
            raise ScriptError(f'Недопустимая настройка CommonDD: {common_dd}')

    def ship_down_hard(self):
        """Удаляет корабль из флота в сложном режиме.

        Если отображается кнопка снятия с позиции, нажимает её, иначе возвращается на экран подготовки.
        """
        if self.appear(DOCK_SHIP_DOWN):
            self.ui_click(DOCK_SHIP_DOWN,
                            appear_button=DOCK_CHECK, check_button=self.page_fleet_check_button, skip_first_screenshot=True)
        else:
            self.ui_back(check_button=FLEET_PREPARATION)

    def dock_enter(self, button):
        """Переходит на экран дока.

        Нажимает кнопку на странице флота для перехода в док.

        Args:
            button (Button): Кнопка для нажатия.

        Returns:
            bool: True при успешном входе; False, если обнаружена игровая подсказка и вход отменён.
        """
        for _ in self.loop():
            if self.appear(DOCK_CHECK, offset=(20, 20)):
                break
            if self.appear(self.page_fleet_check_button, offset=(30, 30), interval=5):
                self.device.click(button)
                continue
            # 2025.05.29 при входе в док игра показывает подсказку о функции скинов
            if self.handle_game_tips():
                return False
        return True

    def flagship_change_with_emotion(self, ship):
        """
        Заменяет флагман и рассчитывает настроение.
        """
        target_ship = max(ship, key=lambda s: (s.level, s.emotion))
        if self.change_vanguard:
            self.set_emotion(min(self.get_emotion(), target_ship.emotion))
        elif self.config.GemsFarming_AllowHighFlagshipLevel:
            self.set_emotion(target_ship.emotion)
        self._ship_change_confirm(target_ship.button)

    def flagship_change_execute(self):
        """
        Выполняет замену флагмана.

        Returns:
            bool: Успешно ли выполнена замена.

        Pages:
            in: page_fleet
            out: page_fleet
        """
        if self.hard_mode:
            if not self.dock_enter(self.fleet_detail_enter_flagship):
                return True
            self.ship_down_hard()  
        if not self.dock_enter(self.fleet_enter_flagship):
            return True

        ship = self.get_common_rarity_cv()
        if ship:
            self.flagship_change_with_emotion(ship)
            logger.info('[Фарм самоцветов] Флагман успешно заменён')
            return True
        else:
            logger.info('[Фарм самоцветов] Не удалось заменить флагман: авианосец обычной редкости не найден.')

            if self.config.SERVER in ['cn']:
                max_level = 100
            else:
                max_level = 70
            ship = self.get_common_rarity_cv(lv=max_level, emotion=0)
            if ship and self.hard_mode:
                self.flagship_change_with_emotion(ship)
            else:
                if self.hard_mode:
                    raise RequestHumanTakeover
                self._dock_reset()
                self.ui_back(check_button=self.page_fleet_check_button)
            return False

    def vanguard_change_with_emotion(self, ship):
        """
        Заменяет авангард и рассчитывает настроение.
        """
        target_ship = max(ship, key=lambda s: s.emotion)
        if self.change_vanguard:
            self.set_emotion(target_ship.emotion)
        self._ship_change_confirm(target_ship.button)

    def vanguard_change_execute(self):
        """
        Выполняет замену корабля авангарда.

        Returns:
            bool: Успешно ли выполнена замена.

        Pages:
            in: page_fleet
            out: page_fleet
        """
        if self.hard_mode:
            if not self.dock_enter(self.fleet_detail_enter):
                return True
            self.ship_down_hard()  
        if not self.dock_enter(self.fleet_enter):
            return True

        ship = self.get_common_rarity_dd()
        if ship:
            self.vanguard_change_with_emotion(ship)
            logger.info('[Фарм самоцветов] Корабль авангарда успешно заменён')
            return True
        else:
            logger.info('[Фарм самоцветов] Не удалось заменить корабль авангарда: эсминец обычной редкости не найден.')
            ship = self.get_common_rarity_dd(emotion=0)
            if ship and self.hard_mode:
                self.vanguard_change_with_emotion(ship)
            else:
                if self.hard_mode:
                    raise RequestHumanTakeover
                self._dock_reset()
                self.ui_back(check_button=self.page_fleet_check_button)
            return False

    _trigger_lv32 = False
    _trigger_emotion = False

    def triggered_stop_condition(self, oil_check=True):
        """Проверяет условия остановки фарма алмазов.

        К родительским условиям остановки добавляются:
        - Ограничение 32-го уровня: флагман достиг 32-го уровня (требуется смена флагмана)
        - Ограничение по настроению: настроение упало ниже допустимого порога (требуется смена корабля)

        Args:
            oil_check (bool): Проверять ли лимит нефти.

        Returns:
            bool: Сработало ли условие остановки.
        """
        # Ограничение 32-го уровня
        if self._trigger_lv32 or (
                self.change_flagship and self.campaign.config.LV32_TRIGGERED
                and not self.config.GemsFarming_AllowHighFlagshipLevel):
            self._trigger_lv32 = True
            logger.hr('[Кампания — фарм] Сработало ограничение 32-го уровня')
            return True

        if self.campaign.config.GEMS_EMOTION_TRIGGERED:
            self._trigger_emotion = True
            logger.hr('[Кампания — фарм] Сработало ограничение настроения')
            return True

        return super().triggered_stop_condition(oil_check=oil_check)

    def get_emotion(self):
        """
        Получает значение настроения флота из конфигурации.
        """
        if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
            return self.campaign.config.Emotion_Fleet2Value
        else:
            return self.campaign.config.Emotion_Fleet1Value

    def set_emotion(self, emotion):
        """
        Устанавливает значение настроения флота в конфигурации.
        """
        if self.config.Fleet_FleetOrder == 'fleet1_standby_fleet2_all':
            self.campaign.config.set_record(Emotion_Fleet2Value=emotion)
        else:
            self.campaign.config.set_record(Emotion_Fleet1Value=emotion)

    def run(self, name, folder='campaign_main', mode='normal', total=0):
        """
        Запускает задачу фарма алмазов.

        Args:
            name (str): Имя файла .py.
            folder (str): Имя папки внутри campaign.
            mode (str): `normal` или `hard`.
            total (int): Ограничение общего количества запусков.
        """
        self.config.STOP_IF_REACH_LV32 = self.change_flagship and not self.config.GemsFarming_AllowHighFlagshipLevel
        # Начальная проверка уровня флагмана.
        # Если включена замена флагмана, принудительно меняем его при запуске.
        # Это решает проблему запуска скрипта с флагманом 32-го уровня, который ещё не был отправлен в отставку.
        initial_check = (
            self.change_flagship
            and not self.config.GemsFarming_AllowHighFlagshipLevel
            and not self._initial_flagship_check_done
        )
        self._initial_flagship_check_done = True
        while 1:
            self._trigger_lv32 = initial_check
            initial_check = False
            is_limit = self.config.StopCondition_RunCount
            try:
                super().run(name=name, folder=folder, total=total)
            except CampaignEnd as e:
                if e.args[0] == 'Emotion control':
                    self._trigger_emotion = True
                elif e.args[0] == 'Emotion withdraw':
                    self._trigger_emotion = True
                    self.set_emotion(0)
                else:
                    raise e
            except HardNotSatisfied:
                try:
                    if self.change_flagship and self.change_vanguard:
                        self.hard_mode_override()
                        self.vanguard_change()
                        self.flagship_change()
                    else:
                        raise RequestHumanTakeover
                except RequestHumanTakeover:
                    raise
                except Exception:
                    from module.exception import GameStuckError
                    raise GameStuckError
            except RequestHumanTakeover as e:
                try:
                    if e.args and e.args[0] == 'Hard not satisfied' and self.change_flagship and self.change_vanguard:
                        self.hard_mode_override()
                        self.vanguard_change()
                        self.flagship_change()
                    else:
                        raise
                except RequestHumanTakeover:
                    raise
                except Exception:
                    from module.exception import GameStuckError
                    raise GameStuckError

            # Условие завершения
            if self._trigger_lv32 or self._trigger_emotion:
                success = True
                self.hard_mode_override()
                emotion = self.get_emotion()
                vanguard_success = True
                flagship_success = True
                if self.change_vanguard:
                    vanguard_success = self.vanguard_change()
                if self.change_flagship and (vanguard_success or self._trigger_lv32):
                    flagship_success = self.flagship_change()
                    if not flagship_success and self.config.GemsFarming_AllowHighFlagshipLevel:
                        self.set_emotion(emotion)
                success = vanguard_success and flagship_success

                if is_limit and self.config.StopCondition_RunCount <= 0:
                    logger.hr('Сработало условие остановки: число запусков')
                    self.config.StopCondition_RunCount = 0
                    self.config.Scheduler_Enable = False
                    break

                self._trigger_lv32 = False
                self.campaign.config.LV32_TRIGGERED = False
                self.campaign.config.GEMS_EMOTION_TRIGGERED = False

                # Планировщик
                if self.config.task_switched():
                    self._trigger_emotion = False
                    self.campaign.ensure_auto_search_exit()
                    self.config.task_stop()
                elif not success and (self.config.GemsFarming_DelayTaskIFNoFlagship \
                        or self._trigger_emotion):
                    self._trigger_emotion = False
                    self.campaign.ensure_auto_search_exit()
                    self.config.task_delay(minute=60)
                    self.config.task_stop()

                self._trigger_emotion = False
                continue
            else:
                break
